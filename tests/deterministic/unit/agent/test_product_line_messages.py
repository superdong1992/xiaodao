"""Product-line context survives queued messages, retries and log handoffs."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from problem_locator.agent.models import AgentMessage, AgentStoreError, SendMessageRequest
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import (
    ApplicationError, ApplicationPortError, DiagnosisMode, ErrorCode,
    MarkInitialLogArchiveExpected, RestartGenericDiagnosis,
)
from tests.deterministic.unit.agent.test_generic_log_messages import _ready, generic_web
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _open


@pytest.fixture
def store(tmp_path):
    repository = _open(tmp_path)
    yield AgentStore(repository, runtime_epoch="product-line")
    repository.close()


@pytest.mark.parametrize("product_line", ["\ud800", "\udfff", "交换\ud800机"])
def test_product_line_request_rejects_invalid_unicode_before_persistence(store, product_line):
    with pytest.raises(ValidationError) as error:
        SendMessageRequest(request_id="invalid-unicode", text="设备重启", product_line=product_line)
    assert error.value.errors()[0]["loc"] == ("product_line",)
    cid = store.create_conversation("create").conversation_id
    with pytest.raises(ValidationError):
        store.submit_message(cid, "invalid-unicode", "设备重启", product_line=product_line)
    assert store.get_conversation(cid).messages == []
    assert store.list_events(cid) == []


def test_legacy_message_json_and_request_hash_remain_unchanged(store):
    cid = store.create_conversation("create").conversation_id
    legacy_request = {"request_id": "legacy", "text": "设备重启", "attachment_ids": []}
    legacy_fingerprint = hashlib.sha256(json.dumps(legacy_request, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    receipt = store.submit_message(cid, **legacy_request)
    with store.repository.database_read() as db:
        fingerprint, raw = db.execute("SELECT fingerprint,body FROM agent_messages WHERE message_id=?",
            (receipt.message_id,)).fetchone()
    assert fingerprint == legacy_fingerprint
    assert "product_line" not in json.loads(raw)
    loaded = AgentMessage.model_validate_json(raw)
    assert loaded.product_line is None
    assert "product_line" not in loaded.model_dump()
    request = SendMessageRequest(**legacy_request, product_line=None)
    assert request.model_dump() == legacy_request
    assert store.message_request(cid, request) == (receipt, None)
    assert store.submit_message(cid, **legacy_request, product_line=None) == receipt


def test_product_line_is_persisted_replayed_and_bound_to_request_content(store):
    cid = store.create_conversation("create").conversation_id
    request = dict(request_id="first", text="设备重启", product_line="交换机")
    receipt = store.submit_message(cid, **request)
    assert store.submit_message(cid, **request) == receipt
    with store.repository.database_read() as db:
        raw = db.execute("SELECT body FROM agent_messages WHERE message_id=?", (receipt.message_id,)).fetchone()[0]
    assert json.loads(raw)["product_line"] == "交换机"
    reopened = _open(store.repository.layout.data_root)
    try:
        restored = AgentStore(reopened, runtime_epoch="product-line")
        assert restored.get_conversation(cid).messages[0].product_line == "交换机"
        assert restored.list_events(cid)[0].data["product_line"] == "交换机"
        assert restored.submit_message(cid, **request) == receipt
        for product_line in (None, "路由器"):
            with pytest.raises(AgentStoreError, match="内容不能更改"):
                restored.submit_message(cid, **{**request, "product_line": product_line})
    finally:
        reopened.close()


@pytest.mark.parametrize("product_line", [None, "交换机"])
def test_queued_problem_passes_product_line_to_create_case(store, monkeypatch, product_line):
    cid = store.create_conversation("create").conversation_id
    service = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    service.send_message(cid, "question", "设备重启", product_line=product_line)
    commands = []
    monkeypatch.setattr(service, "_execute_command", lambda cid, label, command, **kwargs: commands.append(command))
    assert service.run_once(cid)
    assert len(commands) == 1
    assert commands[0].raw_problem_text == "设备重启"
    assert commands[0].product_line == product_line
    assert commands[0].initial_user_facts == []


def test_case_product_line_comes_from_problem_message_not_earlier_attachment(store, monkeypatch):
    cid = store.create_conversation("create").conversation_id
    attachment = _ready(store, cid)
    service = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    service.send_message(cid, "attachment-only", attachment_ids=[attachment], product_line="附件消息中的产品线")
    assert service.run_once(cid)
    service.send_message(cid, "problem", "设备重启", product_line="问题所属产品线")
    commands = []
    monkeypatch.setattr(service, "_execute_command", lambda cid, label, command, **kwargs: commands.append(command))
    assert service.run_once(cid)
    assert len(commands) == 1
    assert commands[0].product_line == "问题所属产品线"


def test_new_run_uses_its_own_product_line_and_old_replay_keeps_its_run(store):
    cid = store.create_conversation("create").conversation_id
    first = store.submit_message(cid, "first", "第一轮问题", product_line="交换机")
    store.fail_conversation(cid)
    second = store.submit_message(cid, "second", "第二轮问题")
    assert second.run_id != first.run_id
    assert store.get_conversation(cid).messages[0].product_line is None
    store.fail_conversation(cid)
    third = store.submit_message(cid, "third", "第三轮问题", product_line="路由器")
    assert third.run_id != second.run_id
    assert store.get_conversation(cid).messages[0].product_line == "路由器"
    assert store.submit_message(cid, "first", "第一轮问题", product_line="交换机") == first
    assert store.get_conversation(cid).run_id == third.run_id


@pytest.mark.parametrize("product_line", [None, "交换机"])
def test_frozen_restart_recovers_product_line_and_replays_original_receipt(generic_web, product_line):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    request = SendMessageRequest(request_id="pending-product", text="补充日志", attachment_ids=[attachment],
        product_line=product_line)
    command = RestartGenericDiagnosis(idempotency_key="restart-product", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64,
        target_run_id=run_id)
    pending = store.pending_generic_restarts()[0]
    assert pending["message"].get("product_line") == product_line
    app.aggregate.case.product_line = "原始产品线"
    receipt = service._dispatch_generic_restart(pending)
    assert store.get_conversation(cid).messages[-1].product_line == product_line
    assert service.send_message(cid, request.request_id, request.text, request.attachment_ids,
        product_line=product_line, target_run_id=run_id) == receipt
    assert app.aggregate.case.product_line == "原始产品线"
    assert len(app.calls) == 1


def test_route_marker_retarget_keeps_product_line_in_recovered_generic_restart(generic_web, monkeypatch):
    store, service, app, cid, run_id = generic_web
    request = SendMessageRequest(request_id="retarget-product", text="补充日志", attachment_ids=[_ready(store, cid)],
        product_line="交换机")
    command = MarkInitialLogArchiveExpected(idempotency_key="retarget-product", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    execute = app.execute

    def route_finished(command):
        if isinstance(command, MarkInitialLogArchiveExpected):
            raise ApplicationPortError(ApplicationError(code=ErrorCode.INVALID_CASE_STATE,
                message="路由已完成。", retryable=False, details=[]))
        return execute(command)

    monkeypatch.setattr(app, "execute", route_finished)
    receipt = service._dispatch_generic_restart(store.pending_generic_restarts()[0])
    assert store.get_conversation(cid).messages[-1].product_line == "交换机"
    assert service.send_message(cid, request.request_id, request.text, request.attachment_ids,
        product_line="交换机") == receipt
    assert len(app.calls) == 1


def test_route_fallback_keeps_product_line_without_changing_case(generic_web, monkeypatch):
    store, service, app, cid, run_id = generic_web
    app.aggregate.case.selected_skill_ref = object()
    app.aggregate.case.product_line = "原始产品线"
    app.aggregate.jobs[JOB_ID].diagnosis_mode = DiagnosisMode.SPECIALIZED
    app.aggregate.jobs[JOB_ID].job_type = SimpleNamespace(value="DIAGNOSE")
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), app.aggregate)
    attachment = _ready(store, cid)
    request = SendMessageRequest(request_id="routed-product", text="补充日志", attachment_ids=[attachment],
        product_line="补充消息中的产品线")
    command = MarkInitialLogArchiveExpected(idempotency_key="route-product", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)

    def reject_marker(command):
        raise ApplicationPortError(ApplicationError(code=ErrorCode.INVALID_CASE_STATE,
            message="路由已完成。", retryable=False, details=[]))

    monkeypatch.setattr(app, "execute", reject_marker)
    receipt = service._dispatch_generic_restart(store.pending_generic_restarts()[0])
    assert store.get_conversation(cid).messages[-1].product_line == request.product_line
    assert service.send_message(cid, request.request_id, request.text, request.attachment_ids,
        product_line=request.product_line) == receipt
    assert app.aggregate.case.product_line == "原始产品线"
    assert store.pending_generic_restarts() == []
