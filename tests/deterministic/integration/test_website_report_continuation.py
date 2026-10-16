"""Confirmed report continuation uses the real Web, domain and runtime paths."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from problem_locator.agent.intake import IntakeDecision, IntakeValue
from problem_locator.contracts import JobType, is_specialized_direct
from problem_locator.runtime.agent_backend import BackendExecution
from tests.deterministic.integration.test_website_agent import (
    _converse_to_waiting, _post, website,
)
from tests.deterministic.journey.test_rpc_timeout import ARCHIVE, PARAMETER_GROUP_A


OLD_REPORT = "# 首轮报告\n\n连接池等待可能导致 RPC 超时，尚需结合另一节点日志核实。\n"
NEW_REPORT = (
    "## 本轮变化\n\n原判断：连接池等待可能导致 RPC 超时。\n"
    "当前判断：结合更正后的时间和新增节点日志，继续保留连接池等待判断。\n"
    "依据：本轮两个日志包中的客户端超时和服务端等待记录。\n\n"
    "# 完整诊断报告\n\n建议检查连接池容量，并核对同一请求的处理时间。\n"
)


class _ReportBackend:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        assert kwargs["backend_phase"] == "METHODS_SPECIALIST"
        assert kwargs["broker_environment"] is None
        root = Path(kwargs["workspace_root"])
        self.calls.append({"prompt": kwargs["prompt"],
            "request": json.loads((root / "inputs/request.json").read_bytes()),
            "logs": json.loads((root / "inputs/target_logs.json").read_bytes())})
        sinks = kwargs["log_sinks"]
        for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
            sink.flush()
            sink.close()
        report = OLD_REPORT if len(self.calls) == 1 else NEW_REPORT
        return BackendExecution(returncode=0, stdout_stderr_bytes=0, workspace_bytes=0,
            elapsed_seconds=0.01,
            final_result=f"<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>\n{report}")


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _upload_second_archive(client, conversation):
    archive = io.BytesIO(ARCHIVE.read_bytes())
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(zipfile.ZipInfo("second-node.txt", date_time=(2026, 7, 31, 0, 0, 0)),
            "本轮补充的另一个节点。\n")
    data = archive.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    prepared = _post(client, "/api/v1/agent/attachments", {
        "conversation_id": conversation, "request_id": "continuation-logs", "name": "logs.zip",
        "content_type": "application/zip", "declared_size": len(data), "declared_sha256": digest})
    upload = prepared["upload"]
    response = client.put(upload["url"], content=data,
        headers={key: value for key, value in upload["required_headers"].items() if value is not None})
    assert response.status_code == 200, response.text
    return prepared["attachment"]["attachment_id"], digest


@pytest.mark.parametrize("log_mode", ["APPEND", "REPLACE"])
def test_confirmed_report_continuation_corrects_facts_selects_logs_and_preserves_versions(website, monkeypatch, log_mode):
    stack, store, engine, service, client = website
    stack.catalog._methods_evidence_validation = "off"
    backend = _ReportBackend()
    stack.runtime._diagnose_backend = backend
    conversation, prefix, first_upload = _converse_to_waiting(website)
    _post(client, prefix + "/messages", {"request_id": "first-facts",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    first = _get(client, prefix + "?include=report")
    assert first["result"]["markdown"] == OLD_REPORT
    parent_run, parent_case = first["run_id"], first["case_id"]
    original = stack.repository.read_case(parent_case)
    original_bytes = original.model_dump_json()
    assert len(backend.calls) == 1
    assert store.get_run(conversation, parent_run)["report_inputs"]["attachment_ids"] == [first_upload]

    intent_calls = []
    question = "原报告为什么认为连接池等待会导致超时？"
    def prepare(**kwargs):
        intent_calls.append(kwargs)
        assert kwargs["previous_report_markdown"] == OLD_REPORT
        return {"intent": "FOLLOWUP" if kwargs["text"] == question else "CONTINUE",
                "effective_problem_text": kwargs["source_problem_text"], "change_note": "按本次输入分流。"}
    monkeypatch.setattr(engine, "prepare_continuation", prepare, raising=False)
    inquiry = _post(client, prefix + f"/runs/{parent_run}/continuation-drafts", {
        "request_id": "report-question", "text": question})
    assert inquiry["status"] == "PREPARING"
    assert service.continuations.run_once(conversation)
    inquiry_path = prefix + f"/continuation-drafts/{inquiry['draft_id']}"
    inquiry = _get(client, inquiry_path)
    assert inquiry["status"] == "FOLLOWUP" and inquiry["followup_text"] == question
    blocked = client.post(inquiry_path + "/confirm", json={"request_id": "cannot-restart-question",
        "expected_revision": inquiry["revision"]})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "AGENT_FOLLOWUP_REQUIRED"
    assert store.get_run(conversation)["run_id"] == parent_run
    assert stack.repository.read_case(parent_case).model_dump_json() == original_bytes
    assert len(backend.calls) == 1

    second_upload, second_sha256 = _upload_second_archive(client, conversation)
    draft = _post(client, prefix + f"/runs/{parent_run}/continuation-drafts", {
        "request_id": "preview-continuation", "log_mode": log_mode, "attachment_ids": [second_upload],
        "change_note": "更正故障时间，并补充另一个节点日志。", "diagnosis_goal": "核实首轮连接池等待判断。"})
    assert draft["status"] == "PREPARING"
    assert service.continuations.run_once(conversation)
    draft = _get(client, prefix + f"/continuation-drafts/{draft['draft_id']}")
    assert draft["status"] == "DRAFT"
    assert len(intent_calls) == 2 and intent_calls[-1]["has_input_changes"] is True
    selected_uploads = [first_upload, second_upload] if log_mode == "APPEND" else [second_upload]
    assert draft["attachment_ids"] == selected_uploads
    assert draft["input_changes"]["added_attachments"][0]["attachment_id"] == second_upload
    corrected_time = "2026-07-31T00:00:04.000Z"
    effective = draft["effective_problem_text"].replace(PARAMETER_GROUP_A["problem_time"], corrected_time)
    assert corrected_time in effective and PARAMETER_GROUP_A["problem_time"] not in effective
    changed = client.patch(prefix + f"/continuation-drafts/{draft['draft_id']}", json={
        "request_id": "correct-facts", "expected_revision": draft["revision"],
        "effective_problem_text": effective})
    assert changed.status_code == 200, changed.text
    draft = changed.json()["data"]
    assert draft["input_changes"]["after_problem_text"] == effective
    assert store.get_run(conversation)["run_id"] == parent_run
    assert len(backend.calls) == 1
    assert stack.repository.read_case(parent_case).model_dump_json() == original_bytes

    def corrected_intake(request):
        facts = []
        for requirement in request.requirements:
            if requirement.kind != "INPUT":
                continue
            value = corrected_time if requirement.name == "problem_time" else PARAMETER_GROUP_A[requirement.name]
            source = next(message for message in reversed(request.messages)
                if message.role == "USER" and value in message.text)
            facts.append(IntakeValue(name=requirement.name, value=value,
                source_message_id=source.message_id, source_quote=value))
        return IntakeDecision(action="SUBMIT_SUPPLEMENT", message="已采用确认后的本轮信息。",
            problem_fields=[], user_facts=facts)

    monkeypatch.setattr(engine, "intake", corrected_intake)
    confirmation = {"request_id": "confirm-continuation", "expected_revision": draft["revision"]}
    confirmed = _post(client, prefix + f"/continuation-drafts/{draft['draft_id']}/confirm", confirmation)
    assert _post(client, prefix + f"/continuation-drafts/{draft['draft_id']}/confirm", confirmation) == confirmed
    assert confirmed["parent_run_id"] == parent_run and confirmed["run_id"] != parent_run
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    assert service.run_once(conversation)
    service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)

    latest = _get(client, prefix + "?include=report")
    assert latest["run_id"] == confirmed["run_id"]
    assert latest["case_id"] != parent_case
    current = stack.repository.read_case(latest["case_id"])
    assert latest["result"]["markdown"] == NEW_REPORT, json.dumps({
        "report": latest["result"], "failure": latest["failure"], "status": latest["status"],
        "case_failure": None if current.case.failure is None else current.case.failure.model_dump(mode="json"),
        "jobs": [(job.job_type.value, job.status.value) for job in current.jobs.values()],
    }, ensure_ascii=False)
    assert current.case.raw_problem_text == effective
    assert current.case.diagnosis_state.problem_spec.statement == effective
    route_jobs = [job for job in current.jobs.values() if job.job_type is JobType.ROUTE]
    assert len(route_jobs) == 1 and route_jobs[0].continuation_context is None
    diagnosis = current.jobs[current.case.generic_result_v2.source_job_id]
    assert is_specialized_direct(diagnosis)
    assert len(diagnosis.attachment_refs) == len(selected_uploads)
    expected_hashes = {hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(), second_sha256} if log_mode == "APPEND" else {second_sha256}
    assert {current.attachments[aid].sha256 for aid in diagnosis.attachment_refs} == expected_hashes
    assert not set(diagnosis.attachment_refs) & set(original.attachments)
    context = diagnosis.continuation_context
    assert context == current.case.continuation_context
    assert context.parent_run_id == parent_run and context.parent_case_id == parent_case
    assert context.parent_report_markdown == OLD_REPORT and context.parent_report_version == 1
    assert context.parent_report_sha256 == hashlib.sha256(OLD_REPORT.encode()).hexdigest()
    times = [fact.statement for fact in diagnosis.context_snapshot.user_facts
        if fact.status.value == "ACTIVE" and fact.provenance.input_name == "problem_time"]
    assert times == [corrected_time]
    assert len(backend.calls) == 2
    assert "连接池等待可能导致 RPC 超时" in backend.calls[1]["prompt"]
    assert "## 本轮变化" in backend.calls[1]["prompt"]
    assert len(backend.calls[1]["logs"]["target_logs"]) == 2 * len(selected_uploads)
    assert len({row["source_archive"]["attachment_id"]
        for row in backend.calls[1]["logs"]["target_logs"]}) == len(selected_uploads)
    assert stack.repository.read_case(parent_case).model_dump_json() == original_bytes
    historical = _get(client, prefix + f"?include=report&run_id={parent_run}")
    assert historical["case_id"] == parent_case and historical["result"]["markdown"] == OLD_REPORT
    versions = _get(client, prefix + f"/runs/{confirmed['run_id']}/report-versions")
    assert versions["root_run_id"] == parent_run and versions["latest_run_id"] == confirmed["run_id"]
    assert [item["version"] for item in versions["versions"]] == [1, 2]
    assert versions["versions"][1]["parent_run_id"] == parent_run
    assert versions["versions"][1]["input_changes"]["after_problem_text"] == effective
    removed = versions["versions"][1]["input_changes"]["removed_attachments"]
    assert [item["attachment_id"] for item in removed] == ([] if log_mode == "APPEND" else [first_upload])
    assert versions["can_continue"] is True
    assert _get(client, prefix + f"/runs/{parent_run}/report-versions")["can_continue"] is False
