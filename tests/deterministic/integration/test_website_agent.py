"""Website raw conversation → grounded RPC report, using deterministic agents."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from problem_locator.agent.intake import IntakeDecision, IntakeValue
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import CreateCase, ReviewPolicy, canonical_json_bytes
from problem_locator.interfaces.http_app import create_http_app
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
        last = next(item for item in reversed(request.messages) if item.role == "USER")

        def value(name, text):
            return IntakeValue(name=name, value=text, source_message_id=last.message_id, source_quote=text)

        if request.frozen_problem_spec is not None:
            return IntakeDecision(action="SUBMIT_SUPPLEMENT", message="已收到补充信息。", problem_fields=[],
                user_facts=[value(item.name, PARAMETER_GROUP_A[item.name]) for item in request.requirements
                    if item.kind == "INPUT"])
        if "RPC completes" not in last.text:
            return IntakeDecision(action="NEED_CLARIFICATION", message="请补充预期表现、实际表现和定位范围。",
                problem_fields=[value("statement", "RPC timeout")], user_facts=[])
        return IntakeDecision(action="CREATE_CASE", message="问题信息已完整，开始定位。",
            problem_fields=[value(name, text) for name, text in {
                "statement": "RPC timeout", "expected_behavior": "RPC completes",
                "actual_behavior": "RPC timeout", "scope": "payment-to-inventory RPC",
            }.items()], user_facts=[value("order_id", "synthetic-order-0001")])


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


def _converse_to_waiting(website):
    stack, store, engine, service, client = website
    conversation = _post(client, "/api/v1/agent/conversations", {"request_id": "create:1"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{conversation}"
    data = ARCHIVE.read_bytes()
    prepared = _post(client, prefix + "/attachments", {"request_id": "logs:1", "name": "logs.zip",
        "content_type": "application/zip", "declared_size": len(data), "declared_sha256": hashlib.sha256(data).hexdigest()})
    upload = prepared["upload"]
    uploaded = client.put(upload["url"], content=data,
        headers={key: value for key, value in upload["required_headers"].items() if value is not None})
    assert uploaded.status_code == 200, uploaded.text
    attachment_id = prepared["attachment"]["attachment_id"]
    first = {"request_id": "message:1", "text": "RPC timeout", "attachment_ids": [attachment_id]}
    accepted = _post(client, prefix + "/messages", first)
    assert _post(client, prefix + "/messages", first) == accepted
    assert service.run_once(conversation)
    assert service.get_conversation(conversation).current_questions
    assert service.get_conversation(conversation).case_id is None
    _post(client, prefix + "/messages", {"request_id": "message:2", "text":
        "故障：RPC timeout\n预期：RPC completes\n实际：RPC timeout\n范围：payment-to-inventory RPC\n订单：synthetic-order-0001"})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = service.get_conversation(conversation)
    assert view.case_id
    assert view.case_status == "WAITING_INPUT", view
    assert len(engine.calls) == 2
    return conversation, prefix, attachment_id


@pytest.mark.parametrize("review", [False, True])
def test_raw_conversation_preupload_sse_and_final_report(website, review):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    conversation, prefix, attachment_id = _converse_to_waiting(website)
    message = {"request_id": "message:3",
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
    assert len(engine.calls) == 3
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
    assert "event: archive.updated" in response.text and '"status":"READY"' in response.text
    assert "event: conversation.completed" in response.text
    assert "event: result.available" not in response.text
    assert len(engine.calls) == 3


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
    assert response.status_code == 200 and "event: conversation.completed" in response.text


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


def test_long_multi_round_history_does_not_overflow_legacy_case_text(website):
    stack, _, engine, service, client = website
    conversation = service.create_conversation("long-history").conversation_id
    first = "RPC timeout\n" + "x" * 40000
    second = "RPC timeout RPC completes payment-to-inventory RPC synthetic-order-0001\n" + "y" * 40000
    service.send_message(conversation, "first", first)
    assert service.run_once(conversation)
    service.send_message(conversation, "second", second)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = service.get_conversation(conversation)
    assert view.case_status == "WAITING_INPUT", view
    assert [item.text for item in view.messages] == [first, second]
    assert len(engine.calls) == 2
    assert stack.application.get_case(view.case_id).case_view.raw_problem_text == "RPC timeout"


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
    public = json.dumps([event.model_dump() for event in store.list_events(conversation)])
    assert "private/path" not in public and "SECRET" not in public


def test_concurrent_message_is_persisted_and_waits_for_serial_intake(website, monkeypatch):
    _, _, engine, service, _ = website
    conversation = service.create_conversation("concurrent").conversation_id
    entered, released = threading.Event(), threading.Event()
    intake = engine.intake

    def blocked(request):
        entered.set()
        assert released.wait(10)
        return intake(request)

    monkeypatch.setattr(engine, "intake", blocked)
    service.send_message(conversation, "one", "RPC timeout")
    worker = threading.Thread(target=lambda: service.run_once(conversation))
    worker.start()
    try:
        assert entered.wait(5)
        receipt = service.send_message(conversation, "two", "RPC timeout RPC completes payment-to-inventory RPC synthetic-order-0001")
        assert receipt.status == "ACCEPTED"
        assert not service.run_once(conversation)
        assert [item.status for item in service.get_conversation(conversation).messages] == ["PROCESSING", "QUEUED"]
    finally:
        released.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(engine.calls) == 1
    assert service.run_once(conversation)
    assert len(engine.calls) == 2
