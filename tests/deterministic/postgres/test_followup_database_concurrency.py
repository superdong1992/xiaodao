"""PostgreSQL regressions for independent work, queue claims and byte quotas."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.agent.usage import ConversationUsageGuard
from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.store import FollowupStore
from problem_locator.storage.database import lock_conversation
from problem_locator.storage.history_retention import HistoryRetentionService
from tests.postgres_helpers import postgres_database_url, postgres_repository

OWNER = "a" * 64
NOW = "2026-09-24T01:00:00.000Z"


@pytest.fixture
def followup_database(postgres_repository):
    clock = SimpleNamespace(now=lambda: NOW)
    agent = AgentStore(postgres_repository, clock=clock, runtime_epoch="postgres-followup")
    followups = FollowupStore(postgres_repository, clock)
    agent.followup_store = followups
    report = "# 诊断报告\n等待更多证据核实连接池排队。\n"
    source = FollowupSource("00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000011", "为什么请求超时？", report,
        hashlib.sha256(report.encode()).hexdigest(), "GENERIC", False, NOW)
    return SimpleNamespace(repository=postgres_repository, agent=agent, followups=followups, source=source)


def _conversation(state, name):
    return state.agent.create_conversation(name, owner_key=OWNER)


def _submit(state, conversation, name="question"):
    return state.followups.submit(conversation.conversation_id, conversation.run_id,
        FollowupRequest(request_id=name, text="结论有哪些依据？"), state.source, OWNER)


def test_postgres_followup_other_conversation_commits_while_one_is_locked(followup_database):
    state = followup_database
    first, second = _conversation(state, "first"), _conversation(state, "second")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with state.repository.database_transaction() as db:
            lock_conversation(db, first.conversation_id)
            # This waits for a commit while the first transaction remains open.
            # The former repository-wide lock cannot satisfy this ordering.
            receipt = executor.submit(_submit, state, second).result(timeout=5)
            assert receipt.conversation_id == second.conversation_id


def test_postgres_followup_concurrent_retries_create_one_event_and_task(followup_database):
    state = followup_database
    conversation = _conversation(state, "same-request")
    barrier = threading.Barrier(6)

    def submit():
        barrier.wait(timeout=10)
        return _submit(state, conversation)

    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(lambda _: submit(), range(6)))
    assert len({receipt.followup_id for receipt in receipts}) == 1
    with state.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_followup_tasks").fetchone()[0] == 1
        assert db.execute("SELECT sequence FROM agent_followup_events").fetchall() == [(1,)]


@pytest.mark.parametrize("snapshot", [False, True])
def test_postgres_followup_claim_skips_an_already_locked_queue_row(followup_database, snapshot):
    state = followup_database
    first, second = _conversation(state, "queue-first"), _conversation(state, "queue-second")
    if snapshot:
        for conversation in (first, second):
            state.followups.enqueue_snapshot(conversation.conversation_id, conversation.run_id, state.source)
        table, claim = "agent_followup_snapshots", state.followups.claim_snapshot
    else:
        _submit(state, first)
        _submit(state, second)
        table, claim = "agent_followup_tasks", state.followups.claim_task
    with ThreadPoolExecutor(max_workers=1) as executor:
        with state.repository.database_transaction() as db:
            db.execute(f"SELECT run_id FROM {table} WHERE run_id=? FOR UPDATE", (first.run_id,)).fetchone()
            claimed = executor.submit(claim).result(timeout=5)
            assert claimed is not None and claimed["run_id"] == second.run_id
    claimed = claim()
    assert claimed is not None and claimed["run_id"] == first.run_id
    assert claim() is None


@pytest.mark.parametrize("snapshot", [False, True])
def test_postgres_followup_claim_never_waits_for_conversation_while_holding_queue_row(followup_database, snapshot):
    state = followup_database
    conversation = _conversation(state, "deleting")
    if snapshot:
        state.followups.enqueue_snapshot(conversation.conversation_id, conversation.run_id, state.source)
        claim = state.followups.claim_snapshot
    else:
        _submit(state, conversation)
        claim = state.followups.claim_task
    with ThreadPoolExecutor(max_workers=1) as executor:
        with state.repository.database_transaction() as db:
            lock_conversation(db, conversation.conversation_id)
            assert executor.submit(claim).result(timeout=5) is None
            # A claim blocked on the conversation would hold the row needed
            # here and turn a normal delete into a deadlock.
            state.followups.revoke(db, conversation.conversation_id)
    assert claim() is None


def test_postgres_followup_competing_snapshots_cannot_exceed_global_byte_budget(followup_database):
    state = followup_database
    conversations = [_conversation(state, name) for name in ("quota-first", "quota-second")]
    for conversation in conversations:
        state.followups.enqueue_snapshot(conversation.conversation_id, conversation.run_id, state.source)
    assert state.followups.claim_snapshot() is not None
    assert state.followups.claim_snapshot() is not None
    barrier = threading.Barrier(2)

    def reserve(conversation):
        barrier.wait(timeout=10)
        try:
            state.followups.reserve_snapshot(conversation.run_id, 75, 100, 100)
        except ValueError as error:
            assert str(error) == "snapshot exceeds its byte budget"
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(reserve, conversations))
    assert accepted.count(True) == 1
    with state.repository.database_read() as db:
        assert db.execute("SELECT sum(reserved_bytes) FROM agent_followup_snapshots").fetchone()[0] == 75


def test_postgres_followup_snapshot_budget_preserves_sqlite_64_bit_range(followup_database):
    state = followup_database
    conversation = _conversation(state, "large-budget")
    state.followups.enqueue_snapshot(conversation.conversation_id, conversation.run_id, state.source)
    assert state.followups.claim_snapshot() is not None
    size = 3 * 1024**3
    state.followups.reserve_snapshot(conversation.run_id, size, 4 * 1024**3, 5 * 1024**3)
    assert state.followups.snapshot(conversation.run_id)["reserved_bytes"] == size


@pytest.mark.parametrize("reference", ["message", "restart"])
def test_postgres_retention_preserves_referenced_upload_then_prunes_its_events(followup_database, reference):
    state = followup_database
    conversation = _conversation(state, "retention")
    cid = conversation.conversation_id
    attachment = state.agent.reserve_attachment(cid, "upload", "trace.log", "text/plain", 1, "a" * 64)
    state.agent.complete_attachment(attachment.attachment_id)
    if reference == "message":
        state.agent.submit_message(cid, "message", "请分析日志。", [attachment.attachment_id])
        table = "agent_messages"
    else:
        with state.repository.database_transaction() as db:
            db.execute("INSERT INTO agent_generic_restarts(conversation_id,request_id,run_id,fingerprint,command_key,status,payload) "
                "VALUES (?,?,?,?,?,'PENDING',?)", (cid, "restart", conversation.run_id, "fingerprint", "command",
                    json.dumps({"message": {"attachment_ids": [attachment.attachment_id]}})))
        table = "agent_generic_restarts"
    # No resource directory was created, so durable cleanup has no file to move.
    history = HistoryRetentionService(state.repository, state.agent, None,
        ConversationUsageGuard(), SimpleNamespace(now=lambda: NOW))
    assert history._expire_upload(attachment.attachment_id, cid, NOW) is False
    with state.repository.database_transaction() as db:
        db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (cid,))
    assert history._expire_upload(attachment.attachment_id, cid, NOW) is True
    with state.repository.database_read() as db:
        assert db.execute("SELECT 1 FROM agent_attachments WHERE attachment_id=?", (attachment.attachment_id,)).fetchone() is None
        head = json.loads(db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()[0])
        assert head["events_pruned_through"] >= 2
    assert history._retry_paths() == (False, [])
    assert history._prune_keys_and_tombstones(NOW) is True
    assert history._prune_keys_and_tombstones(NOW) is False
