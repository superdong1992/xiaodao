"""Website raw conversation → grounded RPC report, using deterministic agents."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from problem_locator.agent.intake import INTAKE_RESOURCE_LIMITS, IntakeDecision, IntakeError, IntakeValue, build_intake_prompt
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import ApplicationError, ApplicationErrorDetail, ApplicationPortError, CreateCase, ErrorCode, ReviewPolicy, canonical_json_bytes
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.operational import OperationalState
from tests.deterministic.journey.test_rpc_timeout import (
    ARCHIVE, PARAMETER_GROUP_A, _Stack, _windows_extended_path,
)
from tests.deterministic.unit.interfaces.fakes import FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness


class ScriptedIntake:
    def __init__(self):
        self.calls = []

    def intake(self, request):
        self.calls.append(request)
        assert request.frozen_problem_spec is not None, "新问题必须先创建 Case，不能先调用 INTAKE。"
        facts = []
        for item in request.requirements:
            if item.kind != "INPUT":
                continue
            text = PARAMETER_GROUP_A[item.name]
            source = next((message for message in reversed(request.messages)
                if message.role == "USER" and text in message.text), None)
            if source is not None:
                facts.append(IntakeValue(name=item.name, value=text,
                    source_message_id=source.message_id, source_quote=text))
        if facts:
            return IntakeDecision(action="SUBMIT_SUPPLEMENT", message="已收到补充信息。", problem_fields=[],
                user_facts=facts)
        return IntakeDecision(action="NEED_CLARIFICATION", message="请补充预期表现、实际表现和定位范围。",
            problem_fields=[], user_facts=[])


@pytest.fixture
def website(tmp_path):
    import os
    root = tmp_path.parent / ("wa-" + uuid.uuid4().hex[:8])
    root.mkdir()
    if os.name == "nt":
        root = _windows_extended_path(root)
    released = root / "released"
    released.write_text("pass\n", encoding="utf-8")
    stack = _Stack(root / "data", logparse_record=root / "logparse.json", agent_record=root / "agent.jsonl",
        review_entered=root / "entered", review_release=released, seed="website-v11")
    store = AgentStore(stack.repository, stack.clock, stack.ids, runtime_epoch="website-epoch")
    engine = ScriptedIntake()
    service = AgentConversationService(store, stack.application, engine, stack.layout)
    stack.runtime._public_progress = store.append_case_progress
    app = create_http_app(command_port=stack.application, query_port=stack.application,
        state_admin=FakeStateAdmin(readiness=readiness()), public_base_url="http://testserver", agent_service=service)
    stack.start()
    with TestClient(app) as client:
        yield stack, store, engine, service, client
    assert service.shutdown(2)
    stack.shutdown()


def _post(client, path, data):
    response = client.post(path, json=data)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _preupload(client, prefix):
    data = ARCHIVE.read_bytes()
    prepared = _post(client, prefix + "/attachments", {"request_id": "logs:1", "name": "logs.zip",
        "content_type": "application/zip", "declared_size": len(data), "declared_sha256": hashlib.sha256(data).hexdigest()})
    upload = prepared["upload"]
    uploaded = client.put(upload["url"], content=data,
        headers={key: value for key, value in upload["required_headers"].items() if value is not None})
    assert uploaded.status_code == 200, uploaded.text
    return prepared["attachment"]["attachment_id"]


def _converse_to_waiting(website):
    stack, store, engine, service, client = website
    conversation = _post(client, "/api/v1/agent/conversations", {"request_id": "create:1"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{conversation}"
    attachment_id = _preupload(client, prefix)
    first = {"request_id": "message:1", "text": "RPC timeout", "attachment_ids": [attachment_id]}
    accepted = _post(client, prefix + "/messages", first)
    assert _post(client, prefix + "/messages", first) == accepted
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = service.get_conversation(conversation)
    assert view.case_id
    assert view.case_status == "WAITING_INPUT", view
    assert engine.calls == []
    return conversation, prefix, attachment_id


@pytest.mark.parametrize("original", [
    "RPC timeout",
    "  RPC timeout\n预期：RPC completes\n订单：synthetic-order-0001\n  ",
])
def test_first_problem_creates_case_before_one_requirement_aware_intake(website, monkeypatch, original):
    stack, store, engine, service, client = website
    commands = []
    execute = stack.application.execute

    def recorded(command):
        if isinstance(command, CreateCase):
            commands.append(command)
        return execute(command)

    monkeypatch.setattr(type(stack.application), "execute", lambda self, command: recorded(command))
    conversation = service.create_conversation("case-first").conversation_id
    prefix = f"/api/v1/agent/conversations/{conversation}"
    message = {"request_id": "first", "text": original}
    receipt = _post(client, prefix + "/messages", message)
    assert _post(client, prefix + "/messages", message) == receipt
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    assert engine.calls == []
    assert service.get_conversation(conversation).current_questions == []
    assert service.run_once(conversation)
    assert not service.run_once(conversation)
    assert _post(client, prefix + "/messages", message) == receipt
    assert len(commands) == 1
    command = commands[0]
    assert command.raw_problem_text == original
    assert command.problem_spec.model_dump() == {
        "statement": original,
        "expected_behavior": "用户未单独说明；以 raw_problem_text 为准。",
        "actual_behavior": original,
        "scope": "仅定位 raw_problem_text 所述问题。",
        "goals": ["定位问题原因并给出结论。"],
        "non_goals": [],
        "constraints": [],
        "completion_criteria": ["给出基于证据的结论；证据不足时明确说明。"],
    }
    assert command.initial_user_facts == [] and command.wait_seconds == 0
    view = service.get_conversation(conversation)
    case = stack.application.get_case(view.case_id).case_view
    assert view.messages[0].status == "APPLIED"
    assert case.raw_problem_text == original and case.user_facts == []
    assert len(engine.calls) == 1
    events = store.list_events(conversation, limit=500)
    created = next(item for item in events if item.type == "case.updated")
    assert not any(item.type == "assistant.question" for item in events if item.sequence < created.sequence)
    expected_questions = [item.prompt for item in case.pending_requirements if item.status.value == "OPEN"]
    assert view.current_questions == expected_questions
    assert all(item.data["questions"] == expected_questions for item in events if item.type == "assistant.question")


def test_attachment_only_message_waits_only_for_description_and_is_imported_after_case_exists(website):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("attachment-first").conversation_id
    prefix = f"/api/v1/agent/conversations/{conversation}"
    attachment_id = _preupload(client, prefix)
    message = {"request_id": "logs-only", "text": None, "attachment_ids": [attachment_id]}
    receipt = _post(client, prefix + "/messages", message)
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.case_id is None and view.messages[0].status == "APPLIED"
    assert view.current_questions == ["请描述需要定位的问题。"]
    assert engine.calls == []
    assert store.get_attachment(attachment_id).status == "READY"
    assert not service.run_once(conversation)
    assert _post(client, prefix + "/messages", message) == receipt
    service.send_message(conversation, "description", "RPC timeout")
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = service.get_conversation(conversation)
    assert view.case_id and engine.calls == []
    assert stack.application.get_case(view.case_id).case_view.raw_problem_text == "RPC timeout"
    assert service.run_once(conversation)
    assert store.get_attachment(attachment_id).status == "IMPORTED"
    assert len(stack.repository.read_case(view.case_id).attachments) == 1
    assert not service.run_once(conversation)


def test_followup_clarification_only_repeats_open_requirement_prompts(website):
    stack, store, engine, service, _ = website
    conversation, _, _ = _converse_to_waiting(website)
    before = service.get_conversation(conversation)
    case = stack.application.get_case(before.case_id).case_view
    expected = [item.prompt for item in case.pending_requirements
        if item.status.value == "OPEN" and item.kind.value == "INPUT"]
    service.send_message(conversation, "unclear", "暂时没有更多信息。")
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.case_id == before.case_id and view.current_questions == expected
    assert view.messages[-1].status == "APPLIED"
    assert len(engine.calls) == 1
    assert stack.application.get_case(view.case_id).case_view.case_revision > case.case_revision
    questions = [item for item in store.list_events(conversation, limit=500) if item.type == "assistant.question"]
    assert questions and all(item.data["questions"] == expected for item in questions)


def test_followup_correction_keeps_frozen_problem_and_requires_new_case(website, monkeypatch):
    stack, _, engine, service, _ = website
    conversation, _, _ = _converse_to_waiting(website)
    before = service.get_conversation(conversation)
    case = stack.application.get_case(before.case_id).case_view

    def correction(request):
        last = next(item for item in reversed(request.messages) if item.role == "USER")
        return IntakeDecision(action="NEW_CASE_REQUIRED", message="请新建任务描述更正后的问题。", user_facts=[],
            problem_fields=[IntakeValue(name="statement", value=last.text,
                source_message_id=last.message_id, source_quote=last.text)])

    monkeypatch.setattr(engine, "intake", correction)
    service.send_message(conversation, "correction", "实际问题是数据库连接失败。")
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.case_id == before.case_id and view.messages[-1].status == "UNUSED"
    assert view.messages[-1].notice == "请新建任务描述更正后的问题。"
    after = stack.application.get_case(view.case_id).case_view
    assert after.problem_spec == case.problem_spec and after.case_revision == case.case_revision


@pytest.mark.parametrize("review", [False, True])
def test_raw_conversation_preupload_sse_and_final_report(website, review):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    conversation, prefix, attachment_id = _converse_to_waiting(website)
    message = {"request_id": "message:2",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())}
    accepted = _post(client, prefix + "/messages", message)
    assert service.run_once(conversation)
    service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", stack.application.get_case(view.case_id).case_view.failure
    assert view.archive_status == "PENDING"
    assert service.list_events(conversation)["stream_closed"] is False
    events = store.list_events(conversation, limit=500)
    result = next(event for event in events if event.type == "result.available")
    assert result.data["result_field"] == "final_result"
    assert [item["kind"] for item in result.data["artifacts"]] == ["USER_RESULT"]
    assert any(event.type == "agent.progress" and event.data["stage"] == "LOGPARSE" for event in events)
    if review:
        reviewing = next(event for event in events if event.type == "case.updated" and event.data["status"] == "REVIEWING")
        assert reviewing.sequence < result.sequence
        assert not any(event.type == "result.available" for event in events if event.sequence <= reviewing.sequence)
    assert store.get_attachment(attachment_id).status == "IMPORTED"
    assert len(stack.repository.read_case(view.case_id).attachments) == 1
    assert len(engine.calls) == 1
    assert _post(client, prefix + "/messages", message) == accepted
    assert client.post(prefix + "/messages", json={"request_id": "later", "text": "新问题"}).status_code == 409
    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    report = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    downloaded = client.get(report["download_url"])
    assert len(downloaded.content) == report["size"] == int(downloaded.headers["content-length"])
    assert hashlib.sha256(downloaded.content).hexdigest() == report["sha256"]
    assert downloaded.json()["format_id"] == "problem-locator-diagnosis-v3"
    assert stack.archive.run_once()
    assert service.get_conversation(conversation).status == "COMPLETED"
    response = client.get(prefix + "/events", headers={"Last-Event-ID": str(result.sequence)})
    assert response.status_code == 200
    frames = response.content.split(b"\n\n")
    assert frames[0] == b": connected" and frames[-1] == b""
    assert all(frame.startswith(b"data: ") and b"\n" not in frame for frame in frames[1:-1])
    streamed = [json.loads(frame[6:]) for frame in frames[1:-1]]
    assert any(item["type"] == "archive.updated" and item["data"]["status"] == "READY" for item in streamed)
    assert streamed[-1]["type"] == "conversation.completed"
    assert all(item["sequence"] > result.sequence and item["type"] != "result.available" for item in streamed)
    assert len(engine.calls) == 1


def test_restart_retains_history_and_requires_explicit_new_task(website):
    stack, store, engine, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    original_calls = len(engine.calls)
    first_events = [item.model_dump() for item in store.list_events(conversation)]
    store.runtime_epoch = "restarted-epoch"
    store.recover()
    assert service.get_conversation(conversation).status == "INTERRUPTED"
    assert len(engine.calls) == original_calls
    assert [item.model_dump() for item in store.list_events(conversation)[:len(first_events)]] == first_events
    assert client.post(prefix + "/messages", json={"request_id": "new", "text": "继续"}).status_code == 409
    assert not service.run_once(conversation)


@pytest.mark.parametrize("unrelated_runtime_fault", [False, True])
def test_restart_history_and_sse_survive_missing_volatile_case_with_operational_state(website, unrelated_runtime_fault):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("lost-volatile-case").conversation_id
    service.send_message(conversation, "received", "重启前已接收的问题")
    missing_case = str(uuid.uuid4())
    store.bind_case(conversation, missing_case)
    store.runtime_epoch = "restarted-epoch"
    store.recover()
    with pytest.raises(ApplicationPortError) as missing:
        stack.application.get_case(missing_case)
    assert missing.value.error.code is ErrorCode.CASE_NOT_FOUND
    operational = OperationalState()
    service.application = replace(stack.application, operational_state=operational)
    if unrelated_runtime_fault:
        operational.record(case_id=None, job_id=None, phase="RESULT_DELIVERY", error_code=ErrorCode.STATE_WRITE_FAILED)
    prefix = f"/api/v1/agent/conversations/{conversation}"
    first = client.get(prefix)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["status"] == "INTERRUPTED"
    assert client.get(prefix).json()["data"]["failure"] == first.json()["data"]["failure"]
    replay = client.get(prefix + "/events")
    assert replay.status_code == 200, replay.text
    frames = [json.loads(frame[6:]) for frame in replay.content.split(b"\n\n") if frame.startswith(b"data: ")]
    assert frames[-2]["type"] == "conversation.interrupted"
    assert frames[-1]["type"] == "conversation.completed"
    assert engine.calls == [] and not service.run_once(conversation)


@pytest.mark.parametrize("review_verdict", ["REJECT", "NEED_MORE_EVIDENCE"])
def test_non_pass_review_publishes_only_inconclusive_report_and_audit(website, monkeypatch, review_verdict):
    stack, store, _, service, client = website
    backend = stack.runtime._diagnose_backend
    execute = backend.execute

    def reviewed(**kwargs):
        result = execute(**kwargs)
        if kwargs.get("backend_phase") == "METHODS_REVIEWER":
            path = kwargs["workspace_root"] / "output" / "method-review.draft.json"
            draft = json.loads(path.read_bytes())
            draft["verdict"] = review_verdict
            for finding in draft["findings"]:
                finding.update(verdict=review_verdict, reason="现有证据不足以接受该结论。")
            path.write_bytes(canonical_json_bytes(draft))
        return result

    monkeypatch.setattr(backend, "execute", reviewed)
    conversation, prefix, _ = _converse_to_waiting(website)
    service.send_message(conversation, "answer", "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items()))
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "UNRESOLVED", view
    assert view.status == "COMPLETED"
    case = stack.application.get_case(view.case_id).case_view
    assert case.final_result is None and case.unresolved_result is not None
    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    assert sorted(item["kind"] for item in artifacts) == ["AUDIT_BUNDLE", "USER_RESULT"]
    report = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    downloaded = client.get(report["download_url"])
    assert downloaded.json()["status"] == "INCONCLUSIVE"
    assert len(downloaded.content) == report["size"]
    assert hashlib.sha256(downloaded.content).hexdigest() == report["sha256"]
    events = store.list_events(conversation, limit=500)
    reports = [item for item in events if item.type == "result.available"]
    assert len(reports) == 1 and reports[0].data["result_field"] == "unresolved_result"
    assert all(item["kind"] != "USER_RESULT_ARCHIVE" for event in events for item in event.data.get("artifacts", []))
    response = client.get(prefix + "/events")
    assert response.status_code == 200 and '"type":"conversation.completed"' in response.text


def test_invalid_upload_can_retry_without_adopting_different_bytes(website):
    _, store, _, service, client = website
    conversation = service.create_conversation("uploads").conversation_id
    payload = b"synthetic archive bytes"
    attachment = service.prepare_attachment(conversation, "upload", "logs.zip", "application/zip",
        len(payload), hashlib.sha256(payload).hexdigest())
    with pytest.raises(Exception, match="SHA-256"):
        service.upload_attachment(attachment.attachment_id, attachment.attachment_id, attachment.content_type,
            attachment.size, attachment.sha256, io.BytesIO(b"x" * len(payload)))
    assert store.get_attachment(attachment.attachment_id).status != "READY"
    result = service.upload_attachment(attachment.attachment_id, attachment.attachment_id, attachment.content_type,
        attachment.size, attachment.sha256, io.BytesIO(payload))
    assert result.status == "READY"
    assert "storage_path" not in json.dumps(service.get_conversation(conversation).model_dump())


def test_long_multi_round_history_keeps_complete_first_message_as_case_text(website):
    stack, _, engine, service, client = website
    conversation = service.create_conversation("long-history").conversation_id
    first = "RPC timeout\n" + "x" * 33000
    second = "补充说明\n" + "y" * 33000
    service.send_message(conversation, "first", first)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    assert engine.calls == []
    service.send_message(conversation, "second", second)
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.case_status == "WAITING_INPUT", view
    assert [item.text for item in view.messages] == [first, second]
    assert len((first + second).encode("utf-8")) > 65_536
    assert len(engine.calls) == 1
    assert len(build_intake_prompt(engine.calls[0]).encode("utf-8")) <= INTAKE_RESOURCE_LIMITS.context_bytes
    case = stack.application.get_case(view.case_id).case_view
    assert case.raw_problem_text == first
    assert case.problem_spec.statement == case.problem_spec.actual_behavior == first


def test_rejected_case_command_never_claims_the_message_was_adopted(website, monkeypatch):
    stack, store, _, service, _ = website
    conversation = service.create_conversation("failed-command").conversation_id
    service.send_message(conversation, "create", "RPC timeout RPC completes payment-to-inventory RPC synthetic-order-0001")
    execute = stack.application.execute

    def rejected(command):
        if isinstance(command, CreateCase):
            raise RuntimeError("private/path SECRET synthetic failure")
        return execute(command)

    monkeypatch.setattr(type(stack.application), "execute", lambda self, command: rejected(command))
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.status == "FAILED"
    assert view.case_id is None
    assert view.messages[0].status == "UNUSED"
    assert view.failure.code == "AGENT_EXECUTION_FAILED"
    assert {item["field"]: item["actual"] for item in view.failure.details}["phase"] == "CREATE_CASE"
    public = json.dumps([event.model_dump() for event in store.list_events(conversation)])
    assert "private/path" not in public and "SECRET" not in public
    assert "private/path" not in view.model_dump_json() and "SECRET" not in view.model_dump_json()


def test_invalid_intake_ends_once_with_durable_safe_snapshot_diagnostics(website, monkeypatch):
    _, store, engine, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    calls = []

    def invalid(request):
        calls.append(request)
        raise IntakeError("INTAKE_OUTPUT_INVALID", "SECRET /srv/private raw model output")

    monkeypatch.setattr(engine, "intake", invalid)
    message = {"request_id": "invalid-answer", "text": "现场补充信息"}
    receipt = _post(client, prefix + "/messages", message)
    assert service.run_once(conversation)
    first = client.get(prefix).json()["data"]
    failure = first["failure"]
    assert first["status"] == "FAILED" and failure["retryable"] is False
    assert failure["code"] == "INTAKE_OUTPUT_INVALID"
    details = {item["field"]: item["actual"] for item in failure["details"]}
    assert details["phase"] == "INTAKE"
    uuid.UUID(details["diagnostic_id"])
    assert len(calls) == 1 and not service.run_once(conversation)
    assert _post(client, prefix + "/messages", message) == receipt
    assert client.post(prefix + "/messages", json={"request_id": "again", "text": "更正"}).status_code == 409
    store.runtime_epoch = "later-runtime"
    store.recover()
    assert client.get(prefix).json()["data"]["failure"] == failure
    events = client.get(prefix + "/events")
    frames = [json.loads(frame[6:]) for frame in events.content.split(b"\n\n") if frame.startswith(b"data: ")]
    failed = next(event for event in frames if event["type"] == "agent.failed")
    assert set(failed["data"]) == {"code", "message"}
    assert frames[-1]["type"] == "conversation.completed"
    assert "SECRET" not in json.dumps(first) + events.text
    assert "/srv/private" not in json.dumps(first) + events.text


def test_typed_command_error_keeps_only_safe_field_location_in_failure(website, monkeypatch):
    stack, store, _, service, client = website
    conversation = service.create_conversation("typed-location").conversation_id
    service.send_message(conversation, "message", "待定位的问题")

    def rejected(self, command):
        raise ApplicationPortError(ApplicationError(code=ErrorCode.VALIDATION_ERROR, message="SECRET /srv/private error",
            retryable=False, details=[ApplicationErrorDetail(field=field, actual="SECRET actual", expected="SECRET expected",
                resource_type=None, resource_id=None, resource_ref=None, limit=None, observed=None)
                for field in ("input_values[2]", "inputs.request_id", "input_values[2]", "raw_output", "inputs./srv/private")]))

    monkeypatch.setattr(type(stack.application), "execute", rejected)
    assert service.run_once(conversation)
    prefix = f"/api/v1/agent/conversations/{conversation}"
    response = client.get(prefix)
    failure = response.json()["data"]["failure"]
    assert failure["code"] == "VALIDATION_ERROR" and failure["retryable"] is False
    assert [item["actual"] for item in failure["details"] if item["field"] == "location"] == ["input_values[2]", "inputs.request_id"]
    assert next(item["actual"] for item in failure["details"] if item["field"] == "phase") == "CREATE_CASE"
    assert "SECRET" not in response.text and "/srv/private" not in response.text
    assert client.get(prefix).json()["data"]["failure"] == failure
    assert set(next(event.data for event in store.list_events(conversation) if event.type == "agent.failed")) == {"code", "message"}


def test_agent_admission_rejects_before_acceptance_and_does_not_finalize_unknown_delivery(website):
    stack, store, engine, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    operational = OperationalState()
    operational.install_epoch(str(uuid.uuid4()))
    service.application = replace(stack.application, operational_state=operational)
    before = store.get_conversation(conversation)
    operational.record(case_id=before.case_id, job_id=before.job_id, phase="RESULT_DELIVERY",
        error_code=ErrorCode.STATE_WRITE_FAILED)
    response = client.post(prefix + "/messages", json={"request_id": "after-fault", "text": "继续"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "DISPATCH_REJECTED"
    assert store.get_conversation(conversation) == before
    assert not service.run_once(conversation) and engine.calls == []
    response = client.get(prefix)
    assert response.status_code == 503
    assert any(item["field"] == "persistence" and item["actual"] == "UNKNOWN" for item in response.json()["error"]["details"])
    response = client.get(prefix + "/events")
    assert response.status_code == 503
    assert not any(event.type == "conversation.completed" for event in store.list_events(conversation, limit=500))


def test_accepted_message_before_case_returns_safe_pause_when_another_task_halts_dispatch(website):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("accepted-before-fatal").conversation_id
    empty = service.create_conversation("empty-history").conversation_id
    closed = service.create_conversation("closed-history").conversation_id
    store.fail_conversation(closed)
    prefix = f"/api/v1/agent/conversations/{conversation}"
    receipt = _post(client, prefix + "/messages", {"request_id": "accepted", "text": "已经接收但尚未创建任务的问题"})
    before = store.get_conversation(conversation)
    events = store.list_events(conversation)
    assert before.case_id is None and before.messages[0].status == "QUEUED"
    operational = OperationalState()
    other_case, other_job = str(uuid.uuid4()), str(uuid.uuid4())
    operational.record(case_id=other_case, job_id=other_job, phase="RESULT_DELIVERY", error_code=ErrorCode.STATE_WRITE_FAILED)
    service.application = replace(stack.application, operational_state=operational)
    assert not service.run_once(conversation)
    for suffix in ("", "/events", ""):
        response = client.get(prefix + suffix)
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "DISPATCH_REJECTED"
        assert error["details"] == [{"field": "phase", "actual": "DISPATCH_PAUSED"},
            {"field": "persistence", "actual": "UNKNOWN"}]
        assert other_case not in response.text and other_job not in response.text
    assert client.get(f"/api/v1/agent/conversations/{empty}").status_code == 200
    assert client.get(f"/api/v1/agent/conversations/{closed}").status_code == 200
    assert store.get_conversation(conversation) == before and store.get_conversation(conversation).failure is None
    assert store.list_events(conversation) == events
    replayed = store.submit_message(conversation, "accepted", "已经接收但尚未创建任务的问题")
    assert replayed.model_dump(mode="json") == receipt
    assert engine.calls == []


def test_archive_status_double_failure_keeps_json_readable_and_snapshot_uncertainty_ephemeral(website, monkeypatch):
    stack, store, _, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    service.send_message(conversation, "answer", "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items()))
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    before = store.get_conversation(conversation)
    assert before.case_status == "RESOLVED" and before.archive_status == "PENDING"
    operational = OperationalState()
    operational.install_epoch(str(uuid.uuid4()))
    service.application = replace(stack.application, operational_state=operational)
    stack.application.queries._operational = operational
    stack.archive.operational_state = operational
    attempted = []

    def fail_status(case_id, status, artifact=None):
        attempted.append(status)
        raise RuntimeError("SECRET /srv/private unavailable status persistence")

    monkeypatch.setattr(stack.archive, "_set_status", fail_status)
    assert stack.archive.run_once()
    assert attempted == ["READY", "FAILED"]
    assert operational.faults[-1].phase == "ARCHIVE_STATUS_COMMIT"
    for _ in range(2):
        response = client.get(prefix)
        assert response.status_code == 200, response.text
        current = response.json()["data"]
        assert current["status"] == "RUNNING" and current["archive_status"] == "PENDING"
        details = {item["field"]: item["actual"] for item in current["failure"]["details"]}
        assert details["phase"] == "ARCHIVE_STATUS_COMMIT" and details["persistence"] == "UNKNOWN"
        assert "SECRET" not in response.text
    assert store.get_conversation(conversation).failure is None
    case_response = client.get(f"/api/v1/cases/{before.case_id}")
    assert case_response.status_code == 200, case_response.text
    artifacts = client.get(f"/api/v1/cases/{before.case_id}/artifacts").json()["data"]["artifacts"]
    report = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    downloaded = client.get(report["download_url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == report["sha256"]
    assert downloaded.json()["status"] == "COMPLETED"
    assert service.list_events(conversation)["stream_closed"] is False
    assert not any(event.type == "conversation.completed" for event in store.list_events(conversation, limit=500))


def test_concurrent_message_is_persisted_and_waits_for_serial_intake(website, monkeypatch):
    _, _, engine, service, _ = website
    conversation, _, _ = _converse_to_waiting(website)
    entered, released = threading.Event(), threading.Event()
    intake = engine.intake

    def blocked(request):
        entered.set()
        assert released.wait(10)
        return intake(request)

    monkeypatch.setattr(engine, "intake", blocked)
    service.send_message(conversation, "one", "暂时没有更多信息。")
    worker = threading.Thread(target=lambda: service.run_once(conversation))
    worker.start()
    try:
        assert entered.wait(5)
        receipt = service.send_message(conversation, "two", "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items()))
        assert receipt.status == "ACCEPTED"
        assert not service.run_once(conversation)
        assert [item.status for item in service.get_conversation(conversation).messages] == ["APPLIED", "PROCESSING", "QUEUED"]
    finally:
        released.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(engine.calls) == 1
    assert service.run_once(conversation)
    assert len(engine.calls) == 2
