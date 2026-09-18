"""Real application journeys for persistent runs, cancellation and deletion."""
from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import replace

import pytest

from problem_locator.agent.cleanup import ConversationCleanupService
from problem_locator.agent import service as service_module
from problem_locator.agent.intake import IntakeError
from problem_locator.contracts import CreateCase, ErrorCode, ReviewPolicy
from problem_locator.operational import OperationalState
from problem_locator.storage.platform import PlatformFileSync, PlatformReplaceOperation
from problem_locator.storage.quarantine import QuarantineMover
from tests.deterministic.integration.test_website_agent import (
    OWNER_KEY, PARAMETER_GROUP_A, _converse_to_waiting, _post, _preupload, website,
)
from tests.deterministic.integration.test_agent_reports import _finish_diagnosis


def _stop(service, cid, request="stop"):
    run_id = service.store.get_run(cid)["run_id"]
    return service.stop_conversation(cid, request, run_id, owner_key=OWNER_KEY)


def _cleanup(website):
    stack, store, _, service, _ = website
    service.cleanup = ConversationCleanupService(store, stack.repository,
        QuarantineMover(stack.layout, stack.coordination_lock, PlatformFileSync(), PlatformReplaceOperation()),
        service.usage_guard, dispatcher=stack.scheduler, archive=stack.archive)
    return service.cleanup


def test_stop_queued_without_model_and_old_requests_cannot_cancel_or_recreate_next_run(website):
    _stack, store, engine, service, client = website
    created = service.create_conversation("queued", owner_key=OWNER_KEY)
    cid = created.conversation_id
    first = service.send_message(cid, "first", "排队中的问题")
    assert _stop(service, cid).status == "CANCELLING"
    assert service.control_once()
    stopped = service.get_conversation(cid)
    assert stopped.current_run.status == "CANCELLED" and stopped.failure is None
    second = service.send_message(cid, "second", "重新定位的新问题")
    assert second.run_id != first.run_id
    assert service.send_message(cid, "first", "排队中的问题") == first
    replay = service.stop_conversation(cid, "stop", first.run_id)
    assert replay.run_id == first.run_id and replay.status == "CANCELLED"
    current = service.get_conversation(cid, include=())
    assert current.current_run.run_id == second.run_id and current.current_run.status == "INTAKE"
    assert current.capabilities.can_stop and not current.capabilities.can_rediagnose
    assert engine.calls == []
    other = client.post(f"/api/v1/agent/conversations/{cid}/stop",
        json={"request_id": "stop", "run_id": second.run_id})
    assert other.status_code == 409


@pytest.mark.parametrize("phase", ["create", "intake"])
def test_stop_during_synchronous_call_waits_for_exit_and_discards_late_result(website, monkeypatch, phase):
    stack, store, engine, service, _ = website
    entered, release = threading.Event(), threading.Event()
    pending_errors = []
    original_log_event = service_module.log_event

    def record_control_error(event, **fields):
        if event == "agent.stop.pending":
            pending_errors.append(fields)
        return original_log_event(event, **fields)

    monkeypatch.setattr(service_module, "log_event", record_control_error)
    if phase == "create":
        cid = service.create_conversation("creating", owner_key=OWNER_KEY).conversation_id
        service.send_message(cid, "problem", "RPC timeout")
        original = type(stack.application).execute
        def blocked(app, command):
            if isinstance(command, CreateCase):
                entered.set()
                assert release.wait(10)
            return original(app, command)
        monkeypatch.setattr(type(stack.application), "execute", blocked)
    else:
        cid, _, _ = _converse_to_waiting(website)
        original = engine.intake
        def blocked(request):
            entered.set()
            assert release.wait(10)
            return original(request)
        monkeypatch.setattr(engine, "intake", blocked)
        service.send_message(cid, "facts", "\n".join(f"{k}={v}" for k, v in PARAMETER_GROUP_A.items()))
    worker = threading.Thread(target=lambda: service.run_once(cid))
    worker.start()
    try:
        assert entered.wait(5)
        assert _stop(service, cid).status == "CANCELLING"
        service.control_once()
        assert service.get_conversation(cid, include=()).current_run.status == "CANCELLING"
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()
    assert stack.scheduler.wait_until_idle(15)
    service.control_once()
    assert stack.scheduler.wait_until_idle(15)
    service.control_once()
    assert pending_errors == [], pending_errors
    view = service.get_conversation(cid)
    assert view.current_run.status == "CANCELLED" and view.failure is None
    assert view.report_state == "UNAVAILABLE"
    case = stack.repository.read_snapshot(view.case_id).cases[view.case_id].case
    assert case.status.value == "CANCELLED" and case.diagnosis_state.user_facts == []
    assert not any(event.type == "agent.failed" for event in store.list_events(cid))


@pytest.mark.parametrize("stage", ["ROUTING", "LOGPARSE", "DIAGNOSING", "REVIEWING"])
def test_stop_real_worker_stages_and_do_not_claim_cancelled_until_worker_returns(website, monkeypatch, stage):
    stack, store, _, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT
    entered, release = threading.Event(), threading.Event()
    announce = stack.runtime._public_progress
    def blocked(case_id, job_id, current):
        announce(case_id, job_id, current)
        if current == stage:
            entered.set()
            assert release.wait(15)
    monkeypatch.setattr(stack.runtime, "_public_progress", blocked)
    cid = _post(client, "/api/v1/agent/conversations", {"request_id": "phase"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{cid}"
    attachment = _preupload(client, prefix)
    service.send_message(cid, "first", "RPC timeout\n" + "\n".join(
        f"{key}={value}" for key, value in PARAMETER_GROUP_A.items()), [attachment])
    assert service.run_once(cid)
    try:
        if stage != "ROUTING":
            assert stack.scheduler.wait_until_idle(15)
            assert service.run_once(cid)
        assert entered.wait(10)
        assert _stop(service, cid).status == "CANCELLING"
        service.control_once()
        assert service.get_conversation(cid, include=()).status == "CANCELLING"
    finally:
        release.set()
    assert stack.scheduler.wait_until_idle(20)
    service.control_once()
    current = service.get_conversation(cid)
    assert current.current_run.status == "CANCELLED" and current.failure is None
    assert not any(event.type == "result.available" for event in store.list_events(cid))


def test_stop_accepted_while_admission_is_paused(website):
    stack, _, engine, service, _ = website
    cid = service.create_conversation("paused", owner_key=OWNER_KEY).conversation_id
    service.send_message(cid, "queued", "RPC timeout")
    operational = OperationalState()
    operational.record(case_id=None, job_id=None, phase="DISPATCH_PAUSED", error_code=ErrorCode.DISPATCH_REJECTED)
    service.application = replace(stack.application, operational_state=operational)
    assert _stop(service, cid).status == "CANCELLING"
    assert service.control_once()
    assert service.get_conversation(cid).status == "CANCELLED" and engine.calls == []


def test_report_completion_wins_stop_and_old_archive_cannot_overwrite_new_run(website):
    stack, store, engine, service, client = website
    cid, prefix = _finish_diagnosis(website)
    old = service.get_conversation(cid)
    assert old.report_state == "READY" and old.archive_status == "PENDING"
    assert _stop(service, cid).status == "ALREADY_FINISHED"
    receipt = service.send_message(cid, "new-diagnosis", "RPC timeout again", [old.attachments[0].attachment_id])
    assert service.get_conversation(cid, include=()).current_run.run_id == receipt.run_id
    assert stack.archive.run_once()
    current = service.get_conversation(cid)
    assert current.current_run.run_id == receipt.run_id and current.status == "INTAKE"
    assert current.messages[-1].status == "QUEUED" and current.result.report is None
    historical = service.get_conversation(cid, run_id=old.current_run.run_id)
    assert historical.current_run.run_id == receipt.run_id
    assert historical.result.report == old.result.report and historical.archive_status == "READY"
    response = client.get(prefix, params={"run_id": old.current_run.run_id})
    assert response.status_code == 200, response.text
    artifact = next(item for item in response.json()["data"]["artifacts"] if item["kind"] == "USER_RESULT")
    downloaded = client.get(artifact["download_url"])
    assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
    assert service.run_once(cid) and stack.scheduler.wait_until_idle(15)
    fresh = service.get_conversation(cid)
    assert fresh.case_id != old.case_id
    assert stack.application.get_case(fresh.case_id).case_view.user_facts == []
    service.send_message(cid, "new-facts", "\n".join(f"{k}={v}" for k, v in PARAMETER_GROUP_A.items()))
    assert service.run_once(cid) and stack.scheduler.wait_until_idle(20)
    latest = service.get_conversation(cid)
    assert latest.report_state == "READY"
    aid = old.attachments[0].attachment_id
    assert store.get_attachment_import(aid, old.current_run.run_id) != store.get_attachment_import(aid, receipt.run_id)
    assert len(engine.calls) == 2


def test_delete_revokes_all_reads_and_cleans_only_owned_case_after_readers_exit(website):
    stack, store, engine, service, client = website
    cid, prefix = _finish_diagnosis(website)
    before = service.get_conversation(cid)
    other = service.create_conversation("unrelated", owner_key=OWNER_KEY).conversation_id
    cleanup = _cleanup(website)
    with service.operation_lease(cid, owner_key=OWNER_KEY):
        response = client.delete(prefix)
        assert response.status_code in {200, 202}
        assert response.json()["data"]["status"] == "DELETING"
        assert not cleanup.run_once()
        assert stack.repository.read_snapshot(before.case_id).cases
        for url in [prefix, prefix + "/events", f"/api/v1/cases/{before.case_id}",
                f"/api/v1/artifacts/{before.artifacts[0].artifact_id}/content?case_id={before.case_id}"]:
            assert client.get(url).status_code == 404
        assert client.post(prefix + "/messages", json={"request_id": "late", "text": "不可复活"}).status_code == 404
        assert all(item.conversation_id != cid for item in service.list_conversations(OWNER_KEY).items)
    assert cleanup.run_once()
    assert client.delete(prefix).json()["data"]["status"] == "DELETED"
    assert not stack.repository.read_snapshot(before.case_id).cases
    assert not (stack.layout.resources / "cases" / before.case_id).exists()
    assert not (stack.layout.conversation_uploads / before.attachments[0].attachment_id).exists()
    assert service.get_conversation(other).conversation_id == other
    assert client.post("/api/v1/agent/conversations", json={"request_id": "create:1"}).status_code == 404
    assert len(engine.calls) == 1


def test_delete_waits_for_upload_and_cannot_publish_ready_after_revocation(website):
    stack, store, _, service, _ = website
    cid = service.create_conversation("upload-delete", owner_key=OWNER_KEY).conversation_id
    data = b"synthetic upload"
    record = service.prepare_attachment(cid, "upload", "log.zip", "application/zip", len(data), hashlib.sha256(data).hexdigest())
    entered, release = threading.Event(), threading.Event()
    class Blocking(io.BytesIO):
        def read(self, size=-1):
            entered.set()
            assert release.wait(10)
            return super().read(size)
    failures = []
    def upload():
        try:
            service.upload_attachment(record.attachment_id, record.attachment_id, record.content_type,
                record.size, record.sha256, Blocking(data))
        except Exception as error:
            failures.append(error)
    worker = threading.Thread(target=upload)
    worker.start()
    cleanup = _cleanup(website)
    try:
        assert entered.wait(5)
        service.delete_conversation(cid, owner_key=OWNER_KEY)
        service.control_once()
        assert not cleanup.run_once()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and failures
    service.control_once()
    assert service.delete_conversation(cid, owner_key=OWNER_KEY).status == "DELETED"
    assert not (stack.layout.conversation_uploads / record.attachment_id).exists()


def test_delete_drains_waiting_case_from_an_earlier_failed_intake_run(website, monkeypatch):
    stack, store, engine, service, client = website
    cid, prefix, _ = _converse_to_waiting(website)
    old_case = service.get_conversation(cid).case_id
    def invalid(_request):
        raise IntakeError("INTAKE_OUTPUT_INVALID", "synthetic invalid intake")
    monkeypatch.setattr(engine, "intake", invalid)
    service.send_message(cid, "invalid", "无法识别的补充")
    assert service.run_once(cid)
    assert service.get_conversation(cid).status == "FAILED"
    assert stack.repository.read_snapshot(old_case).cases[old_case].case.status.value == "WAITING_INPUT"
    service.send_message(cid, "new-round", "新一轮问题")
    _cleanup(website)
    assert client.delete(prefix).status_code in {200, 202}
    service.control_once()
    assert stack.scheduler.wait_until_idle(10)
    service.control_once()
    assert service.delete_conversation(cid, owner_key=OWNER_KEY).status == "DELETED"
    assert not stack.repository.read_snapshot(old_case).cases
