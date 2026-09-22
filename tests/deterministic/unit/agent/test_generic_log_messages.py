"""Web archive adoption, atomic restart acceptance and durable cancellation."""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError, SendMessageRequest
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import (
    ApplicationError, ApplicationPortError, CaseStatus, DiagnosisMode, ErrorCode,
    RestartGenericDiagnosis, MarkInitialLogArchiveExpected,
)
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _open


class _RestartApplication:
    """Keep projection on the real SQLite transaction, with a controlled core."""

    def __init__(self, store, aggregate):
        self.store, self.aggregate = store, aggregate
        self.receipts, self.calls = {}, []
        self.before_commit = None
        self.dispatch_pending = False

    def execute(self, command):
        assert isinstance(command, RestartGenericDiagnosis)
        self.calls.append(command)
        if command.idempotency_key not in self.receipts:
            if self.before_commit is not None:
                self.before_commit()
            candidate = copy.deepcopy(self.aggregate)
            candidate.jobs[command.source_job_id].status = "CANCELLED"
            new_id = str(uuid.uuid4())
            candidate.jobs[new_id] = SimpleNamespace(job_id=new_id, diagnosis_mode=DiagnosisMode.GENERIC,
                attachment_refs=[], generic_supplement_texts=[command.supplement_text] if command.supplement_text else [])
            candidate.case.active_job_id = new_id
            candidate.case.case_revision += 1
            record = SimpleNamespace(operation="RestartGenericDiagnosis", idempotency_key=command.idempotency_key,
                case_id=CASE_ID)
            state = SimpleNamespace(cases={CASE_ID: candidate}, idempotency_records={command.idempotency_key: record})
            with self.store.repository.database_transaction() as db:
                self.store._project_state(db, state)
            self.aggregate = candidate
            self.receipts[command.idempotency_key] = new_id
        return SimpleNamespace(dispatch_pending=self.dispatch_pending)


@pytest.fixture
def generic_web(tmp_path, monkeypatch):
    repository = _open(tmp_path)
    store = AgentStore(repository, runtime_epoch="generic-web")
    conversation = store.create_conversation("create")
    cid = conversation.conversation_id
    initial = store.submit_message(cid, "problem", "设备偶发重启")
    store.set_message_status(cid, initial.message_id, "APPLIED")
    store.finish_intake(cid, [initial.message_id])
    store.bind_case(cid, CASE_ID)
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=JOB_ID, case_revision=1,
        status=CaseStatus.RUNNING, archive_status="NOT_REQUIRED")
    aggregate = SimpleNamespace(case=case, jobs={JOB_ID: SimpleNamespace(job_id=JOB_ID,
        diagnosis_mode=DiagnosisMode.GENERIC, attachment_refs=[], generic_supplement_texts=[], status="RUNNING")},
        attachments={})
    app = _RestartApplication(store, aggregate)
    monkeypatch.setattr(repository, "read_snapshot", lambda *args, **kwargs: SimpleNamespace(cases={CASE_ID: app.aggregate}))
    with repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), aggregate)
    service = AgentConversationService(store, app, SimpleNamespace(intake=lambda request: pytest.fail("Generic不得调用INTAKE")),
        SimpleNamespace())
    yield store, service, app, cid, conversation.run_id
    repository.close()


def _ready(store, cid, key="archive", digest="a" * 64):
    record = store.reserve_attachment(cid, key, "logs.zip", "application/zip", 10, digest)
    store.set_attachment_status(record.attachment_id, "UPLOADING")
    return store.complete_attachment(record.attachment_id).attachment_id


def _accepted(store, cid):
    return [event for event in store.list_events(cid) if event.type == "message.accepted"]


def test_restart_keeps_case_and_run_and_accepts_only_with_core_projection(generic_web):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    before = len(_accepted(store, cid))
    # Durable intent is visible, but no success receipt/message yet.
    app.before_commit = lambda: _assert_unaccepted(store, cid, before)
    receipt = service.send_message(cid, "new-logs", "这是故障后的日志", [attachment])
    assert receipt.run_id == run_id
    view = store.get_conversation(cid)
    assert view.case_id == CASE_ID and view.run_id == run_id and view.job_id != JOB_ID
    assert view.messages[-1].status == "QUEUED"
    assert app.aggregate.jobs[JOB_ID].status == "CANCELLED"
    assert app.calls[0].supplement_text == "这是故障后的日志"
    accepted = _accepted(store, cid)
    assert len(accepted) == before + 1 and accepted[-1].job_id == view.job_id
    assert service.send_message(cid, "new-logs", "这是故障后的日志", [attachment]) == receipt
    assert len(app.calls) == 1
    with pytest.raises(AgentStoreError, match="内容不能更改"):
        service.send_message(cid, "new-logs", "修改请求", [attachment])
    with pytest.raises(AgentStoreError, match="无需重复提交"):
        service.send_message(cid, "same-logs", "", [attachment])
    assert len(app.calls) == 1


def _assert_unaccepted(store, cid, count):
    assert len(_accepted(store, cid)) == count
    assert store.pending_generic_restarts()[0]["status"] == "PENDING"


@pytest.mark.parametrize("selection", ["multiple", "not-ready", "foreign"])
def test_invalid_selection_does_not_cancel_or_accept(generic_web, selection):
    store, service, app, cid, _ = generic_web
    selected = [_ready(store, cid)]
    if selection == "multiple":
        selected.append(_ready(store, cid, "second", "b" * 64))
    elif selection == "not-ready":
        selected = [store.reserve_attachment(cid, "reserved", "logs.zip", "application/zip", 10, "b" * 64).attachment_id]
    else:
        other = store.create_conversation("other").conversation_id
        selected = [_ready(store, other)]
    with pytest.raises(AgentStoreError):
        service.send_message(cid, "bad-selection", "", selected)
    assert app.calls == [] and len(_accepted(store, cid)) == 1
    assert store.pending_generic_restarts() == []


def test_core_commit_rollback_never_exposes_message_accepted(generic_web, monkeypatch):
    store, service, app, cid, _ = generic_web
    attachment = _ready(store, cid)
    original = store._project_case
    def fail_after_accept(db, body, aggregate):
        original(db, body, aggregate)
        raise RuntimeError("commit-failed")
    monkeypatch.setattr(store, "_project_case", fail_after_accept)
    with pytest.raises(RuntimeError, match="commit-failed"):
        service.send_message(cid, "logs", "", [attachment])
    assert len(_accepted(store, cid)) == 1
    assert store.get_status(cid).job_id == JOB_ID
    assert store.pending_generic_restarts()[0]["status"] == "PENDING"
    assert app.aggregate.jobs[JOB_ID].status == "RUNNING"
    monkeypatch.setattr(store, "_project_case", original)
    service.send_message(cid, "logs", "", [attachment])
    assert len(_accepted(store, cid)) == 2 and len(app.receipts) == 1


def test_report_winning_restart_race_rejects_without_accepting(generic_web):
    store, service, app, cid, _ = generic_web
    attachment = _ready(store, cid)
    def result_wins():
        with store.repository.database_transaction() as db:
            body = store._load(db, cid)
            store._close(db, body, "COMPLETED")
            store._save(db, body)
        raise ApplicationPortError(ApplicationError(code=ErrorCode.INVALID_CASE_STATE,
            message="已结束", retryable=False, details=[]))
    app.before_commit = result_wins
    with pytest.raises(AgentStoreError, match="本次定位已结束"):
        service.send_message(cid, "logs", "", [attachment])
    assert len(_accepted(store, cid)) == 1 and app.receipts == {}
    assert store.pending_generic_restarts() == []
    with pytest.raises(AgentStoreError, match="本次定位已结束"):
        service.send_message(cid, "logs", "", [attachment])
    assert len(app.calls) == 1


@pytest.mark.parametrize("action", ["stop", "delete"])
def test_stop_or_delete_winning_commit_fences_restart_and_message(generic_web, action):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    app.before_commit = (lambda: service.stop_conversation(cid, "stop", run_id)) if action == "stop" else (
        lambda: service.delete_conversation(cid))
    with pytest.raises(AgentStoreError):
        service.send_message(cid, "logs", "", [attachment])
    assert app.receipts == {} and app.aggregate.jobs[JOB_ID].status == "RUNNING"
    # Retry only the durable restart controller, not the independent stop worker.
    pending = store.pending_generic_restarts()[0]
    with pytest.raises(AgentStoreError):
        service._dispatch_generic_restart(pending)
    assert store.pending_generic_restarts() == []
    with store.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_messages WHERE conversation_id=?", (cid,)).fetchone()[0] == 1


def test_cancel_failure_keeps_durable_request_and_replays_same_job(generic_web):
    store, service, app, cid, _ = generic_web
    attachment = _ready(store, cid)
    app.dispatch_pending = True
    receipt = service.send_message(cid, "logs", "补充说明", [attachment])
    job_id = store.get_status(cid).job_id
    assert store.pending_generic_restarts()[0]["status"] == "COMMITTED"
    assert service.send_message(cid, "logs", "补充说明", [attachment]) == receipt
    assert len(app.calls) == 1
    app.dispatch_pending = False
    assert service.control_once()
    assert store.pending_generic_restarts() == [] and store.get_status(cid).job_id == job_id
    assert len(app.calls) == 2 and len(app.receipts) == 1 and len(_accepted(store, cid)) == 2


def test_stopped_run_does_not_replay_pending_cancellation_as_new_work(generic_web):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    app.dispatch_pending = True
    service.send_message(cid, "logs", "", [attachment])
    service.stop_conversation(cid, "stop", run_id)
    pending = store.pending_generic_restarts()[0]
    with pytest.raises(AgentStoreError):
        service._dispatch_generic_restart(pending)
    assert len(app.calls) == 1 and store.pending_generic_restarts() == []


def test_stop_preserves_failed_old_worker_cancellation_until_signal_succeeds(generic_web):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    app.dispatch_pending = True
    service.send_message(cid, "logs", "", [attachment])
    service.stop_conversation(cid, "stop", run_id)
    signals = []
    def cancel(job_id):
        signals.append(job_id)
        if len(signals) == 1:
            raise RuntimeError("cancel-unavailable")
    service.dispatcher = SimpleNamespace(cancel=cancel)
    with pytest.raises(RuntimeError, match="cancel-unavailable"):
        service._dispatch_generic_restart(store.pending_generic_restarts()[0])
    assert len(store.pending_generic_restarts()) == 1
    with pytest.raises(AgentStoreError):
        service._dispatch_generic_restart(store.pending_generic_restarts()[0])
    assert signals == [JOB_ID, JOB_ID] and len(app.calls) == 1
    assert store.pending_generic_restarts() == []


def test_runtime_epoch_recovery_never_restarts_pending_model_work(generic_web):
    store, service, app, cid, _ = generic_web
    attachment = _ready(store, cid)
    app.dispatch_pending = True
    service.send_message(cid, "logs", "", [attachment])
    store.runtime_epoch = "recovered-epoch"
    store.recover()
    assert store.get_status(cid).status == "INTERRUPTED"
    with pytest.raises(AgentStoreError):
        service._dispatch_generic_restart(store.pending_generic_restarts()[0])
    assert len(app.calls) == 1 and store.pending_generic_restarts() == []


@pytest.mark.parametrize("cleanup", ["delete", "retention"])
def test_restart_payloads_follow_run_cleanup(generic_web, cleanup):
    from problem_locator.storage.history_retention import HistoryRetentionService
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    service.send_message(cid, "logs", "需随历史清理的描述", [attachment])
    if cleanup == "delete":
        store.request_delete(cid)
        store.finish_cleanup(cid)
    else:
        store.request_stop(cid, "stop", run_id)
        store.finish_stop(cid, run_id)
        store.submit_message(cid, "another-run", "下一轮问题")
        retention = HistoryRetentionService(
            store.repository, store, quarantine=None, usage_guard=None, clock=None,
        )
        with store.repository.database_transaction() as db:
            assert retention._prune_run(db, cid, run_id, "2999-01-01T00:00:00Z")
    with store.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_generic_restarts WHERE run_id=?", (run_id,)).fetchone()[0] == 0


def test_unaccepted_restart_preserves_selected_upload_during_history_expiry(generic_web):
    from problem_locator.storage.history_retention import HistoryRetentionService
    store, service, _, cid, run_id = generic_web
    attachment = _ready(store, cid)
    request = SendMessageRequest(request_id="pending-logs", attachment_ids=[attachment])
    command = RestartGenericDiagnosis(idempotency_key="pending-restart", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    history = HistoryRetentionService(store.repository, store, SimpleNamespace(), service.usage_guard, None)
    assert not history._expire_upload(attachment, cid, "2999-01-01T00:00:00Z")
    assert store.get_attachment(attachment).status == "READY"
    assert len(_accepted(store, cid)) == 1


@pytest.mark.parametrize("winner", ["stop", "delete", "result", "new_run"])
def test_specialized_route_message_conversion_obeys_terminal_and_run_fences(generic_web, winner):
    store, service, app, cid, run_id = generic_web
    app.aggregate.case.selected_skill_ref = object()
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), app.aggregate)
    attachment = _ready(store, cid)
    command = MarkInitialLogArchiveExpected(idempotency_key="route-marker", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    request = SendMessageRequest(request_id="racing-logs", text="追加的定位参数", attachment_ids=[attachment])
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    if winner in {"stop", "new_run"}:
        service.stop_conversation(cid, "stop", run_id)
        if winner == "new_run":
            store.finish_stop(cid, run_id)
            store.submit_message(cid, "new-problem", "下一轮问题")
    elif winner == "delete":
        service.delete_conversation(cid)
    else:
        with store.repository.database_transaction() as db:
            body = store._load(db, cid)
            store._close(db, body, "COMPLETED")
            store._save(db, body)
    with pytest.raises(AgentStoreError):
        store.submit_message(cid, request.request_id, request.text, request.attachment_ids,
            routed_request_key=command.idempotency_key)
    with store.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_messages WHERE conversation_id=? AND request_id=?",
            (cid, request.request_id)).fetchone()[0] == 0


def test_specialized_route_message_conversion_rolls_back_acceptance_and_replays_once(generic_web, monkeypatch):
    store, _, app, cid, run_id = generic_web
    app.aggregate.case.selected_skill_ref = object()
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), app.aggregate)
    attachment = _ready(store, cid)
    command = MarkInitialLogArchiveExpected(idempotency_key="route-marker", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    request = SendMessageRequest(request_id="racing-logs", text="追加的定位参数", attachment_ids=[attachment])
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    save = store._save
    def fail_save(*args):
        raise RuntimeError("save-failed")
    monkeypatch.setattr(store, "_save", fail_save)
    with pytest.raises(RuntimeError, match="save-failed"):
        store.submit_message(cid, request.request_id, request.text, request.attachment_ids,
            routed_request_key=command.idempotency_key)
    assert len(_accepted(store, cid)) == 1 and store.pending_generic_restarts()[0]["status"] == "PENDING"
    monkeypatch.setattr(store, "_save", save)
    receipt = store.submit_message(cid, request.request_id, request.text, request.attachment_ids,
        routed_request_key=command.idempotency_key)
    assert store.submit_message(cid, request.request_id, request.text, request.attachment_ids) == receipt
    assert len(_accepted(store, cid)) == 2 and store.pending_generic_restarts() == []


@pytest.mark.parametrize("projection", [False, None])
def test_routed_message_conversion_without_current_specialized_projection_stays_pending(generic_web, projection):
    store, _, _, cid, run_id = generic_web
    attachment = _ready(store, cid)
    command = MarkInitialLogArchiveExpected(idempotency_key="route-marker", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    request = SendMessageRequest(request_id="racing-logs", text="追加日志", attachment_ids=[attachment])
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    if projection is None:
        with store.repository.database_transaction() as db:
            body = store._load(db, cid)
            body.pop("case_has_selected_skill", None)
            store._save(db, body)
    with pytest.raises(AgentStoreError) as error:
        store.submit_message(cid, request.request_id, request.text, request.attachment_ids,
            routed_request_key=command.idempotency_key)
    assert error.value.code == "AGENT_ROUTE_CHANGED"
    assert len(_accepted(store, cid)) == 1
    assert store.pending_generic_restarts()[0]["status"] == "PENDING"


def test_missing_specialized_projection_rechecks_once_without_accepting(generic_web, monkeypatch):
    store, service, app, cid, run_id = generic_web
    attachment = _ready(store, cid)
    app.aggregate.case.selected_skill_ref = object()
    app.aggregate.jobs[JOB_ID].diagnosis_mode = DiagnosisMode.SPECIALIZED
    app.aggregate.jobs[JOB_ID].job_type = SimpleNamespace(value="DIAGNOSE")
    with store.repository.database_transaction() as db:
        body = store._load(db, cid)
        body.pop("case_has_selected_skill", None)
        store._save(db, body)
    request = SendMessageRequest(request_id="racing-logs", text="追加日志", attachment_ids=[attachment])
    command = MarkInitialLogArchiveExpected(idempotency_key="route-marker", case_id=CASE_ID,
        expected_case_revision=1, source_job_id=JOB_ID)
    store.freeze_generic_restart(cid, request, command, run_id=run_id, archive_sha256="a" * 64)
    calls = []
    def rejected_marker(command):
        calls.append(command)
        raise ApplicationPortError(ApplicationError(code=ErrorCode.INVALID_CASE_STATE,
            message="路由已完成。", retryable=False, details=[]))
    monkeypatch.setattr(app, "execute", rejected_marker)
    with pytest.raises(AgentStoreError) as error:
        service.send_message(cid, request.request_id, request.text, request.attachment_ids)
    assert error.value.code == "AGENT_RESTART_PENDING" and error.value.status_code == 503
    assert len(calls) == 2 and len(_accepted(store, cid)) == 1
    assert store.pending_generic_restarts()[0]["status"] == "PENDING"


@pytest.mark.parametrize("with_logs", [True, False])
def test_create_freezes_selected_archive_expectation_without_intake(tmp_path, monkeypatch, with_logs):
    repository = _open(tmp_path)
    try:
        store = AgentStore(repository)
        cid = store.create_conversation("first").conversation_id
        attachments = [_ready(store, cid)] if with_logs else []
        store.submit_message(cid, "question", "设备重启，没有时间进程参数", attachments)
        service = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        commands = []
        monkeypatch.setattr(service, "_execute_command", lambda cid, label, command, **kwargs: commands.append(command))
        assert service.run_once(cid)
        assert len(commands) == 1
        assert commands[0].initial_log_archive_expected is with_logs
        assert commands[0].initial_user_facts == []
    finally:
        repository.close()


def test_waiting_generic_imports_archive_and_preserves_text_without_intake(generic_web, monkeypatch):
    store, service, app, cid, _ = generic_web
    attachment = _ready(store, cid)
    store.submit_message(cid, "logs", "额外观察：重启前指示灯闪烁", [attachment])
    requirement = SimpleNamespace(requirement_id=str(uuid.uuid4()), name="log_archive", prompt="请上传日志。",
        kind=SimpleNamespace(value="ATTACHMENT"), status=SimpleNamespace(value="OPEN"),
        supplement_policy=SimpleNamespace(value="MISSING_ONLY"), requested_by_job_id=JOB_ID,
        constraints=SimpleNamespace(min_count=1, max_count=1, allowed_content_types=["application/zip"]))
    case = app.aggregate.case
    case.status = CaseStatus.WAITING_ATTACHMENT
    case.active_job_id = None
    case.diagnosis_state = SimpleNamespace(pending_requirements=[requirement])
    case.pending_requirements = [requirement]
    case.selected_skill_ref = None
    case.raw_problem_text = "设备偶发重启"
    # A new message may be a substring of an older observation; message IDs,
    # not textual overlap, decide whether the new observation was adopted.
    app.aggregate.jobs[JOB_ID].generic_supplement_texts = ["此前怀疑额外观察：重启前指示灯闪烁"]
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), app.aggregate)
    app.get_case = lambda case_id: SimpleNamespace(case_view=case)
    supplements = []
    def adopt(cid, label, command, **kwargs):
        supplements.append((command.inputs, command.attachment_ids, command.generic_supplement_text))
        store.set_message_status(cid, kwargs["message_id"], "APPLIED")
    monkeypatch.setattr(service.uploads, "import_into_case", lambda cid, case_id, attachment_id, *args, **kwargs: attachment_id)
    monkeypatch.setattr(service, "_execute_command", adopt)
    assert service.run_once(cid)
    assert supplements == [({}, [attachment], "额外观察：重启前指示灯闪烁")]
    assert store.get_conversation(cid).messages[-1].status == "APPLIED"
