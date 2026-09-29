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
