"""Explicit deletion drains usage and removes only its durable ownership set."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from problem_locator.agent.cleanup import ConversationCleanupService
from problem_locator.agent.store import AgentStore
from problem_locator.agent.usage import ConversationUsageGuard
from problem_locator.contracts import Case, CaseStatus, StateFile
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.quarantine import QuarantineMover
from tests.deterministic.unit.storage.fakes import FakeFileSync, FaultInjectingReplace
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _empty_mutation, _finish, _open, _populate

OTHER_CASE = "00000000-0000-0000-0000-000000000002"
OTHER_JOB = "00000000-0000-0000-0000-000000000020"


def _file(root, relative, content=b"private data"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _other_case(repository):
    raw = Path("tests/fixtures/contracts/positive/state.json").read_text(encoding="utf-8")
    state = StateFile.model_validate_json(raw.replace(CASE_ID, OTHER_CASE).replace(JOB_ID, OTHER_JOB))
    aggregate = state.cases[OTHER_CASE]
    repository.commit(1, None, _empty_mutation(upsert_case=aggregate.case, insert_jobs=list(aggregate.jobs.values())))
    current = repository.read_snapshot(OTHER_CASE)
    case = current.cases[OTHER_CASE].case
    repository.commit(current.generation, case.case_revision, _empty_mutation(upsert_case=Case.model_validate({
        **case.model_dump(), "status": CaseStatus.CANCELLED, "active_job_id": None,
        "case_revision": case.case_revision + 1})))


@pytest.fixture
def stack(tmp_path):
    repository = _open(tmp_path)
    _populate(repository)
    _other_case(repository)
    store = AgentStore(repository)
    cid = store.create_conversation("delete-me").conversation_id
    store.bind_case(cid, CASE_ID)
    attachment = store.reserve_attachment(cid, "log", "trace.log", "text/plain", 3, hashlib.sha256(b"log").hexdigest())
    upload = _file(tmp_path, f"resources/conversations/{attachment.attachment_id}/payload", b"log")
    store.complete_attachment(attachment.attachment_id, str(upload))
    store.submit_message(cid, "message", "问题描述", [attachment.attachment_id])
    paths = [upload,
        _file(tmp_path, f"resources/cases/{CASE_ID}/artifacts/{JOB_ID}/payload"),
        _file(tmp_path, f"jobs/{JOB_ID}/execution.json"),
        _file(tmp_path, f"tmp/workspaces/{JOB_ID}/model-response.txt"),
        _file(tmp_path, f"tmp/workspaces/{JOB_ID}.logparse-preprocess/output/result.json"),
        _file(tmp_path, f"tmp/proposals/{JOB_ID}/p-example/payload")]
    retained = _file(tmp_path, f"resources/cases/{OTHER_CASE}/artifacts/{OTHER_JOB}/payload", b"keep other case")
    usage = ConversationUsageGuard()
    mover = QuarantineMover(repository.layout, StorageCoordinationLock(), FakeFileSync(), FaultInjectingReplace())
    cleanup = ConversationCleanupService(store, repository, mover, usage)
    yield repository, store, cid, paths, retained, usage, cleanup
    repository.close()


def _stop_finished(store, cid):
    for value in store.pending_stops():
        if value["conversation_id"] == cid:
            store.finish_stop(cid, value["run_id"])


def test_deleted_conversation_removes_only_its_case_uploads_jobs_and_history(stack):
    repository, store, cid, paths, retained, _, cleanup = stack
    _finish(repository)
    before = repository.read_case(OTHER_CASE)
    store.request_delete(cid)
    assert repository.is_agent_case_deleted(CASE_ID)
    assert not repository.is_agent_case_deleted(OTHER_CASE)
    assert cleanup.run_once()
    assert all(not path.exists() for path in paths)
    assert retained.read_bytes() == b"keep other case"
    assert repository.read_case(OTHER_CASE) == before
    assert repository.read_snapshot(CASE_ID).cases == {}
    assert store.request_delete(cid).status == "DELETED"
    assert not cleanup.run_once()
    for table in ("agent_messages", "agent_events", "agent_dispatches", "agent_attachments", "agent_conversation_runs"):
        assert repository._db.execute(f"SELECT count(*) FROM {table} WHERE conversation_id=?", (cid,)).fetchone()[0] == 0
    for table in ("completed_cases", "object_index", "request_index", "resource_index", "archive_tasks"):
        assert repository._db.execute(f"SELECT count(*) FROM {table} WHERE case_id=?", (CASE_ID,)).fetchone()[0] == 0


def test_cleanup_waits_for_durable_stop_even_if_workers_are_idle(stack):
    repository, store, cid, paths, _, _, cleanup = stack
    store.request_delete(cid)
    assert not cleanup.run_once()
    assert all(path.exists() for path in paths)
    assert CASE_ID in repository.read_snapshot(CASE_ID).cases
    assert repository._db.execute("SELECT error_code FROM agent_cleanup_jobs").fetchone()[0] == "CLEANUP_AWAITING_STOP"
    _finish(repository)
    _stop_finished(store, cid)
    assert cleanup.run_once()


def test_cleanup_waits_for_download_upload_and_archive_usage(stack):
    repository, store, cid, paths, _, usage, cleanup = stack
    _finish(repository)
    store.request_delete(cid)
    with usage.acquire(cid):
        assert not cleanup.run_once()
        assert all(path.exists() for path in paths)
    class Worker:
        idle = False
        def cancel_cases(self, case_ids):
            assert case_ids == [CASE_ID]
        def cases_idle(self, case_ids):
            return self.idle
    worker = Worker()
    cleanup.archive = worker
    assert not cleanup.run_once()
    worker.idle = True
    assert cleanup.run_once()


def test_cleanup_crash_after_core_purge_retries_durable_quarantine_manifest(stack, monkeypatch):
    repository, store, cid, paths, retained, _, cleanup = stack
    _finish(repository)
    store.request_delete(cid)
    delete = cleanup.quarantine.delete
    calls = []
    def failure(path):
        calls.append(path)
        raise OSError("injected deletion failure")
    monkeypatch.setattr(cleanup.quarantine, "delete", failure)
    assert not cleanup.run_once()
    assert repository.read_snapshot(CASE_ID).cases == {}
    assert calls[0].exists()
    row = repository._db.execute("SELECT status,manifest FROM agent_cleanup_jobs WHERE conversation_id=?", (cid,)).fetchone()
    assert row[0] == "FAILED" and "paths" in row[1]
    monkeypatch.setattr(cleanup.quarantine, "delete", delete)
    # Reopen SQLite and reconstruct the adapter and cleaner, as after process
    # loss. The Case is gone; only its persisted manifest can guide this.
    root = repository.layout.data_root
    repository.close()
    reopened = _open(root)
    try:
        restarted_store = AgentStore(reopened)
        restarted = ConversationCleanupService(restarted_store, reopened, cleanup.quarantine,
            ConversationUsageGuard())
        assert restarted.run_once()
        assert restarted_store.request_delete(cid).status == "DELETED"
    finally:
        reopened.close()
    assert all(not path.exists() for path in paths)
    assert not calls[0].exists() and retained.exists()


def test_cleanup_removes_intake_workspaces_without_guessing_other_directories(stack):
    repository, store, cid, _, _, _, cleanup = stack
    workspace_id = "00000000-0000-0000-0000-000000000071"
    unrelated_id = "00000000-0000-0000-0000-000000000072"
    store.record_dispatch(cid, "intake-workspace", {"operation": "Intake", "workspace_id": workspace_id})
    owned = _file(repository.layout.data_root, f"tmp/workspaces/{workspace_id}/runtime/model.txt")
    unrelated = _file(repository.layout.data_root, f"tmp/workspaces/{unrelated_id}/runtime/model.txt")
    _finish(repository)
    store.request_delete(cid)
    assert cleanup.run_once()
    assert not owned.exists()
    assert unrelated.exists()


def test_cleanup_removes_owned_preprocessing_without_touching_other_jobs(stack):
    repository, store, cid, paths, _, _, cleanup = stack
    unrelated = _file(repository.layout.data_root,
        f"tmp/workspaces/{OTHER_JOB}.logparse-preprocess/output/result.json")
    owned = next(path for path in paths if ".logparse-preprocess" in str(path))
    _finish(repository)
    store.request_delete(cid)

    assert cleanup.run_once()

    assert not owned.exists()
    assert unrelated.read_bytes() == b"private data"


def test_core_download_delays_whole_conversation_cleanup_until_close(stack):
    from problem_locator.application.queries import ApplicationQueryService
    from problem_locator.contracts import ResourceRef
    from tests.deterministic.contracts.fakes import InMemoryResourceStore
    from tests.deterministic.unit.application.test_queries import ARTIFACT_ID, _Notifier, _diagnostic_artifact

    repository, store, cid, paths, _, _, cleanup = stack
    artifact = _diagnostic_artifact(b"{}")
    state = repository.read_snapshot(CASE_ID)
    repository.commit(state.generation, state.cases[CASE_ID].case.case_revision,
        _empty_mutation(insert_artifacts=[artifact]))
    _finish(repository)
    resources = InMemoryResourceStore()
    resources.seed_formal_resource(ResourceRef(resource_kind=artifact.resource_kind,
        storage_key=artifact.storage_key, size=artifact.size, sha256=artifact.sha256),
        state_reference_count=1, payload=b"{}")
    opened = ApplicationQueryService(repository, resources, _Notifier()).open_artifact(CASE_ID, ARTIFACT_ID)
    store.request_delete(cid)

    assert not cleanup.run_once()
    assert all(path.exists() for path in paths)
    assert repository._db.execute("SELECT error_code FROM agent_cleanup_jobs WHERE conversation_id=?", (cid,)).fetchone()[0] == "CLEANUP_BUSY"
    assert opened.stream.read(2) == b"{}"
    opened.stream.close()

    assert cleanup.run_once()
    assert all(not path.exists() for path in paths)


def test_cleanup_recovers_running_claim_after_adapter_restart(stack):
    repository, store, cid, paths, _, _, cleanup = stack
    _finish(repository)
    store.request_delete(cid)
    claim = store.claim_cleanup()
    assert claim["conversation_id"] == cid
    restarted = ConversationCleanupService(AgentStore(repository), repository, cleanup.quarantine,
        ConversationUsageGuard())
    assert restarted.run_once()
    assert all(not path.exists() for path in paths)


def test_repository_rejects_unrelated_case_in_cleanup_manifest(stack):
    repository, store, cid, _, retained, _, _ = stack
    _finish(repository)
    store.request_delete(cid)
    with pytest.raises(ValueError, match="does not belong"):
        repository.prepare_agent_case_cleanup(cid, [OTHER_CASE])
    with pytest.raises(ValueError, match="does not belong"):
        repository.purge_agent_cases(cid, [OTHER_CASE])
    assert retained.exists() and repository.read_case(OTHER_CASE).case.case_id == OTHER_CASE


def test_cleanup_failure_preserves_report_rows_until_manifest_is_durable(stack, monkeypatch):
    repository, store, cid, paths, _, _, cleanup = stack
    _finish(repository)
    store.request_delete(cid)
    def unavailable(*args):
        raise OSError("manifest write unavailable")
    monkeypatch.setattr(store, "update_cleanup_manifest", unavailable)
    assert not cleanup.run_once()
    assert repository.read_case(CASE_ID).case.case_id == CASE_ID
    assert all(path.exists() for path in paths)
