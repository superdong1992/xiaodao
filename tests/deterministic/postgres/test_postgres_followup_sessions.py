"""Run native session transaction assertions against real PostgreSQL."""
from types import SimpleNamespace

import pytest

from tests.deterministic.postgres.test_followup_database_concurrency import followup_database
from tests.deterministic.unit.agent import test_followup_sessions_store as shared
from tests.postgres_helpers import postgres_database_url, postgres_repository


@pytest.fixture
def system(followup_database):
    state = followup_database
    created = state.agent.create_conversation("session", owner_key=shared.OWNER)
    return SimpleNamespace(repository=state.repository, agent=state.agent, store=state.followups,
        sessions=state.followups.sessions, source=state.source, cid=created.conversation_id,
        rid=created.run_id, clock=state.followups.clock)


def test_postgres_session_checkpoints_and_resumes_after_store_restart(system):
    shared.test_completed_turn_checkpoints_once_and_resumes_after_store_restart(system)


def test_postgres_session_history_uses_storage_order_across_reports(system):
    shared.test_history_uses_conversation_storage_order_across_reports_and_excludes_other_conversations(system)


def test_postgres_session_answer_and_checkpoint_roll_back_together(system, monkeypatch):
    shared.test_answer_and_checkpoint_roll_back_together_on_database_failure(system, monkeypatch)


def test_postgres_session_rejects_callback_after_stop(system):
    shared.test_late_init_callback_is_rejected(system, "cancel")
