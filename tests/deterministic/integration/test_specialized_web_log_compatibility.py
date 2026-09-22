"""Generic archive handoff must preserve specialized Web intake semantics."""
from __future__ import annotations

import threading
import uuid

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import (
    DiagnosisMode, ErrorCode, ExecutionFailure, ExecutionStage, GenericDiagnosisOutcome, JobOutcome,
    JobType, MarkInitialLogArchiveExpected, RestartGenericDiagnosis,
)
from tests.deterministic.integration.test_website_agent import OWNER_KEY, PARAMETER_GROUP_A, _preupload, website
from tests.deterministic.unit.domain._builders import diagnosis_outcome


@pytest.mark.parametrize("boundary", ["specialized_running", "specialized_waiting"])
def test_route_marker_losing_to_specialized_accepts_original_message_once(website, monkeypatch, boundary):
    stack, store, engine, service, client = website
    route_entered, route_release = threading.Event(), threading.Event()
    specialized_entered, specialized_release = threading.Event(), threading.Event()
    original_runtime = stack.runtime.execute
    original_execute = type(stack.application).execute
    commands = []
    def hold_runtime(job, cancellation):
        if job.job_type is JobType.ROUTE:
            route_entered.set()
            assert route_release.wait(15)
        elif job.diagnosis_mode is DiagnosisMode.SPECIALIZED:
            specialized_entered.set()
            assert specialized_release.wait(15)
        return original_runtime(job, cancellation)
    def route_wins(app, command):
        commands.append(command)
        if isinstance(command, MarkInitialLogArchiveExpected):
            route_release.set()
            assert specialized_entered.wait(15)
            if boundary == "specialized_waiting":
                specialized_release.set()
                assert stack.scheduler.wait_until_idle(15)
        return original_execute(app, command)
    monkeypatch.setattr(stack.runtime, "execute", hold_runtime)
    monkeypatch.setattr(type(stack.application), "execute", route_wins)
    created = service.create_conversation("specialized-marker-race", owner_key=OWNER_KEY)
    cid = created.conversation_id
    service.send_message(cid, "problem", "RPC timeout")
    try:
        assert service.run_once(cid)
        assert route_entered.wait(5)
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        text = "\n".join(f"{key}={value}" for key, value in PARAMETER_GROUP_A.items())
        receipt = service.send_message(cid, "racing-log-input", text, [attachment])
        assert receipt.run_id == created.run_id
        assert service.send_message(cid, "racing-log-input", text, [attachment]) == receipt
        assert len([event for event in store.list_events(cid, limit=500) if event.type == "message.accepted"
            and event.data["request_id"] == "racing-log-input"]) == 1
        assert store.pending_generic_restarts() == []
        specialized_release.set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_INPUT"
        assert service.run_once(cid)
        assert stack.scheduler.wait_until_idle(15)
        view = store.get_conversation(cid)
        case = stack.application.get_case(view.case_id).case_view
        assert case.status.value == "RESOLVED" and case.raw_problem_text == "RPC timeout"
        assert view.messages[-1].status == "APPLIED" and len(engine.calls) == 1
        assert store.get_attachment(attachment).status == "IMPORTED"
        assert not any(isinstance(command, RestartGenericDiagnosis) for command in commands)
        assert all(job.diagnosis_mode is not DiagnosisMode.GENERIC
            for job in stack.repository.read_snapshot(case.case_id).cases[case.case_id].jobs.values())
    finally:
        route_release.set()
        specialized_release.set()


def test_repeated_archive_during_route_preserves_additional_specialized_inputs(website, monkeypatch):
    stack, store, engine, service, client = website
    entered, release = threading.Event(), threading.Event()
    original_runtime = stack.runtime.execute
    def hold_route(job, cancellation):
        if job.job_type is JobType.ROUTE:
            entered.set()
            assert release.wait(15)
        return original_runtime(job, cancellation)
    monkeypatch.setattr(stack.runtime, "execute", hold_route)
    cid = service.create_conversation("same-archive-new-specialized-input", owner_key=OWNER_KEY).conversation_id
    service.send_message(cid, "problem", "RPC timeout")
    try:
        assert service.run_once(cid)
        assert entered.wait(5)
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        facts = list(PARAMETER_GROUP_A.items())
        first_text = "\n".join(f"{key}={value}" for key, value in facts[:4])
        second_text = "\n".join(f"{key}={value}" for key, value in facts[4:])
        first = service.send_message(cid, "inputs-one", first_text, [attachment])
        second = service.send_message(cid, "inputs-two", second_text, [attachment])
        assert first.message_id != second.message_id
        assert service.send_message(cid, "inputs-two", second_text, [attachment]) == second
        release.set()
        assert stack.scheduler.wait_until_idle(15)
        assert service.run_once(cid)
        assert service.run_once(cid)
        assert stack.scheduler.wait_until_idle(15)
        view = store.get_conversation(cid)
        case = stack.application.get_case(view.case_id).case_view
        assert case.status.value == "RESOLVED" and case.raw_problem_text == "RPC timeout"
        assert [item.status for item in view.messages] == ["APPLIED", "APPLIED", "APPLIED"]
        assert len(engine.calls) == 2 and len(stack.repository.read_case(case.case_id).attachments) == 1
        assert {item.provenance.input_name: item.statement for item in case.user_facts} == PARAMETER_GROUP_A
    finally:
        release.set()


def _reroute_outcome(stack, job):
    value = diagnosis_outcome().model_dump(mode="json")
    outcome_id = str(uuid.uuid4())
    value.update(outcome_id=outcome_id, case_id=job.case_id, job_id=job.job_id,
        base_state_revision=job.base_state_revision, produced_at=stack.clock.now(), result_type="REROUTE",
        consumed_evidence_refs=[], proposed_evidence=[], proposed_artifacts=[])
    value["payload"].update(candidate_conclusion_draft=None, recommended_next_step="根据新增问题重新选择定位能力。")
    value["payload"]["state_delta"]["add_open_questions"] = [dict(item_id=str(uuid.uuid4()),
        statement="需要确认故障是否超出当前 RPC 定位范围。", evidence_bindings=[], supersedes=[],
        provenance=dict(source_type="AGENT_OUTCOME", source_ref=outcome_id, input_name=None))]
    audit = value["decision_audit"]
    audit.update(job_id=job.job_id, case_id=job.case_id, skill_ref=job.skill_ref.model_dump(), required_evidence_bindings=[])
    for rule in audit["rules"]:
        rule["server_evaluation"]["evidence_bindings"] = []
    return JobOutcome.model_validate(value)


@pytest.mark.parametrize("boundary", ["generic", "route"])
def test_specialized_conversion_racing_reroute_adopts_logs_in_generic_once(website, monkeypatch, boundary):
    stack, store, engine, service, client = website
    route_entered, route_release = threading.Event(), threading.Event()
    specialized_entered, specialized_release = threading.Event(), threading.Event()
    reroute_entered, reroute_release = threading.Event(), threading.Event()
    generic_entered, adopted, finish = threading.Event(), threading.Event(), threading.Event()
    original_runtime, original_execute, original_submit = stack.runtime.execute, type(stack.application).execute, store.submit_message
    routes, generic_jobs = [], []

    def runtime(job, cancellation):
        if job.job_type is JobType.ROUTE:
            routes.append(job)
            entered, release = (route_entered, route_release) if len(routes) == 1 else (reroute_entered, reroute_release)
            entered.set()
            assert release.wait(15)
            return original_runtime(job, cancellation)
        if job.diagnosis_mode is DiagnosisMode.SPECIALIZED:
            specialized_entered.set()
            assert specialized_release.wait(15)
            return stack.runtime._publisher.publish_success(job, _reroute_outcome(stack, job))
        if job.diagnosis_mode is DiagnosisMode.GENERIC:
            if job.generic_log_archive_expected and not job.attachment_refs:
                return original_runtime(job, cancellation)
            generic_jobs.append(job)
            if not job.attachment_refs:
                generic_entered.set()
                assert cancellation.wait(15)
                return stack.runtime._publisher.publish_failure(job, ExecutionFailure(code=ErrorCode.BACKEND_CANCELLED,
                    stage=ExecutionStage.BACKEND_EXECUTE, message="旧任务已取消。", retryable=False, details=[]))
            adopted.set()
            assert finish.wait(15)
            outcome = JobOutcome(outcome_id=str(uuid.uuid4()), job_id=job.job_id, case_id=job.case_id,
                job_type=job.job_type, base_state_revision=job.base_state_revision, result_type="COMPLETED",
                payload=GenericDiagnosisOutcome(status="UNRESOLVED", conclusion="日志已接入。",
                    root_cause_analysis="测试仅验证日志接入和任务替换。", skill_name=job.generic_skill_name),
                consumed_evidence_refs=[], proposed_evidence=[], proposed_artifacts=[], error=None, produced_at=stack.clock.now())
            return stack.runtime._publisher.publish_success(job, outcome)
        return original_runtime(job, cancellation)

    def execute(app, command):
        if isinstance(command, MarkInitialLogArchiveExpected) and not specialized_entered.is_set():
            route_release.set()
            assert specialized_entered.wait(15)
        return original_execute(app, command)

    def submit(*args, **kwargs):
        if kwargs.get("routed_request_key"):
            assert stack.repository.read_case(store.get_status(args[0]).case_id).case.selected_skill_ref is not None
            monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
            specialized_release.set()
            assert reroute_entered.wait(15)
            if boundary == "generic":
                reroute_release.set()
                assert generic_entered.wait(15)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(stack.runtime, "execute", runtime)
    monkeypatch.setattr(type(stack.application), "execute", execute)
    monkeypatch.setattr(store, "submit_message", submit)
    cid = service.create_conversation("reroute-conversion-race", owner_key=OWNER_KEY).conversation_id
    service.send_message(cid, "problem", "RPC timeout")
    try:
        assert service.run_once(cid) and route_entered.wait(5)
        attachment = _preupload(client, f"/api/v1/agent/conversations/{cid}")
        if boundary == "route":
            with pytest.raises(AgentStoreError) as error:
                service.send_message(cid, "logs", "追加故障现场日志", [attachment])
            assert error.value.code == "AGENT_RESTART_PENDING"
            assert not any(item.request_id == "logs" for item in store.get_conversation(cid).messages)
            assert store.pending_generic_restarts()[0]["status"] == "PENDING"
            reroute_release.set()
            assert generic_entered.wait(15)
        receipt = service.send_message(cid, "logs", "追加故障现场日志", [attachment])
        assert service.send_message(cid, "logs", "追加故障现场日志", [attachment]) == receipt
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
        assert service.run_once(cid) and adopted.wait(15)
        view = store.get_conversation(cid)
        assert view.messages[-1].status == "APPLIED" and len(routes) == 2 and engine.calls == []
        assert store.get_attachment(attachment).status == "IMPORTED"
        assert len(generic_jobs) == 2 and not generic_jobs[0].generic_log_archive_expected
        assert generic_jobs[1].generic_log_archive_expected
        assert generic_jobs[1].attachment_refs == [store.get_attachment_import(attachment, receipt.run_id)]
        assert generic_jobs[1].generic_supplement_texts == ["追加故障现场日志"]
        assert len([event for event in store.list_events(cid, limit=500) if event.type == "message.accepted"
            and event.data["request_id"] == "logs"]) == 1
        assert store.pending_generic_restarts() == []
        finish.set()
        assert stack.scheduler.wait_until_idle(15)
        assert store.get_conversation(cid).messages[-1].status == "APPLIED"
    finally:
        route_release.set()
        specialized_release.set()
        reroute_release.set()
        finish.set()
        if generic_jobs and not adopted.is_set():
            stack.scheduler.cancel(generic_jobs[0].job_id)
