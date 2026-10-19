"""Recall source fences use fresh PostgreSQL state, including paginated revoke."""
from tests.deterministic.postgres.test_postgres_permanent_memory import (
    system, postgres_database_url, postgres_repository,
)
from tests.deterministic.unit.agent.test_memory_recall_source import (
    test_final_source_check_requires_current_state_and_exact_content,
    test_final_source_check_observes_vote_switches_and_explicit_delete,
    test_source_revoked_between_pages_is_not_returned_from_cached_content,
)
