"""Cross-report memory corrections retain identity and revocation on PostgreSQL."""
from tests.deterministic.postgres.test_postgres_permanent_memory import (
    system, postgres_database_url, postgres_repository,
)
from tests.deterministic.unit.agent.test_memory_revision_store import (
    test_usage_is_immutable_skill_scoped_and_survives_restart,
    test_plain_dislike_does_not_revoke_used_history,
    test_verified_global_error_fences_duplicates_future_cards_and_relikes,
    test_scoped_correction_overrides_only_matching_conditions,
    test_forged_review_cannot_revoke_other_history,
    test_old_feedback_late_result_cannot_revoke_used_history,
    test_deleted_usage_source_cannot_publish_late_revision,
    test_specialized_execution_never_records_usage,
    test_global_rejection_survives_correction_source_deletion,
    test_global_rejection_isolated_by_skill,
    test_review_without_actual_use_is_not_authorized,
    test_unrelated_feedback_keeps_historical_card_and_creates_no_revision,
    test_usage_survives_natural_resource_expiry,
    test_recall_revision_metadata_does_not_add_per_card_queries,
)
