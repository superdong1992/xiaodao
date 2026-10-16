"""Explicit deletion waits for files already owned by a history cleanup receipt."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.deterministic.unit.storage.test_history_retention import _close_run, stack
from tests.deterministic.unit.storage.test_web_resource_retention import upload


@pytest.mark.parametrize("legacy_receipt", [False, True])
def test_user_delete_racing_history_isolation_waits_for_physical_cleanup(stack, monkeypatch, legacy_receipt):
    created = stack.store.create_conversation("delete-during-isolation")
    cid, rid = created.conversation_id, created.run_id
    aid, source = upload(stack, cid)
    stack.store.submit_message(cid, "question", "保留正文，删除日志", [aid])
    _close_run(stack.store, cid, rid)
    isolated, release = threading.Event(), threading.Event()
    paths = []
    delete = stack.quarantine.delete

    def fail_after_isolation(path):
        paths.append(path)
        isolated.set()
        assert release.wait(5)
        raise OSError("injected failure after isolation")

    monkeypatch.setattr(stack.quarantine, "delete", fail_after_isolation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        reclaim = executor.submit(stack.history.reclaim_web_resources)
        try:
            assert isolated.wait(5)
            assert not source.exists() and paths[0].exists()
            assert stack.store.request_delete(cid).status == "DELETING"
            # An in-progress history delete owns the conversation lease.
            assert not stack.cleanup.run_once()
        finally:
            release.set()
        assert reclaim.result(timeout=5) is False

    if legacy_receipt:
        # Receipts written before the resource/body split have no kind field.
        with stack.repository.database_transaction() as db:
            cleanup_id, raw = db.execute("SELECT cleanup_id,manifest FROM history_cleanup_jobs").fetchone()
            manifest = json.loads(raw)
            manifest.pop("kind")
            db.execute("UPDATE history_cleanup_jobs SET manifest=? WHERE cleanup_id=?",
                (json.dumps(manifest), cleanup_id))

    for _ in range(2):
        assert not stack.cleanup.run_once()
        assert stack.store.request_delete(cid).status == "DELETING"
        assert paths[0].exists()
        with stack.repository.database_read() as db:
            assert db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1
            assert db.execute("SELECT error_code FROM agent_cleanup_jobs WHERE conversation_id=?",
                (cid,)).fetchone()[0] == "CLEANUP_AWAITING_HISTORY"
            assert db.execute("SELECT count(*) FROM agent_conversation_runs WHERE conversation_id=?",
                (cid,)).fetchone()[0] == 1

    monkeypatch.setattr(stack.quarantine, "delete", delete)
    # The explicit cleaner has released its lease, so old and current receipts
    # can both drain while their conversation remains tombstoned.
    assert stack.history.run_once()
    assert not paths[0].exists()
    with stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 0
    assert stack.store.request_delete(cid).status == "DELETING"
    assert stack.cleanup.run_once()
    assert stack.store.request_delete(cid).status == "DELETED"


def test_unrelated_history_receipt_does_not_block_explicit_delete(stack, monkeypatch):
    pending = stack.store.create_conversation("pending-history")
    aid, _ = upload(stack, pending.conversation_id)
    stack.store.submit_message(pending.conversation_id, "question", "待清理的日志", [aid])
    _close_run(stack.store, pending.conversation_id, pending.run_id)

    def unavailable(_):
        raise OSError("injected deletion failure")

    monkeypatch.setattr(stack.quarantine, "delete", unavailable)
    assert not stack.history.reclaim_web_resources()
    deleted = stack.store.create_conversation("unrelated-delete")
    _close_run(stack.store, deleted.conversation_id, deleted.run_id)
    stack.store.request_delete(deleted.conversation_id)
    assert stack.cleanup.run_once()
    assert stack.store.request_delete(deleted.conversation_id).status == "DELETED"
    with stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1
