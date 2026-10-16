"""Answer-target feedback and correction replacement invariants."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.entrypoints.postgres_import import _projection_defaults, _source_schema, PostgresImportError
from problem_locator.memory.models import FeedbackView, FollowupFeedbackView
from problem_locator.memory.service import FeedbackService
from problem_locator.memory.store import LEGACY_DEFAULTS, MemoryStore
from tests.deterministic.unit.agent.test_generic_feedback import (
    CARD, OWNER, OTHER, put, system, task_rows)
from tests.deterministic.unit.agent.test_report_followup import system as followup_system

FID = "00000000-0000-0000-0000-000000000021"
OTHER_FID = "00000000-0000-0000-0000-000000000022"
EVIDENCE = json.dumps({"evidence": [{"sha256": "a" * 64, "line_start": 1, "line_end": 2}]})


def answer_source(system, *, correction=False, fid=FID, target=""):
    answer = "先检查连接池等待时长，再核对队列长度。"
    return replace(system.source, answer_key=fid, source_kind="CORRECTION" if correction else "FOLLOWUP",
        report_markdown=answer, report_sha256=hashlib.sha256(answer.encode()).hexdigest(),
        context_json=json.dumps({"original_problem": system.source.problem_text,
            "original_report": system.source.report_markdown, "prior_turns": [],
            "question": "请检查排队方向", "answer": answer, "correction": {}}, ensure_ascii=False),
        replaces_answer_key=target if correction else None)


def answer_vote(system, source, request="answer-up", rating="LIKE"):
    return system.memory.put_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, request_id=request, rating=rating, source=source, answer_key=source.answer_key)


def enqueue(system, source):
    with system.repository.database_transaction() as db:
        return system.memory.enqueue_correction_in_transaction(db,
            system.created.conversation_id, system.created.run_id, source)


def test_report_and_each_answer_keep_independent_votes_and_replays(system):
    report = put(system, rating="DISLIKE")
    first = answer_source(system)
    second = answer_source(system, fid=OTHER_FID)
    assert answer_vote(system, first).rating == "LIKE"
    assert answer_vote(system, second, "other-up").followup_id == OTHER_FID
    down = answer_vote(system, first, "answer-down", "DISLIKE")
    assert answer_vote(system, first) == down
    assert put(system, rating="DISLIKE") == report
    with pytest.raises(AgentStoreError) as conflict:
        answer_vote(system, second)
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    with pytest.raises(AgentStoreError):
        answer_vote(system, replace(first, report_sha256="a" * 64), "changed")
    assert set(report.model_dump()) == {"schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"}
    assert isinstance(report, FeedbackView) and not isinstance(report, FollowupFeedbackView)
    assert len(task_rows(system)) == 2


def test_correction_publication_without_vote_supersedes_inflight_original_permanently(system):
    put(system)
    old = system.memory.claim_task()
    correction = answer_source(system, correction=True)
    assert enqueue(system, correction)
    assert not enqueue(system, correction)
    new = system.memory.claim_task()
    assert new["source_kind"] == "CORRECTION"
    assert system.memory.finish_task(new["task_id"], CARD, evidence_json=EVIDENCE)
    assert system.memory.finish_task(old["task_id"], CARD)
    assert [row["answer_key"] for row in system.memory.active_cards(system.source.skill_name)] == [FID]
    put(system, "down", "DISLIKE")
    put(system, "relike")
    assert [row["answer_key"] for row in system.memory.active_cards(system.source.skill_name)] == [FID]
    answer_vote(system, correction, "correction-down", "DISLIKE")
    assert system.memory.active_cards(system.source.skill_name) == []
    put(system, "original-up-again")
    assert system.memory.active_cards(system.source.skill_name) == []
    answer_vote(system, correction, "correction-up")
    assert [row["answer_key"] for row in system.memory.active_cards(system.source.skill_name)] == [FID]
    assert system.memory.claim_task() is None


def test_correction_fences_original_that_had_no_extraction_task(system):
    correction = answer_source(system, correction=True)
    enqueue(system, correction)
    accepted = system.memory.claim_task()
    system.memory.finish_task(accepted["task_id"], CARD, evidence_json=EVIDENCE)
    put(system)
    assert system.memory.claim_task() is None
    assert [row["answer_key"] for row in system.memory.active_cards(system.source.skill_name)] == [FID]


def test_late_disliked_correction_stays_disabled_and_no_evidence_cannot_publish(system):
    correction = answer_source(system, correction=True)
    enqueue(system, correction)
    task = system.memory.claim_task()
    answer_vote(system, correction, "down", "DISLIKE")
    with pytest.raises(ValueError, match="证据"):
        system.memory.finish_task(task["task_id"], CARD)
    assert system.memory.finish_task(task["task_id"], CARD, evidence_json=EVIDENCE)
    assert system.memory.active_cards(system.source.skill_name) == []
    with system.repository.database_read() as db:
        assert db.execute("SELECT context_json,problem_text,report_markdown FROM memory_tasks").fetchone() == (None, None, None)


def test_failed_correction_is_never_reclassified_or_requeued_by_like(system):
    correction = answer_source(system, correction=True)
    enqueue(system, correction)
    task = system.memory.claim_task()
    system.memory.fail_task(task["task_id"])
    answer_vote(system, correction)
    assert system.memory.claim_task() is None
    with pytest.raises(AgentStoreError):
        answer_vote(system, replace(correction, source_kind="FOLLOWUP"), "bypass")


def test_targeted_followup_replacement_does_not_change_other_answer_or_report_votes(system):
    put(system)
    original = system.memory.claim_task()
    system.memory.finish_task(original["task_id"], CARD)
    earlier = answer_source(system)
    answer_vote(system, earlier)
    task = system.memory.claim_task()
    system.memory.finish_task(task["task_id"], CARD)
    correction = answer_source(system, fid=OTHER_FID, correction=True, target=FID)
    enqueue(system, correction)
    accepted = system.memory.claim_task()
    system.memory.finish_task(accepted["task_id"], CARD, evidence_json=EVIDENCE)
    assert {row["answer_key"] for row in system.memory.active_cards(system.source.skill_name)} == {"", OTHER_FID}
    assert answer_vote(system, earlier).rating == "LIKE"
    assert put(system).rating == "LIKE"


def test_correction_deletion_and_restart_do_not_publish(system):
    correction = answer_source(system, correction=True)
    enqueue(system, correction)
    task = system.memory.claim_task()
    reopened = MemoryStore(system.repository, system.clock)
    assert reopened.recover() == 1
    assert reopened.claim_task() is None
    assert not reopened.finish_task(task["task_id"], CARD, evidence_json=EVIDENCE)
    other = answer_source(system, fid=OTHER_FID, correction=True)
    enqueue(system, other)
    task = system.memory.claim_task()
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert not reopened.finish_task(task["task_id"], CARD, evidence_json=EVIDENCE)
    with system.repository.database_read() as db:
        assert all(row == (None, None) for row in db.execute("SELECT context_json,evidence_json FROM memory_tasks"))


def test_legacy_schema_migration_preserves_report_votes_receipts_and_card_bytes(system):
    put(system)
    task = system.memory.claim_task()
    system.memory.finish_task(task["task_id"], CARD)
    before = system.memory.active_cards(system.source.skill_name)
    with system.repository.database_transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='memory_storage_version'")
        for table, defaults in LEGACY_DEFAULTS.items():
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] not in defaults]
            names = ",".join(columns)
            db.execute(f"CREATE TABLE {table}_legacy AS SELECT {names} FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_legacy RENAME TO {table}")
    migrated = MemoryStore(system.repository, system.clock)
    assert migrated.active_cards(system.source.skill_name) == before
    system.memory = migrated
    put(system, "down", "DISLIKE")
    assert put(system).rating == "DISLIKE"
    answer_vote(system, answer_source(system))
    assert len(task_rows(system)) == 2
    assert MemoryStore(system.repository, system.clock).get_feedback(system.created.conversation_id,
        system.created.run_id, owner_key=OWNER, can_rate=True).rating == "DISLIKE"


def test_import_projection_only_accepts_explicit_legacy_defaults():
    assert _projection_defaults("memory_feedback", ["run_id"], {"run_id", "answer_key"}) == {"answer_key": ""}
    assert _projection_defaults("agent_followup_tasks", ["body"], {"body", "correction_json", "storage_order"}) == {"correction_json": "{}"}
    with pytest.raises(PostgresImportError):
        _projection_defaults("memory_tasks", ["task_id"], {"task_id", "answer_key"})
    with pytest.raises(PostgresImportError):
        _projection_defaults("agent_messages", ["body"], {"body", "new_unrecognized_column"})


def test_feedback_service_checks_completed_owner_same_run_and_keeps_specialized_out_of_learning(followup_system):
    state = followup_system
    memory = MemoryStore(state.repository, state.clock)
    state.agent.followups = state.service
    feedback = FeedbackService(state.agent, memory, memory_enabled=True)
    state.source = replace(state.source, skill_name="generic-test")
    with state.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (state.source.case_id, state.rid))
    receipt = state.service.submit(state.cid, state.rid, "first", "为什么排队？", owner_key=OWNER)
    fid = receipt.followup_id
    assert not feedback.get_followup_feedback(state.cid, state.rid, fid, owner_key=OWNER).can_rate
    with pytest.raises(AgentStoreError) as pending:
        feedback.put_followup_feedback(state.cid, state.rid, fid, "up", "LIKE", owner_key=OWNER)
    assert pending.value.code == "AGENT_FEEDBACK_UNSUPPORTED"
    claimed = state.service.store.claim_task()
    assert state.service.store.finish_task(claimed["followup_id"], answer="连接池排队需要核对当前日志。")
    with pytest.raises(AgentStoreError) as foreign:
        feedback.get_followup_feedback(state.cid, state.rid, fid, owner_key=OTHER)
    assert foreign.value.status_code == 404
    with pytest.raises(AgentStoreError):
        feedback.get_followup_feedback(state.cid, state.rid, OTHER_FID, owner_key=OWNER)
    assert state.service.get(state.cid, state.rid, owner_key=OWNER).items[0].status == "COMPLETED", state.service.get(state.cid, state.rid, owner_key=OWNER).items[0]
    up = feedback.put_followup_feedback(state.cid, state.rid, fid, "up", "LIKE", owner_key=OWNER)
    assert up.can_rate and up.followup_id == fid
    task = memory.claim_task()
    assert task["source_kind"] == "FOLLOWUP"
    context = json.loads(task["context_json"])
    assert context["question"] == "为什么排队？" and context["original_report"] == state.source.report_markdown
    assert state.backend.calls == []
    with state.repository.database_transaction() as db:
        source = json.loads(db.execute("SELECT source_json FROM agent_followup_snapshots WHERE run_id=?", (state.rid,)).fetchone()[0])
        source["source_kind"] = "SKILL_DIRECT"
        db.execute("UPDATE agent_followup_snapshots SET source_json=? WHERE run_id=?", (json.dumps(source), state.rid))
    second = state.service.submit(state.cid, state.rid, "second", "再解释一次", owner_key=OWNER)
    claimed = state.service.store.claim_task()
    assert state.service.store.finish_task(claimed["followup_id"], answer="连接池排队需要核对当前日志。")
    feedback.put_followup_feedback(state.cid, state.rid, second.followup_id, "second-up", "LIKE", owner_key=OWNER)
    assert memory.claim_task() is None

@pytest.mark.parametrize("mode", ["marked_missing", "mixed_columns"])
def test_migration_rejects_inconsistent_storage_without_recreating_tables(system, mode):
    with system.repository.database_transaction() as db:
        if mode == "marked_missing":
            for table in LEGACY_DEFAULTS:
                db.execute(f"DROP TABLE {table}")
        else:
            db.execute("DELETE FROM metadata WHERE key='memory_storage_version'")
            db.execute("ALTER TABLE memory_feedback RENAME TO memory_feedback_bad")
            db.execute("CREATE TABLE memory_feedback AS SELECT run_id,conversation_id,owner_key,source_job_id,skill_name,report_sha256,rating,updated_at FROM memory_feedback_bad")
            db.execute("DROP TABLE memory_feedback_bad")
    with pytest.raises(AgentStoreError) as error:
        MemoryStore(system.repository, system.clock)
    assert error.value.code == "STATE_SCHEMA_UNSUPPORTED"
    if mode == "marked_missing":
        with system.repository.database_read() as db:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='memory_tasks'").fetchall()
            with pytest.raises(PostgresImportError):
                _source_schema(db)


def test_accepted_correction_cancels_pending_old_extraction_without_model_work(system):
    put(system)
    correction = answer_source(system, correction=True)
    enqueue(system, correction)
    with system.repository.database_transaction() as db:
        db.execute("UPDATE memory_tasks SET status='RUNNING' WHERE answer_key=?", (FID,))
        fid = db.execute("SELECT task_id FROM memory_tasks WHERE answer_key=?", (FID,)).fetchone()[0]
    assert system.memory.finish_task(fid, CARD, evidence_json=EVIDENCE)
    assert system.memory.claim_task() is None
    with system.repository.database_read() as db:
        assert db.execute("SELECT status,superseded,problem_text,report_markdown,context_json FROM memory_tasks WHERE answer_key='' ").fetchone() == ("FAILED", 1, None, None, None)


@pytest.mark.parametrize("matching", [True, False])
def test_legacy_followup_like_recovers_skill_only_from_identical_original_report(followup_system, matching):
    state = followup_system
    memory = MemoryStore(state.repository, state.clock)
    state.agent.followups = state.service
    feedback = FeedbackService(state.agent, memory, memory_enabled=True)
    from problem_locator.memory.models import FeedbackSource
    original = FeedbackSource(case_id=state.source.case_id, source_job_id=state.source.source_job_id,
        skill_name="historical-generic-skill", problem_text=state.source.problem_text,
        report_markdown=state.source.report_markdown, report_sha256=state.source.report_sha256)
    feedback._source = lambda cid, rid: original if matching else replace(original, report_sha256="a" * 64)
    with state.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (state.source.case_id, state.rid))
    receipt = state.service.submit(state.cid, state.rid, "legacy", "还有哪些排查步骤？", owner_key=OWNER)
    with state.repository.database_transaction() as db:
        source = json.loads(db.execute("SELECT source_json FROM agent_followup_snapshots WHERE run_id=?", (state.rid,)).fetchone()[0])
        source.pop("skill_name", None)
        db.execute("UPDATE agent_followup_snapshots SET source_json=? WHERE run_id=?", (json.dumps(source), state.rid))
    task = state.service.store.claim_task()
    assert state.service.store.finish_task(task["followup_id"], answer="先核对连接池等待时间。")
    assert feedback.put_followup_feedback(state.cid, state.rid, receipt.followup_id, "up", "LIKE", owner_key=OWNER).rating == "LIKE"
    task = memory.claim_task()
    if matching:
        assert task["skill_name"] == "historical-generic-skill"
    else:
        assert task is None
