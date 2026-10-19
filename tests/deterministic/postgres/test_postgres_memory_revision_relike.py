"""Correction reactivation uses the same atomic publication rules on PostgreSQL."""
from tests.deterministic.postgres.test_postgres_permanent_memory import (
    system, postgres_database_url, postgres_repository,
)
from tests.deterministic.unit.agent.test_memory_revision_relike import (
    test_relike_publishes_inactive_correction_relation_once_without_new_learning,
    test_deleted_historical_source_cannot_be_reactivated_by_later_correction_like,
    test_superseded_inactive_correction_cannot_publish_its_old_relation_on_relike,
    test_deleting_correction_source_discards_unpublished_relation,
    test_repeated_vote_switch_reuses_published_revision_and_global_rejection_stays,
)
