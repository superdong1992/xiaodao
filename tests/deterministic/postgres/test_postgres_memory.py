"""Feedback and extraction invariants on the production PostgreSQL adapter."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from problem_locator.memory import store as memory_module
from problem_locator.storage.database import lock_conversation
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.storage.fakes import FixedClock
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID
from tests.deterministic.unit.runtime.test_experience_memory import (
    task, card_json,
    test_durable_switch_during_model_and_replay_reuse_only_one_call,
    test_durable_delete_during_model_cannot_publish_or_reactivate,
    test_durable_recovery_never_reexecutes_uncertain_claim,
    test_durable_natural_report_retention_keeps_abstract_card,
)


@pytest.fixture
def durable_memory(postgres_repository):
    clock = FixedClock("2026-09-21T00:00:00.000Z")
    agents = AgentStore(postgres_repository, clock=clock, runtime_epoch="pg-memory")
    memory = MemoryStore(postgres_repository, clock=clock)
    agents.memory_store = memory
    created = agents.create_conversation("create", owner_key="owner")
    agents.bind_case(created.conversation_id, CASE_ID)
    content = task()
    source = FeedbackSource(case_id=CASE_ID, source_job_id=JOB_ID, skill_name="generic",
        problem_text=content["problem_text"], report_markdown=content["report_markdown"],
        report_sha256=content["report_sha256"])

    def vote(rating, request_id):
        return memory.put_feedback(created.conversation_id, created.run_id, owner_key="owner",
            request_id=request_id, rating=rating, source=source)

    return SimpleNamespace(repository=postgres_repository, agents=agents, memory=memory,
        created=created, source=source, vote=vote, clock=clock)


def test_postgres_memory_claim_does_not_deadlock_with_deleting_conversation(durable_memory):
    state = durable_memory
    state.vote("LIKE", "like")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with state.repository.database_transaction() as db:
            lock_conversation(db, state.created.conversation_id)
            assert executor.submit(state.memory.claim_task).result(timeout=5) is None
            state.memory.revoke_conversation(db, state.created.conversation_id)
    assert state.memory.claim_task() is None


def test_postgres_memory_concurrent_claims_never_duplicate_work(durable_memory):
    state = durable_memory
    state.vote("LIKE", "like")
    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(lambda _: state.memory.claim_task(), range(6)))
    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert state.memory.finish_task(claimed[0]["task_id"], card_json()) is True
    state.agents.request_delete(state.created.conversation_id, owner_key="owner")
    assert state.memory.active_cards("generic") == []


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_postgres_feedback_survives_disabled_or_full_extraction(durable_memory, monkeypatch, memory_enabled):
    state = durable_memory
    monkeypatch.setattr(memory_module, "MAX_TASKS", 0)
    for request_id, rating, expected in [("up", "LIKE", "LIKE"), ("down", "DISLIKE", "DISLIKE"),
                                         ("up", "LIKE", "DISLIKE")]:
        result = state.memory.put_feedback(state.created.conversation_id, state.created.run_id,
            owner_key="owner", request_id=request_id, rating=rating, source=state.source,
            extract_memory=memory_enabled)
        assert result.can_rate is True and result.rating == expected
    assert state.memory.claim_task() is None


def test_postgres_memory_legacy_migration_preserves_reports_and_request_replay(durable_memory):
    from problem_locator.memory.store import LEGACY_DEFAULTS
    from problem_locator.storage.database import table_columns
    state = durable_memory
    state.vote("LIKE", "up")
    task = state.memory.claim_task()
    state.memory.finish_task(task["task_id"], card_json())
    before = state.memory.active_cards(state.source.skill_name)
    with state.repository.database_transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='memory_storage_version'")
        for table, defaults in LEGACY_DEFAULTS.items():
            names = ",".join(sorted(table_columns(db, table) - set(defaults)))
            db.execute(f"CREATE TABLE {table}_legacy AS SELECT {names} FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_legacy RENAME TO {table}")
    migrated = MemoryStore(state.repository, state.clock)
    assert migrated.active_cards(state.source.skill_name) == before
    state.vote("DISLIKE", "down")
    assert state.vote("LIKE", "up").rating == "DISLIKE"
    assert migrated.claim_task() is None


def test_postgres_correction_and_feedback_share_task_quota(durable_memory, monkeypatch):
    import hashlib
    import json
    import threading
    from dataclasses import replace
    state = durable_memory
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    other = state.agents.create_conversation("other", owner_key="other-owner")
    other_case = "00000000-0000-0000-0000-000000000099"
    with state.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (other_case, other.run_id))
    answer = "先核对队列长度和连接池等待时间。"
    correction = replace(state.source, answer_key="00000000-0000-0000-0000-000000000021",
        source_kind="CORRECTION", replaces_answer_key="", report_markdown=answer,
        report_sha256=hashlib.sha256(answer.encode()).hexdigest(), context_json=json.dumps({"question": "纠正排队原因"}))
    barrier = threading.Barrier(2)
    def enqueue():
        barrier.wait(timeout=5)
        with state.repository.database_transaction() as db:
            lock_conversation(db, state.created.conversation_id)
            return state.memory.enqueue_correction_in_transaction(db, state.created.conversation_id, state.created.run_id, correction)
    def vote():
        barrier.wait(timeout=5)
        return state.memory.put_feedback(other.conversation_id, other.run_id, owner_key="other-owner", request_id="up",
            rating="LIKE", source=replace(state.source, case_id=other_case))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(enqueue), executor.submit(vote)
        first.result(timeout=10)
        assert second.result(timeout=10).rating == "LIKE"
    with state.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 1


def test_postgres_accepted_correction_and_late_original_completion_are_serialized(durable_memory):
    import hashlib
    import json
    from dataclasses import replace
    state = durable_memory
    state.vote("LIKE", "up")
    old = state.memory.claim_task()
    answer = "先核对队列长度和连接池等待时间。"
    fid = "00000000-0000-0000-0000-000000000021"
    correction = replace(state.source, answer_key=fid, source_kind="CORRECTION", replaces_answer_key="",
        report_markdown=answer, report_sha256=hashlib.sha256(answer.encode()).hexdigest(), context_json="{}")
    with state.repository.database_transaction() as db:
        lock_conversation(db, state.created.conversation_id)
        assert state.memory.enqueue_correction_in_transaction(db, state.created.conversation_id, state.created.run_id, correction)
    new = state.memory.claim_task()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(state.memory.finish_task, old["task_id"], card_json())
        second = executor.submit(state.memory.finish_task, new["task_id"], card_json(), evidence_json=json.dumps({"validated": True}))
        assert first.result(timeout=10) and second.result(timeout=10)
    assert [row["answer_key"] for row in state.memory.active_cards(state.source.skill_name)] == [fid]
    state.vote("DISLIKE", "down")
    state.vote("LIKE", "relike")
    assert [row["answer_key"] for row in state.memory.active_cards(state.source.skill_name)] == [fid]
