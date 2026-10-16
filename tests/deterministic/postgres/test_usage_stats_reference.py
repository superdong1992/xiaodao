"""使用真实 PostgreSQL 和业务 Store 验证长期统计参考方案。"""
from __future__ import annotations

import hashlib
import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import psycopg
import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.store import FollowupStore
from tests.postgres_helpers import postgres_database_url, postgres_repository


REFERENCE = Path(__file__).resolve().parents[3] / "usage-stats-reference"
OWNER = "a" * 64
NOW = "2026-10-16T02:00:00.000Z"


@pytest.fixture
def usage_database(postgres_repository, postgres_database_url):
    clock = SimpleNamespace(now=lambda: NOW)
    agent = AgentStore(postgres_repository, clock=clock, runtime_epoch="usage-regression")
    followups = FollowupStore(postgres_repository, clock)
    agent.followup_store = followups
    with psycopg.connect(postgres_database_url, autocommit=True) as db:
        db.execute((REFERENCE / "schema.sql").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("usage_stats_reference_writer", REFERENCE / "writer.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    report = "# 诊断报告\n请求超时的原因仍需更多证据确认。\n"
    source = FollowupSource("00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000011", "为什么请求超时？", report,
        hashlib.sha256(report.encode()).hexdigest(), "GENERIC", False, NOW)
    return SimpleNamespace(repository=postgres_repository, database_url=postgres_database_url,
        agent=agent, followups=followups, writer=writer, source=source)


def _close(state, conversation, status="COMPLETED"):
    with state.repository.database_transaction() as db:
        body = state.agent._load(db, conversation.conversation_id, run_id=conversation.run_id)
        state.agent._close(db, body, status)
        state.agent._save(db, body)


def _conversation(state, name="usage", *, close=True):
    conversation = state.agent.create_conversation(name, owner_key=OWNER)
    state.agent.submit_message(conversation.conversation_id, name + "-initial", "为什么请求超时？")
    if close:
        _close(state, conversation)
    return conversation


def _submit(state, conversation, request_id="followup"):
    return state.followups.submit(conversation.conversation_id, conversation.run_id,
        FollowupRequest(request_id=request_id, text="这个结论有哪些依据？"), state.source, OWNER)


def _cancel(state, conversation, receipt):
    return state.followups.stop(conversation.conversation_id, conversation.run_id,
        receipt.followup_id, "stop-" + receipt.request_id, OWNER)


def _refresh(state, conversation):
    refresh = getattr(state.writer, "refresh_closed_run", state.writer.record_closed_run)
    return refresh(state.repository,
        conversation_id=conversation.conversation_id, run_id=conversation.run_id)


def _backfill(state):
    with state.repository.database_transaction() as db:
        db.execute("SET LOCAL statement_timeout = '10000ms'")
        result = db.execute((REFERENCE / "backfill.sql").read_text(encoding="utf-8"))
        assert result.description[0][0] == "inserted_count"
        return result.fetchone()[0]


def _count(state, conversation):
    with state.repository.database_read() as db:
        row = db.execute("SELECT message_count FROM agent_run_usage_stats "
            "WHERE conversation_id=? AND run_id=?", (conversation.conversation_id, conversation.run_id)).fetchone()
        return None if row is None else row[0]


def test_usage_refresh_counts_initial_input_and_two_followups_once(usage_database):
    state = usage_database
    conversation = _conversation(state)
    retry = state.agent.submit_message(conversation.conversation_id, "usage-initial", "为什么请求超时？")
    assert retry.run_id == conversation.run_id
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 1
    metadata_query = ("SELECT owner_key,created_at,run_created_at,completed_at,terminal_status,created_day,recorded_at "
        "FROM agent_run_usage_stats WHERE conversation_id=? AND run_id=?")
    run_ids = (conversation.conversation_id, conversation.run_id)
    with state.repository.database_read() as db:
        original_metadata = db.execute(metadata_query, run_ids).fetchone()
    first = _submit(state, conversation, "first")
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 2
    task = state.followups.claim_task()
    assert task["followup_id"] == first.followup_id
    assert state.followups.finish_task(first.followup_id, answer="报告中的日志支持该结论。") is True
    second = _submit(state, conversation, "second")
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 3
    assert _submit(state, conversation, "second").followup_id == second.followup_id
    assert _refresh(state, conversation) is True
    assert _backfill(state) == 0
    assert _count(state, conversation) == 3
    assert state.writer.record_closed_run(state.repository,
        conversation_id=conversation.conversation_id, run_id=conversation.run_id) is True
    assert _count(state, conversation) == 3
    with state.repository.database_read() as db:
        assert db.execute(metadata_query, run_ids).fetchone() == original_metadata
        assert db.execute("SELECT count(*) FROM agent_messages").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM agent_followup_tasks").fetchone()[0] == 2


def test_usage_concurrent_followup_retries_and_refreshes_do_not_duplicate(usage_database):
    state = usage_database
    conversation = _conversation(state)
    assert _refresh(state, conversation) is True
    barrier = threading.Barrier(4)

    def submit_and_refresh():
        barrier.wait(timeout=10)
        receipt = _submit(state, conversation)
        assert _refresh(state, conversation) is True
        return receipt.followup_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: submit_and_refresh(), range(4)))
    assert len(set(ids)) == 1
    assert _count(state, conversation) == 2
    assert _backfill(state) == 0


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "CANCELLING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"])
def test_usage_counts_followups_in_every_status(usage_database, status):
    state = usage_database
    conversation = _conversation(state)
    receipt = _submit(state, conversation)
    if status not in {"QUEUED", "CANCELLED"}:
        assert state.followups.claim_task()["followup_id"] == receipt.followup_id
    if status in {"CANCELLED", "CANCELLING"}:
        assert _cancel(state, conversation, receipt).status == status
    elif status == "COMPLETED":
        assert state.followups.finish_task(receipt.followup_id, answer="已核对证据。") is True
    elif status == "FAILED":
        assert state.followups.finish_task(receipt.followup_id, code="AGENT_FOLLOWUP_FAILED") is True
    elif status == "INTERRUPTED":
        assert state.followups.finish_task(receipt.followup_id, interrupted=True) is True
    with state.repository.database_read() as db:
        assert db.execute("SELECT status FROM agent_followup_tasks").fetchone()[0] == status
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 2
    assert _refresh(state, conversation) is True
    assert _backfill(state) == 0
    assert _count(state, conversation) == 2


def test_usage_rejected_and_rolled_back_followups_are_not_counted(usage_database):
    state = usage_database
    conversation = _conversation(state)
    with pytest.raises(RuntimeError, match="rollback"):
        with state.repository.database_transaction():
            _submit(state, conversation, "rolled-back")
            raise RuntimeError("rollback")
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 1
    _submit(state, conversation, "accepted")
    with pytest.raises(AgentStoreError) as busy:
        _submit(state, conversation, "rejected")
    assert busy.value.code == "AGENT_FOLLOWUP_BUSY"
    with pytest.raises(AgentStoreError) as conflict:
        state.followups.submit(conversation.conversation_id, conversation.run_id,
            FollowupRequest(request_id="accepted", text="修改后的请求正文"), state.source, OWNER)
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 2


@pytest.mark.parametrize("already_recorded", [False, True])
def test_usage_failed_refresh_keeps_followup_and_backfill_repairs_count(usage_database, already_recorded):
    state = usage_database
    conversation = _conversation(state)
    if already_recorded:
        assert _refresh(state, conversation) is True
    receipt = _submit(state, conversation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with state.repository.database_transaction() as db:
            db.execute("LOCK TABLE agent_run_usage_stats IN ACCESS EXCLUSIVE MODE")
            assert executor.submit(_refresh, state, conversation).result(timeout=5) is False
    with state.repository.database_read() as db:
        assert db.execute("SELECT followup_id FROM agent_followup_tasks").fetchone()[0] == receipt.followup_id
    assert _count(state, conversation) == (1 if already_recorded else None)
    assert _backfill(state) == 1
    assert _count(state, conversation) == 2
    assert _backfill(state) == 0


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"])
def test_usage_closure_refresh_includes_followups_accepted_before_run_closes(usage_database, terminal_status):
    state = usage_database
    conversation = _conversation(state, close=False)
    _submit(state, conversation)
    assert _refresh(state, conversation) is False
    assert _count(state, conversation) is None
    assert _backfill(state) == 0
    assert _count(state, conversation) is None
    _close(state, conversation, terminal_status)
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 2


def test_usage_refresh_uses_requested_run_when_a_newer_run_exists(usage_database):
    state = usage_database
    first = _conversation(state)
    _cancel(state, first, _submit(state, first, "first-followup"))
    assert _refresh(state, first) is True
    message = state.agent.submit_message(first.conversation_id, "new-diagnosis", "重新定位另一个问题。")
    second = SimpleNamespace(conversation_id=first.conversation_id, run_id=message.run_id)
    assert second.run_id != first.run_id
    _close(state, second)
    _submit(state, first, "old-report-followup")
    assert _refresh(state, first) is True
    assert _refresh(state, second) is True
    assert _count(state, first) == 3
    assert _count(state, second) == 1
    with state.repository.database_read() as db:
        assert db.execute("SELECT message_count,closed_run_count FROM agent_conversation_usage_stats").fetchone() == (4, 2)


def test_usage_stale_backfill_snapshot_cannot_overwrite_newer_refresh(usage_database):
    state = usage_database
    conversation = _conversation(state)
    assert _refresh(state, conversation) is True
    _cancel(state, conversation, _submit(state, conversation, "first-followup"))
    # 只暂停旧补录的 INSERT；在线刷新继续执行真实 SQL。
    with state.repository.database_transaction() as db:
        db.execute("""CREATE FUNCTION pause_stale_usage_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF current_setting('application_name') = 'usage-stale-snapshot' THEN
                    PERFORM pg_advisory_xact_lock(91741, 611);
                END IF;
                RETURN NEW;
            END $$""")
        db.execute("CREATE TRIGGER pause_stale_usage_insert BEFORE INSERT ON agent_run_usage_stats "
            "FOR EACH ROW EXECUTE FUNCTION pause_stale_usage_insert()")
    pids = Queue()

    def old_backfill():
        with state.repository.database_transaction() as db:
            db.execute("SET LOCAL application_name='usage-stale-snapshot'")
            db.execute("SET LOCAL statement_timeout='10000ms'")
            pids.put(db.execute("SELECT pg_backend_pid()").fetchone()[0])
            return db.execute((REFERENCE / "backfill.sql").read_text(encoding="utf-8")).fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(state.database_url, autocommit=True) as blocker:
            blocker.execute("SELECT pg_advisory_lock(91741, 611)")
            future = executor.submit(old_backfill)
            try:
                pid = pids.get(timeout=5)
                deadline = time.monotonic() + 5
                while not blocker.execute("SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE pid=%s AND locktype='advisory' AND NOT granted)", (pid,)).fetchone()[0]:
                    assert time.monotonic() < deadline, "旧补录未进入预期的快照等待点"
                    time.sleep(0.01)
                _submit(state, conversation, "second-followup")
                assert _refresh(state, conversation) is True
                assert _count(state, conversation) == 3
            finally:
                blocker.execute("SELECT pg_advisory_unlock(91741, 611)")
            future.result(timeout=5)
    assert _count(state, conversation) == 3
    assert _backfill(state) == 0


def test_usage_backfill_advances_past_full_batch_of_up_to_date_rows(usage_database):
    state = usage_database
    # 大批量边界用数据库夹具；用户输入与追问路径由上面的 Store 用例覆盖。
    with state.repository.database_transaction() as db:
        db.execute("""INSERT INTO agent_conversations
            (conversation_id,request_id,epoch,status,body,owner_key,current_run_id,updated_at)
            SELECT 'batch-' || lpad(n::text,4,'0'), 'batch-' || n, 'usage-regression', 'COMPLETED',
                jsonb_build_object('created_at', ?::text, 'completed_at', ?::text)::text,
                ?, 'run-' || lpad(n::text,4,'0'), ? FROM generate_series(1,1001) AS n""", (NOW, NOW, OWNER, NOW))
        db.execute("""INSERT INTO agent_conversation_runs(run_id,conversation_id,ordinal,epoch,status,body)
            SELECT current_run_id,conversation_id,1,epoch,status,body FROM agent_conversations""")
        db.execute("""INSERT INTO agent_messages
            (message_id,conversation_id,request_id,fingerprint,body,receipt,run_id)
            SELECT 'message-' || conversation_id,conversation_id,'initial','fingerprint','{}','{}',current_run_id
            FROM agent_conversations""")
    assert _backfill(state) == 1000
    assert _backfill(state) == 1
    assert _backfill(state) == 0
    with state.repository.database_transaction() as db:
        db.execute("UPDATE agent_run_usage_stats SET message_count=0 WHERE conversation_id='batch-1001'")
    assert _backfill(state) == 1
    assert _backfill(state) == 0
    with state.repository.database_read() as db:
        assert db.execute("SELECT count(*),sum(message_count) FROM agent_run_usage_stats").fetchone() == (1001, 1001)


@pytest.mark.parametrize("remove_run", [False, True])
def test_usage_source_cleanup_preserves_recorded_message_count(usage_database, remove_run):
    state = usage_database
    conversation = _conversation(state)
    _cancel(state, conversation, _submit(state, conversation, "first-followup"))
    _submit(state, conversation, "second-followup")
    assert _refresh(state, conversation) is True
    assert _count(state, conversation) == 3
    with state.repository.database_transaction() as db:
        db.execute("DELETE FROM agent_messages WHERE conversation_id=?", (conversation.conversation_id,))
        db.execute("DELETE FROM agent_followup_tasks WHERE conversation_id=?", (conversation.conversation_id,))
        if remove_run:
            db.execute("DELETE FROM agent_conversation_runs WHERE conversation_id=?", (conversation.conversation_id,))
            db.execute("DELETE FROM agent_conversations WHERE conversation_id=?", (conversation.conversation_id,))
    assert _refresh(state, conversation) is True
    assert _backfill(state) == 0
    assert _count(state, conversation) == 3
