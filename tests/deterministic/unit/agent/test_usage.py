"""Temporary log cleanup never reports a permanently saved chat as deleted."""
from __future__ import annotations

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.usage import ConversationUsageGuard


@pytest.mark.parametrize("temporary,status,retryable", [(True, 503, True), (False, 404, False)])
def test_cleanup_blocks_new_users_with_the_correct_retry_semantics(temporary, status, retryable):
    usage = ConversationUsageGuard()
    lease = usage.acquire_cleanup_if_idle("conversation", temporary=temporary)
    assert lease is not None
    with lease:
        with pytest.raises(AgentStoreError) as error, usage.acquire("conversation"):
            pytest.fail("cleanup must exclude new operations")
        assert error.value.status_code == status
        assert error.value.retryable is retryable
        if temporary:
            assert error.value.code == "AGENT_RESOURCES_CLEANING"
        assert usage.acquire_cleanup_if_idle("conversation", temporary=temporary) is None
    with usage.acquire("conversation"):
        assert usage.active("conversation")
        assert usage.acquire_cleanup_if_idle("conversation", temporary=temporary) is None
    assert not usage.active("conversation")


def test_temporary_cleanup_failure_releases_exclusion_and_does_not_change_delete_errors():
    usage = ConversationUsageGuard()
    with pytest.raises(OSError), usage.acquire_cleanup_if_idle("conversation", temporary=True):
        raise OSError("simulated delete failure")
    with usage.acquire_cleanup_if_idle("conversation"):
        with pytest.raises(AgentStoreError) as error, usage.acquire("conversation"):
            pytest.fail("explicit deletion must still exclude access")
        assert error.value.status_code == 404 and not error.value.retryable
