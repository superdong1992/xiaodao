"""Standalone negative comments never create chat or mutate target votes."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.memory.models import FeedbackCommentRequest
from problem_locator.memory.store import MemoryStore
from problem_locator.memory.comments import COMMENT_TABLE, COMMENT_VERSION_KEY
from problem_locator.entrypoints.postgres_import import _source_schema, PostgresImportError
from tests.deterministic.unit.agent.test_generic_feedback import CARD, OWNER, OTHER, system, put, task_rows
from tests.deterministic.unit.agent.test_answer_feedback_memory import answer_source, answer_vote, FID, OTHER_FID, EVIDENCE


def comment(state, request_id="comment", *, source=None, **kwargs):
    source = source or state.source
    payload = {"text": "连接池排队才是需要检查的方向。", **kwargs}
    return state.memory.post_feedback_comment(state.created.conversation_id, state.created.run_id,
        owner_key=OWNER, request_id=request_id, source=source, answer_key=source.answer_key, **payload)


@pytest.mark.parametrize("payload", [
    {"text": "用户意见"}, {"analysis_text": "管理员检查发现连接池持续排队。"},
    {"text": "遗漏排队", "analysis_text": "观测到等待线程增长。", "analysis_reference": "web-123"}])
def test_comment_accepts_independent_sources_without_creating_followup(system, payload):
    vote = put(system, rating="DISLIKE")
    receipt = comment(system, text=None, **payload) if "text" not in payload else comment(system, **payload)
    assert receipt.followup_id is None and receipt.request_id == "comment"
    assert put(system, rating="DISLIKE") == vote
    task = system.memory.claim_task()
    assert task["answer_key"] == "feedback:" + receipt.comment_id
    context = json.loads(task["context_json"])
    assert context["origin"] == "FEEDBACK_COMMENT" and "answer" not in context
    assert context["feedback"]["text"] == payload.get("text")
    assert context["feedback"]["analysis_text"] == payload.get("analysis_text")
    assert context["target_answer_sha256"] == system.source.report_sha256
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_messages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_dispatches").fetchone()[0] == 0


def test_same_request_returns_receipt_after_vote_change_without_new_task(system):
    put(system, rating="DISLIKE")
    first = comment(system)
    put(system, "later-like", "LIKE", extract_memory=False)
    assert comment(system) == first
    assert len(task_rows(system)) == 1
    with pytest.raises(AgentStoreError) as conflict:
        comment(system, analysis_text="改变资料")
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    with pytest.raises(AgentStoreError) as requires:
        comment(system, "new-request")
    assert requires.value.code == "AGENT_FEEDBACK_REQUIRES_DISLIKE"
    assert system.memory.get_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, can_rate=True).rating == "LIKE"


@pytest.mark.parametrize("vote", [None, "LIKE"])
def test_comment_requires_existing_dislike(system, vote):
    if vote:
        put(system, rating=vote, extract_memory=False)
    with pytest.raises(AgentStoreError) as error:
        comment(system)
    assert error.value.code == "AGENT_FEEDBACK_REQUIRES_DISLIKE"
    assert task_rows(system) == []


def test_comments_cannot_cross_owner_run_or_answer(system):
    put(system, rating="DISLIKE")
    first = comment(system)
    for owner, rid in ((OTHER, system.created.run_id), (OWNER, "00000000-0000-0000-0000-000000000099")):
        with pytest.raises(AgentStoreError) as error:
            system.memory.post_feedback_comment(system.created.conversation_id, rid, owner_key=owner,
                request_id="comment", text="连接池排队才是需要检查的方向。", source=system.source)
        assert error.value.status_code == 404
    answer = answer_source(system)
    answer_vote(system, answer, rating="DISLIKE")
    with pytest.raises(AgentStoreError) as error:
        comment(system, source=answer)
    assert error.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    second = comment(system, "answer-comment", source=answer)
    assert first.followup_id is None and second.followup_id == FID
    assert len(task_rows(system)) == 2


def test_concurrent_comment_replays_enqueue_exactly_once(system):
    put(system, rating="DISLIKE")
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: comment(system), range(4)))
    assert all(value == receipts[0] for value in receipts)
    assert len(task_rows(system)) == 1


@pytest.mark.parametrize("enabled,specialized", [(False, False), (True, True)])
def test_learning_off_or_specialized_records_without_learning(system, enabled, specialized):
    source = replace(system.source, problem_text="") if specialized else system.source
    put(system, rating="DISLIKE")
    comment(system, source=source, extract_memory=enabled)
    assert task_rows(system) == []
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback_comments").fetchone()[0] == 1


def test_enqueue_failure_keeps_feedback_and_rolls_back_only_optional_task(system, monkeypatch):
    put(system, rating="DISLIKE")
    original = system.memory._enqueue
    def fail(*args):
        original(*args)
        raise RuntimeError("private source should never escape")
    monkeypatch.setattr(system.memory, "_enqueue", fail)
    receipt = comment(system)
    assert comment(system) == receipt
    assert task_rows(system) == []


def test_restart_and_deletion_do_not_repeat_or_publish_comment(system):
    put(system, rating="DISLIKE")
    receipt = comment(system, analysis_text="私有管理员分析")
    reopened = MemoryStore(system.repository, system.clock)
    assert reopened.recover() == 0
    task = reopened.claim_task()
    assert task is not None
    assert reopened.recover() == 1
    assert reopened.claim_task() is None
    assert comment(system, analysis_text="私有管理员分析") == receipt
    comment(system, "next")
    task = reopened.claim_task()
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert not reopened.finish_task(task["task_id"], CARD, evidence_json=EVIDENCE)
    with system.repository.database_read() as db:
        assert all(row == (None, None, None) for row in db.execute(
            "SELECT text,analysis_text,analysis_reference FROM memory_feedback_comments"))
    system.agent_store.finish_cleanup(system.created.conversation_id)
    reopened.prune()
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback_comments").fetchone()[0] == 0


def test_raw_retention_keeps_immutable_idempotency_receipt(system):
    put(system, rating="DISLIKE")
    receipt = comment(system, analysis_text="私有分析", analysis_reference="web-123")
    system.clock.value = "2026-09-29T00:00:00.000Z"
    system.memory.prune()
    with system.repository.database_read() as db:
        assert db.execute("SELECT text,analysis_text,analysis_reference FROM memory_feedback_comments").fetchone() == (None, None, None)
    assert comment(system, analysis_text="私有分析", analysis_reference="web-123") == receipt
    assert system.memory.claim_task() is None


def test_newer_comment_prevents_late_old_adoption_and_supersedes_old_cards(system):
    put(system, rating="DISLIKE")
    comment(system)
    old = system.memory.claim_task()
    comment(system, "newer", text="新的排查过程")
    new = system.memory.claim_task()
    assert not system.memory.finish_task(old["task_id"], CARD, evidence_json=EVIDENCE)
    assert system.memory.finish_task(new["task_id"], CARD, evidence_json=EVIDENCE)
    assert [x["card_id"] for x in system.memory.active_cards(system.source.skill_name)] == [new["task_id"]]
    comment(system, "newest", text="进一步确认的方法")
    newest = system.memory.claim_task()
    assert system.memory.finish_task(newest["task_id"], CARD, evidence_json=EVIDENCE)
    assert [x["card_id"] for x in system.memory.active_cards(system.source.skill_name)] == [newest["task_id"]]
    put(system, "relike")
    assert system.memory.claim_task() is None


def test_new_table_upgrade_preserves_old_feedback_and_rejects_missing_marker(system):
    vote = put(system, rating="DISLIKE")
    with system.repository.database_transaction() as db:
        db.execute("DROP TABLE memory_feedback_comments")
        db.execute("DELETE FROM metadata WHERE key=?", (COMMENT_VERSION_KEY,))
    system.memory = MemoryStore(system.repository, system.clock)
    assert put(system, rating="DISLIKE") == vote
    comment(system)
    with system.repository.database_read() as db:
        tables, _ = _source_schema(db)
        assert COMMENT_TABLE in tables
    with system.repository.database_transaction() as db:
        db.execute("DELETE FROM metadata WHERE key=?", (COMMENT_VERSION_KEY,))
    with pytest.raises(AgentStoreError):
        MemoryStore(system.repository, system.clock)
    with system.repository.database_read() as db, pytest.raises(PostgresImportError):
        _source_schema(db)


def test_comment_capacity_still_allows_existing_receipt(system, monkeypatch):
    from problem_locator.memory import comments
    put(system, rating="DISLIKE")
    receipt = comment(system)
    monkeypatch.setattr(comments, "MAX_COMMENTS_PER_ANSWER", 1)
    assert comment(system) == receipt
    with pytest.raises(AgentStoreError) as error:
        comment(system, "extra")
    assert error.value.status_code == 429
