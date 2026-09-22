"""Seven-day expiry must remove owned history without racing active work."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.agent.cleanup import ConversationCleanupService
from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.agent.usage import ConversationUsageGuard
from problem_locator.contracts import ApplicationPortError, Attachment, AttachmentStatus, Case, CaseStatus, ErrorCode, StateFile
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from problem_locator.storage.coordination import AttachmentUploadRegistry, StorageCoordinationLock
from problem_locator.storage.history_retention import HistoryRetentionService
from problem_locator.storage.quarantine import QuarantineMover
from problem_locator.storage.state_repository import CaseStateRepository
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FaultInjectingReplace
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _empty_mutation, _finish, _populate
from tests.deterministic.unit.storage.test_case_store_v10 import _create as _create_distinct, _cancel

OLD = "2026-07-31T00:01:00.000Z"
BOUNDARY = "2026-08-07T00:01:00.000Z"
NOW = "2026-08-08T00:01:00.000Z"


class MutableClock:
    def __init__(self, value=OLD):
        self.value = value
    def now(self):
        return self.value


@pytest.fixture
def stack(tmp_path):
    clock = MutableClock()
    coordination = StorageCoordinationLock()
    repository = CaseStateRepository(tmp_path, coordination, clock,
        DeterministicIdGenerator(seed="history-retention"), file_sync=FakeFileSync())
    store = AgentStore(repository, clock=clock)
    usage = ConversationUsageGuard()
    registry = AttachmentUploadRegistry()
    quarantine = QuarantineMover(repository.layout, coordination, FakeFileSync(), FaultInjectingReplace())
    history = HistoryRetentionService(repository, store, quarantine, usage, clock,
        attachment_registry=registry, coordination_lock=coordination)
    cleanup = ConversationCleanupService(store, repository, quarantine, usage)
    yield SimpleNamespace(root=tmp_path, repository=repository, store=store, clock=clock,
        usage=usage, registry=registry, quarantine=quarantine, history=history, cleanup=cleanup)
    repository.close()


def _file(root, relative, content=b"private bytes"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _completed(stack):
    _populate(stack.repository)
    before = stack.repository.read_snapshot(CASE_ID)
    case = before.cases[CASE_ID].case
    stack.repository.commit(before.generation, case.case_revision, _empty_mutation(upsert_case=Case.model_validate({
        **case.model_dump(), "status": CaseStatus.CANCELLED, "active_job_id": None,
        "updated_at": OLD, "case_revision": case.case_revision + 1})))
    return [_file(stack.root, relative) for relative in (
        f"resources/cases/{CASE_ID}/artifacts/{JOB_ID}/payload",
        f"jobs/{JOB_ID}/execution.json", f"tmp/workspaces/{JOB_ID}/model.txt",
        f"tmp/workspaces/{JOB_ID}.logparse-preprocess/input.log", f"tmp/proposals/{JOB_ID}/p-one/payload")]


def _close_run(store, cid, rid):
    store.request_stop(cid, "stop:" + rid, rid)
    store.finish_stop(cid, rid)


def _old_and_new_runs(stack):
    created = stack.store.create_conversation("create")
    original = stack.store.submit_message(created.conversation_id, "old", "旧问题")
    _close_run(stack.store, created.conversation_id, original.run_id)
    stack.clock.value = NOW
    newer = stack.store.submit_message(created.conversation_id, "new", "新问题")
    return created, original, newer


def _memory_history(stack, status="READY", *, source_created_at=OLD):
    memory = MemoryStore(stack.repository, stack.clock)
    stack.store.memory_store = memory
    owner = "a" * 64
    created = stack.store.create_conversation("memory-history", owner_key=owner)
    stack.store.submit_message(created.conversation_id, "question", "请求排队")
    stack.store.bind_case(created.conversation_id, CASE_ID)
    paths = _completed(stack)
    report = "# 诊断报告\n业务原文仅供后台提炼。\n"
    source = FeedbackSource(case_id=CASE_ID, source_job_id=JOB_ID, skill_name="generic-test",
        problem_text="请求排队，请检查。", report_markdown=report,
        report_sha256=hashlib.sha256(report.encode()).hexdigest())
    card = json.dumps({"problem_features": ["请求排队"], "applicability": ["服务繁忙"],
        "steps": ["核对队列长度"], "limitations": ["需要当前日志支持"]}, ensure_ascii=False)
    stack.clock.value = source_created_at
    memory.put_feedback(created.conversation_id, created.run_id, owner_key=owner,
        request_id="like", rating="LIKE", source=source)
    if status in {"RUNNING", "READY"}:
        task_id = memory.claim_task()["task_id"]
        if status == "READY":
            assert memory.finish_task(task_id, card)
    else:
        with stack.repository.database_read() as db:
            task_id = db.execute("SELECT task_id FROM memory_tasks").fetchone()[0]
    return SimpleNamespace(memory=memory, created=created, owner=owner, paths=paths,
        source=source, task_id=task_id, card=card)


def _memory_task(stack, task_id):
    with stack.repository.database_read() as db:
        return db.execute("SELECT status,problem_text,report_markdown,card_json,active "
                          "FROM memory_tasks WHERE task_id=?", (task_id,)).fetchone()


def test_natural_conversation_expiry_keeps_ready_memory_until_card_ttl(stack):
    history = _memory_history(stack)
    cards = history.memory.active_cards(history.source.skill_name)
    assert len(cards) == 1
    stack.clock.value = BOUNDARY
    assert stack.history.run_once()
    assert history.memory.active_cards(history.source.skill_name) == cards
    assert stack.cleanup.run_once()
    assert all(not path.exists() for path in history.paths)
    history.memory.prune()
    assert history.memory.active_cards(history.source.skill_name) == cards
    # The conversation tombstone expires too; the completed card keeps its own age.
    stack.clock.value = "2026-08-14T00:01:00.000Z"
    assert stack.history.run_once()
    with stack.repository.database_read() as db:
        assert db.execute("SELECT 1 FROM agent_conversations WHERE conversation_id=?",
                          (history.created.conversation_id,)).fetchone() is None
    assert history.memory.active_cards(history.source.skill_name) == cards
    stack.clock.value = "2026-10-29T00:00:59.999Z"
    assert history.memory.active_cards(history.source.skill_name) == cards
    stack.clock.value = "2026-10-29T00:01:00.000Z"
    assert history.memory.active_cards(history.source.skill_name) == []
    history.memory.prune()
    assert _memory_task(stack, history.task_id) is None


@pytest.mark.parametrize("status", ["PENDING", "RUNNING"])
def test_natural_conversation_expiry_erases_unfinished_memory_and_fences_late_result(stack, status):
    # The source report is seven days old while the extraction request is fresh.
    history = _memory_history(stack, status, source_created_at="2026-08-06T00:01:00.000Z")
    stack.clock.value = BOUNDARY
    assert stack.history.run_once()
    assert _memory_task(stack, history.task_id) == ("DELETED", None, None, None, 0)
    assert history.memory.claim_task() is None
    assert not history.memory.finish_task(history.task_id, history.card)
    assert stack.cleanup.run_once()
    history.memory.prune()
    assert _memory_task(stack, history.task_id) is None
    assert not history.memory.finish_task(history.task_id, history.card)


@pytest.mark.parametrize("cleanup_finished", [False, True])
def test_explicit_delete_after_natural_expiry_revokes_ready_memory(stack, cleanup_finished):
    history = _memory_history(stack)
    stack.clock.value = BOUNDARY
    assert stack.history.run_once()
    if cleanup_finished:
        assert stack.cleanup.run_once()
        history.memory.prune()
    with pytest.raises(AgentStoreError) as denied:
        stack.store.request_delete(history.created.conversation_id, owner_key="b" * 64)
    assert denied.value.status_code == 404
    assert len(history.memory.active_cards(history.source.skill_name)) == 1
    receipt = stack.store.request_delete(history.created.conversation_id, owner_key=history.owner)
    assert receipt.status == ("DELETED" if cleanup_finished else "DELETING")
    assert _memory_task(stack, history.task_id) == ("DELETED", None, None, None, 0)
    assert history.memory.active_cards(history.source.skill_name) == []
    assert not history.memory.finish_task(history.task_id, history.card)


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "READY"])
def test_natural_run_expiry_preserves_completed_memory_and_leaves_new_run_untouched(stack, status):
    history = _memory_history(stack, status, source_created_at="2026-08-06T00:01:00.000Z")
    stack.clock.value = NOW
    newer = stack.store.submit_message(history.created.conversation_id, "newer", "新的问题")
    new_source = replace(history.source, case_id="00000000-0000-0000-0000-000000000099")
    stack.store.bind_case(history.created.conversation_id, new_source.case_id)
    history.memory.put_feedback(history.created.conversation_id, newer.run_id, owner_key=history.owner,
        request_id="new-like", rating="LIKE", source=new_source)
    with stack.repository.database_read() as db:
        new_task_id = db.execute("SELECT task_id FROM memory_tasks WHERE run_id=?", (newer.run_id,)).fetchone()[0]
    assert stack.history.run_once()
    assert stack.store.get_run(history.created.conversation_id)["run_id"] == newer.run_id
    assert _memory_task(stack, new_task_id)[:3] == ("PENDING", new_source.problem_text, new_source.report_markdown)
    if status == "READY":
        assert _memory_task(stack, history.task_id)[0] == "READY"
        assert len(history.memory.active_cards(history.source.skill_name)) == 1
    else:
        assert _memory_task(stack, history.task_id) == ("DELETED", None, None, None, 0)
        assert not history.memory.finish_task(history.task_id, history.card)
    assert all(not path.exists() for path in history.paths)


def test_core_report_and_owned_resources_expire_at_seven_days_only(stack):
    paths = _completed(stack)
    external = _file(stack.root, "codeagent/session.json", b"keep codeagent")
    unknown_phase = _file(stack.root, f"tmp/workspaces/{JOB_ID}.unrecognized/keep.txt")
    stack.clock.value = "2026-08-07T00:00:59.999Z"
    assert not stack.history.run_once()
    assert all(path.exists() for path in paths)
    stack.clock.value = BOUNDARY
    assert stack.history.run_once()
    assert stack.repository.read_snapshot(CASE_ID).cases == {}
    assert all(not path.exists() for path in paths)
    assert external.read_bytes() == b"keep codeagent" and unknown_phase.exists()
    for table in ("completed_cases", "completed_case_retention", "object_index", "request_index", "resource_index", "archive_tasks", "history_cleanup_jobs"):
        assert stack.repository._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_case_download_lease_defers_expiry_and_cleanup_reservation_rejects_read(stack):
    paths = _completed(stack)
    stack.clock.value = NOW
    with stack.repository.case_usage(CASE_ID):
        assert stack.repository.case_cleanup_if_idle(CASE_ID) is None
        stack.history.run_once()
        assert all(path.exists() for path in paths)
    exclusive = stack.repository.case_cleanup_if_idle(CASE_ID)
    assert exclusive is not None
    with exclusive:
        with pytest.raises(ApplicationPortError) as failure:
            with stack.repository.case_usage(CASE_ID):
                pass
        assert failure.value.error.code is ErrorCode.CASE_NOT_FOUND
    assert stack.history.run_once()
    assert all(not path.exists() for path in paths)
    assert not stack.repository._case_users and not stack.repository._case_cleaning


def test_running_case_and_pending_archive_are_never_expired(stack):
    _populate(stack.repository)
    stack.clock.value = NOW
    assert not stack.history.run_once()
    assert stack.repository.read_case(CASE_ID).case.active_job_id == JOB_ID
    _finish(stack.repository)
    with stack.repository.database_transaction() as db:
        db.execute("INSERT INTO archive_tasks VALUES (?,'PENDING','{}',NULL)", (CASE_ID,))
    assert not stack.history.run_once()
    assert stack.repository.read_snapshot(CASE_ID).cases
    with stack.repository.database_transaction() as db:
        db.execute("UPDATE archive_tasks SET status='FAILED' WHERE case_id=?", (CASE_ID,))
    assert stack.history.run_once()


def test_old_waiting_case_without_worker_is_reclaimed(stack):
    aggregate = StateFile.model_validate_json(Path("tests/fixtures/contracts/positive/state.json").read_bytes()).cases[CASE_ID]
    case = Case.model_validate({**aggregate.case.model_dump(), "status": CaseStatus.WAITING_INPUT,
        "active_job_id": None, "updated_at": OLD})
    stack.repository.commit(1, None, _empty_mutation(upsert_case=case,
        insert_jobs=list(aggregate.jobs.values())))
    stack.clock.value = NOW
    assert stack.history.run_once()
    assert not stack.repository.read_snapshot(CASE_ID).cases


def test_worker_activity_defers_terminal_case_expiry(stack):
    paths = _completed(stack)
    worker = SimpleNamespace(idle=False)
    worker.cases_idle = lambda ids: worker.idle
    stack.history.archive = worker
    stack.clock.value = NOW
    assert not stack.history.run_once()
    assert all(path.exists() for path in paths)
    worker.idle = True
    assert stack.history.run_once()


def test_inflight_core_attachment_upload_blocks_history_cleanup(stack):
    _populate(stack.repository)
    aid = "00000000-0000-0000-0000-000000000123"
    before = stack.repository.read_snapshot(CASE_ID)
    attachment = Attachment(attachment_id=aid, case_id=CASE_ID, status=AttachmentStatus.UPLOADING,
        name="log.zip", content_type="application/zip", declared_size=3,
        declared_sha256=hashlib.sha256(b"log").hexdigest(), size=None, sha256=None,
        storage_key=None, created_at=OLD, updated_at=OLD)
    stack.repository.commit(before.generation, before.cases[CASE_ID].case.case_revision,
        _empty_mutation(upsert_attachments=[attachment]))
    _finish(stack.repository)
    path = _file(stack.root, f"tmp/uploads/{aid}/payload")
    stack.clock.value = NOW
    with stack.registry.acquire(aid):
        assert not stack.history.run_once()
        assert path.exists()
    assert stack.history.run_once()
    assert not path.exists()


def test_old_run_is_removed_while_new_run_stays_and_sse_cursor_expires(stack):
    created, original, newer = _old_and_new_runs(stack)
    cid = created.conversation_id
    old_events = [event.sequence for event in stack.store.list_events(cid) if event.run_id == original.run_id]
    with stack.usage.acquire(cid):
        stack.history.run_once()
        assert stack.store.get_run(cid, original.run_id)
    assert stack.history.run_once()
    with pytest.raises(AgentStoreError) as gone:
        stack.store.get_run(cid, original.run_id)
    assert gone.value.code == "AGENT_RUN_NOT_FOUND"
    assert stack.store.get_run(cid)["run_id"] == newer.run_id
    with pytest.raises(AgentStoreError) as cursor:
        stack.store.list_events(cid, after=0)
    assert cursor.value.code == "AGENT_EVENT_CURSOR_EXPIRED" and cursor.value.status_code == 409
    floor = max(old_events)
    assert cursor.value.details == [{"field": "retained_after_sequence", "actual": floor}]
    retained = stack.store.list_events(cid, after=floor)
    assert retained and retained[0].sequence == floor + 1
    stack.store.submit_message(cid, "third", "进一步说明")
    with pytest.raises(AgentStoreError, match="事件记录已过保留期"):
        stack.store.list_events(cid, after=0)
    with stack.repository.database_read() as db:
        for table in ("agent_messages", "agent_events", "agent_dispatches", "agent_attachment_imports", "agent_stop_requests"):
            assert db.execute(f"SELECT count(*) FROM {table} WHERE run_id=?", (original.run_id,)).fetchone()[0] == 0
    # The expired creation key no longer dereferences the removed ordinal=1 run.
    replacement = stack.store.create_conversation("create")
    assert replacement.conversation_id != cid


def test_completion_age_is_not_extended_by_rename(stack):
    created = stack.store.create_conversation("rename")
    stack.store.submit_message(created.conversation_id, "message", "问题")
    _close_run(stack.store, created.conversation_id, created.run_id)
    stack.clock.value = NOW
    stack.store.rename_conversation(created.conversation_id, "仍然到期")
    assert stack.store.get_run(created.conversation_id)["completed_at"] == OLD
    assert stack.history.run_once()
    with pytest.raises(AgentStoreError):
        stack.store.get_run(created.conversation_id)


def test_legacy_closed_run_preserves_original_age_on_first_rename(stack):
    created = stack.store.create_conversation("legacy-rename")
    stack.store.submit_message(created.conversation_id, "message", "旧版已完成的问题")
    _close_run(stack.store, created.conversation_id, created.run_id)
    with stack.repository.database_transaction() as db:
        for table in ("agent_conversations", "agent_conversation_runs"):
            db.execute(f"UPDATE {table} SET body=json_remove(body,'$.completed_at') WHERE conversation_id=?",
                       (created.conversation_id,))
    stack.clock.value = NOW
    stack.store.rename_conversation(created.conversation_id, "重命名不会延长保留期")
    assert stack.store.get_run(created.conversation_id)["completed_at"] == OLD
    stack.history.run_once()
    with pytest.raises(AgentStoreError):
        stack.store.get_run(created.conversation_id)


def test_expired_web_run_removes_its_case_and_report_but_preserves_new_run(stack):
    created = stack.store.create_conversation("web-case")
    stack.store.submit_message(created.conversation_id, "original", "旧问题")
    _populate(stack.repository)
    stack.store.bind_case(created.conversation_id, CASE_ID)
    report = _file(stack.root, f"resources/cases/{CASE_ID}/artifacts/{JOB_ID}/payload")
    _finish(stack.repository)
    stack.clock.value = NOW
    newer = stack.store.submit_message(created.conversation_id, "new", "新的问题")
    assert stack.history.run_once()
    assert not report.exists()
    assert not stack.repository.read_snapshot(CASE_ID).cases
    assert stack.store.get_run(created.conversation_id)["run_id"] == newer.run_id


def test_idle_empty_conversation_and_unadopted_upload_expire_but_fresh_conversation_stays(stack):
    old = stack.store.create_conversation("empty")
    active = stack.store.create_conversation("active")
    upload = stack.store.reserve_attachment(active.conversation_id, "upload", "log.zip", "application/zip", 3,
        hashlib.sha256(b"log").hexdigest())
    path = _file(stack.root, f"resources/conversations/{upload.attachment_id}/payload", b"log")
    stack.store.complete_attachment(upload.attachment_id, str(path))
    stack.clock.value = NOW
    stack.store.submit_message(active.conversation_id, "fresh", "近期问题")
    with stack.usage.acquire(active.conversation_id):
        stack.history.run_once()
        assert path.exists()
    assert stack.history.run_once()
    assert not path.exists()
    assert stack.store.get_run(active.conversation_id)
    with pytest.raises(AgentStoreError):
        stack.store.get_run(old.conversation_id)
    stack.store.finish_stop(old.conversation_id, old.run_id)
    assert stack.cleanup.run_once()


def test_selected_upload_stays_until_its_retained_run_expires(stack):
    created = stack.store.create_conversation("selected")
    upload = stack.store.reserve_attachment(created.conversation_id, "upload", "log.zip", "application/zip", 3,
        hashlib.sha256(b"log").hexdigest())
    path = _file(stack.root, f"resources/conversations/{upload.attachment_id}/payload", b"log")
    stack.store.complete_attachment(upload.attachment_id, str(path))
    stack.clock.value = NOW
    stack.store.submit_message(created.conversation_id, "fresh", "读取此日志", [upload.attachment_id])
    stack.history.run_once()
    assert path.exists()
    assert stack.store.get_attachment(upload.attachment_id)


def test_deleted_tombstone_and_all_retry_indexes_have_finite_lifetime(stack):
    created = stack.store.create_conversation("deleted", owner_key="a" * 64)
    stack.store.request_delete(created.conversation_id, owner_key="a" * 64)
    # An empty run has no worker; settle the durable stop like the controller.
    stack.store.finish_stop(created.conversation_id, created.run_id)
    assert stack.cleanup.run_once()
    assert stack.store.request_delete(created.conversation_id, owner_key="a" * 64).status == "DELETED"
    stack.clock.value = BOUNDARY
    assert stack.history.run_once()
    for table in ("agent_conversations", "agent_cleanup_jobs", "agent_deleted_requests", "agent_create_keys", "agent_create_key_times"):
        assert stack.repository._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert stack.store.create_conversation("deleted", owner_key="a" * 64).conversation_id != created.conversation_id


def test_filesystem_failure_keeps_exact_manifest_and_retries_after_reopen(stack, monkeypatch):
    paths = _completed(stack)
    stack.clock.value = NOW
    original_delete = stack.quarantine.delete
    monkeypatch.setattr(stack.quarantine, "delete", lambda path: (_ for _ in ()).throw(OSError("injected deletion failure")))
    with pytest.raises(OSError):
        stack.history.run_once()
    assert not stack.repository.read_snapshot(CASE_ID).cases
    assert stack.repository._db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1
    monkeypatch.setattr(stack.quarantine, "delete", original_delete)
    stack.repository.close()
    reopened = CaseStateRepository(stack.root, StorageCoordinationLock(), stack.clock,
        DeterministicIdGenerator(seed="reopen-history"), file_sync=FakeFileSync())
    try:
        restarted = HistoryRetentionService(reopened, AgentStore(reopened, clock=stack.clock), stack.quarantine,
            ConversationUsageGuard(), stack.clock)
        restarted.run_once()
        assert reopened._db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 0
    finally:
        reopened.close()
    assert all(not path.exists() for path in paths)


def test_database_compaction_reclaims_pages_and_truncates_wal(stack):
    with stack.repository.database_transaction() as db:
        db.execute("CREATE TABLE retention_compaction_fixture (payload BLOB)")
        db.executemany("INSERT INTO retention_compaction_fixture VALUES (?)", [(b"x" * 131072,)] * 32)
    stack.repository._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_before = (stack.root / "completed.sqlite3").stat().st_size
    with stack.repository.database_transaction() as db:
        db.execute("DELETE FROM retention_compaction_fixture")
    assert stack.repository._db.execute("PRAGMA freelist_count").fetchone()[0] > 0
    stack.repository.compact_history_database()
    assert stack.repository._db.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert (stack.root / "completed.sqlite3").stat().st_size < size_before
    wal = stack.root / "completed.sqlite3-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_hourly_maintenance_reclaims_rows_deleted_after_previous_history_pass(stack):
    with stack.repository.database_transaction() as db:
        db.execute("CREATE TABLE asynchronous_cleanup_fixture (payload BLOB)")
        db.executemany("INSERT INTO asynchronous_cleanup_fixture VALUES (?)", [(b"x" * 131072,)] * 32)
    stack.history.run_once()
    before = (stack.root / "completed.sqlite3").stat().st_size
    # This represents the independent conversation cleanup worker committing
    # after history retention has completed its own maintenance cycle.
    with stack.repository.database_transaction() as db:
        db.execute("DELETE FROM asynchronous_cleanup_fixture")
    stack.clock.value = "2026-07-31T01:01:00.000Z"
    assert not stack.history.run_once()
    assert stack.repository._db.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert (stack.root / "completed.sqlite3").stat().st_size < before
    wal = stack.root / "completed.sqlite3-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_legacy_age_index_is_filled_only_during_maintenance_without_extending_retention(stack):
    _completed(stack)
    original = stack.repository._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (CASE_ID,)).fetchone()[0]
    stack.repository._db.execute("DELETE FROM completed_case_retention WHERE case_id=?", (CASE_ID,))
    stack.clock.value = NOW
    reopened = CaseStateRepository(stack.root, StorageCoordinationLock(), stack.clock,
        DeterministicIdGenerator(seed="legacy-age-reopen"), file_sync=FakeFileSync())
    try:
        assert reopened._db.execute("SELECT count(*) FROM completed_case_retention").fetchone()[0] == 0
        assert reopened.history_case_candidates(NOW) == [CASE_ID]
        assert reopened._db.execute("SELECT retained_at FROM completed_case_retention WHERE case_id=?", (CASE_ID,)).fetchone()[0] == OLD
        assert reopened._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (CASE_ID,)).fetchone()[0] == original
    finally:
        reopened.close()


@pytest.mark.parametrize("corruption", ["invalid-json", "invalid-age"])
def test_unverifiable_legacy_age_is_preserved_without_blocking_other_candidates(stack, corruption):
    _completed(stack)
    raw = stack.repository._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (CASE_ID,)).fetchone()[0]
    if corruption == "invalid-json":
        damaged = b"not JSON"
    else:
        payload = json.loads(raw)
        payload["cases"][CASE_ID]["case"]["updated_at"] = "invalid date"
        damaged = json.dumps(payload).encode()
    stack.repository._db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?", (damaged, CASE_ID))
    stack.repository._db.execute("DELETE FROM completed_case_retention WHERE case_id=?", (CASE_ID,))
    other = _create_distinct(stack.repository, 22)
    _cancel(stack.repository, other)
    assert stack.repository.history_case_candidates(NOW) == [other]
    assert stack.repository.health().valid
    assert stack.repository._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (CASE_ID,)).fetchone()[0] == damaged
    assert stack.repository._db.execute("SELECT retained_at FROM completed_case_retention WHERE case_id=?", (CASE_ID,)).fetchone() is None


def test_failed_cleanup_does_not_starve_other_cases_or_next_pass(stack, monkeypatch):
    _completed(stack)
    second = _create_distinct(stack.repository, 2)
    _cancel(stack.repository, second)
    second_path = _file(stack.root, f"resources/cases/{second}/artifacts/{JOB_ID}/payload")
    original = stack.quarantine.delete
    def delete(path):
        if CASE_ID in path.parts:
            raise OSError("one Case remains busy on disk")
        original(path)
    monkeypatch.setattr(stack.quarantine, "delete", delete)
    stack.clock.value = NOW
    with pytest.raises(OSError):
        stack.history.run_once()
    assert not second_path.exists()
    assert stack.repository._db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1
    third = _create_distinct(stack.repository, 3)
    _cancel(stack.repository, third)
    third_path = _file(stack.root, f"resources/cases/{third}/artifacts/{JOB_ID}/payload")
    with pytest.raises(OSError):
        stack.history.run_once()
    assert not third_path.exists()
    assert stack.repository._db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1
