"""Bounded history SQL and pagination on the production PostgreSQL adapter."""
import pytest

from problem_locator.agent.store import AgentStore
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent.test_history_query_count import (
    test_history_sql_count_is_bounded_for_one_fifty_and_hundred_entries,
    test_batched_history_preserves_mixed_pages_and_conversation_boundaries,
    test_batched_history_never_substitutes_missing_or_foreign_dependencies,
    test_history_does_not_hydrate_the_extra_cursor_row_or_unreferenced_messages,
    test_empty_history_uses_one_query,
)


@pytest.fixture
def store(postgres_repository):
    return AgentStore(postgres_repository, runtime_epoch="postgres-history-query-count")
