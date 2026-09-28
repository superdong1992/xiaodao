"""Guarded website supplements never accidentally create a new diagnosis."""
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError, SendMessageRequest
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import MarkInitialLogArchiveExpected
from tests.deterministic.unit.storage.test_state_repository import _open
from tests.deterministic.unit.interfaces.test_agent_http import OWNER, run_request
from tests.deterministic.unit.agent.test_generic_log_messages import generic_web, _ready


@pytest.fixture
def stack(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository)
    service = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), repository.layout)
    created = store.create_conversation("create", owner_key=OWNER)
    yield store, service, created
    repository.close()


def _close(store, created):
    store.request_stop(created.conversation_id, "stop", created.run_id)
    store.finish_stop(created.conversation_id, created.run_id)


def test_guard_rejects_closed_run_but_old_client_keeps_new_run_behavior(stack):
    store, service, created = stack
    _close(store, created)
    events = store.list_events(created.conversation_id)
    path = f"/api/v1/agent/conversations/{created.conversation_id}/messages"
    response = run_request(service, "POST", path, json={"request_id": "guarded", "text": "进一步解释", "target_run_id": created.run_id})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_RUN_CHANGED"
    assert store.list_events(created.conversation_id) == events
    assert store.get_status(created.conversation_id).run_id == created.run_id
    response = run_request(service, "POST", path, json={"request_id": "legacy", "text": "独立诊断"})
    assert response.status_code == 200
    assert response.json()["data"]["run_id"] != created.run_id


def test_target_is_part_of_idempotency_and_retry_after_completion_is_stable(stack):
    store, service, created = stack
    kwargs = dict(conversation_id=created.conversation_id, request_id="supplement", text="补充文字",
                  target_run_id=created.run_id, owner_key=OWNER)
    original = service.send_message(**kwargs)
    _close(store, created)
    assert service.send_message(**kwargs) == original
    with pytest.raises(AgentStoreError) as error:
        service.send_message(**{**kwargs, "target_run_id": None})
    assert error.value.code == "AGENT_IDEMPOTENCY_CONFLICT"


def test_completion_between_service_read_and_store_write_is_fenced(stack, monkeypatch):
    store, service, created = stack
    original = store.submit_message
    def racing_submit(*args, **kwargs):
        _close(store, created)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "submit_message", racing_submit)
    with pytest.raises(AgentStoreError) as error:
        service.send_message(created.conversation_id, "race", "仍是原轮补充",
            target_run_id=created.run_id, owner_key=OWNER)
    assert error.value.code == "AGENT_RUN_CHANGED"
    assert store.get_status(created.conversation_id).run_id == created.run_id
    assert not store.get_conversation(created.conversation_id).messages


def test_guarded_generic_log_restart_replays_original_receipt(generic_web):
    store, service, app, cid, rid = generic_web
    attachment = _ready(store, cid)
    original = service.send_message(cid, "guarded-logs", "这次补充日志", [attachment],
                                    owner_key=OWNER, target_run_id=rid)
    assert service.send_message(cid, "guarded-logs", "这次补充日志", [attachment],
                                owner_key=OWNER, target_run_id=rid) == original
    assert len(app.calls) == 1
    assert original.run_id == rid


def test_report_completion_before_log_restart_freeze_returns_target_conflict(generic_web, monkeypatch):
    store, service, app, cid, rid = generic_web
    attachment = _ready(store, cid)
    freeze = store.freeze_generic_restart
    def finish_then_freeze(*args, **kwargs):
        with store.repository.database_transaction() as db:
            body = store._load(db, cid)
            body.update(status="COMPLETED", report_available=True)
            store._save(db, body)
        return freeze(*args, **kwargs)
    monkeypatch.setattr(store, "freeze_generic_restart", finish_then_freeze)
    with pytest.raises(AgentStoreError) as error:
        service.send_message(cid, "racing-logs", "新日志", [attachment], owner_key=OWNER, target_run_id=rid)
    assert error.value.code == "AGENT_RUN_CHANGED"
    assert not app.calls and not store.pending_generic_restarts()
    assert store.get_run(cid)["run_id"] == rid


def test_guarded_route_fallback_returns_target_conflict_when_original_run_ends(generic_web):
    store, _, app, cid, rid = generic_web
    attachment = _ready(store, cid)
    request = SendMessageRequest(request_id="route-logs", text="补充日志", attachment_ids=[attachment])
    command = MarkInitialLogArchiveExpected(idempotency_key="route-logs", case_id=app.aggregate.case.case_id,
        expected_case_revision=1, source_job_id=app.aggregate.case.active_job_id)
    store.freeze_generic_restart(cid, request, command, run_id=rid, archive_sha256="a" * 64, target_run_id=rid)
    store.request_stop(cid, "stop", rid)
    store.finish_stop(cid, rid)
    with pytest.raises(AgentStoreError) as error:
        store.submit_message(cid, request.request_id, request.text, request.attachment_ids,
            target_run_id=rid, routed_request_key=command.idempotency_key)
    assert error.value.code == "AGENT_RUN_CHANGED"
    assert store.get_run(cid)["run_id"] == rid


def test_snapshot_schedule_failure_cannot_fail_accepted_diagnosis(stack):
    store, service, created = stack
    def fail(_cid):
        raise OSError("disk unavailable")
    service.followups = SimpleNamespace(schedule_observe=fail)
    receipt = service.send_message(created.conversation_id, "legacy", "旧网站诊断", owner_key=OWNER)
    assert receipt.status == "ACCEPTED"
    assert len(store.get_conversation(created.conversation_id).messages) == 1
