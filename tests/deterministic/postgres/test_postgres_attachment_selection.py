"""Selection rechecks use the same bounded query on the production adapter."""
import pytest

from problem_locator.agent.store import AgentStore
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent.test_attachment_selection_performance import (
    test_attachment_commit_rechecks_only_the_latest_selection_without_loading_history,
)


@pytest.fixture
def store(postgres_repository):
    return AgentStore(postgres_repository, runtime_epoch="postgres-attachment-selection")
