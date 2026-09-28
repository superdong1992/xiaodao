"""Run durable Agent contracts and actual concurrent transactions on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.storage.database import lock_conversation
from tests.postgres_helpers import postgres_database_url, postgres_repository

# Reuse the same business assertions against the production database adapter.
from tests.deterministic.unit.agent.test_store import (
    test_create_and_message_retries_return_exact_receipt_even_after_close,
    test_concurrent_retries_publish_one_message_and_monotonic_events,
    test_first_applied_message_holds_questions_until_intake_finishes_once,
    test_intake_finish_cannot_cover_a_concurrently_received_message,
    test_intake_finish_uses_latest_authoritative_remaining_questions,
    test_intake_coverage_and_question_publication_roll_back_together,
    test_attachment_reservation_and_import_are_idempotent_with_atomic_quota,
    test_explicit_upload_retry_publishes_new_transitions_but_duplicate_does_not,
    test_expected_create_binding_exists_before_core_commit_returns,
    test_accepted_command_adopts_message_in_case_transaction_before_fast_terminal,
    test_failed_core_commit_rolls_back_message_adoption_with_case_state,
    test_terminal_case_snapshot_and_public_events_roll_back_together,
    test_safe_failure_never_exposes_internal_failure_and_marks_queued_unused,
    test_terminal_failure_snapshot_and_events_commit_atomically,
    test_notification_failure_cannot_reject_a_committed_message,
    test_dispatch_payload_is_frozen_and_not_exposed,
    test_report_is_published_before_archive_and_completion_waits,
)


@pytest.fixture
def store(postgres_repository):
    return AgentStore(postgres_repository, runtime_epoch="postgres-agent")


def test_postgres_different_conversation_commits_before_first_transaction_finishes(store):
    first = store.create_conversation("first")
    second = store.create_conversation("second")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.repository.database_transaction() as db:
            lock_conversation(db, first.conversation_id)
            receipt = executor.submit(store.submit_message, second.conversation_id,
                "second-message", "这个请求必须在首个事务提交前完成").result(timeout=5)
            assert receipt.event_id == 1
            assert store.list_events(second.conversation_id)[0].sequence == 1


def test_postgres_concurrent_unique_messages_have_no_lost_update_or_event_gap(store):
    conversation = store.create_conversation("sequence")
    barrier = threading.Barrier(8)

    def submit(index):
        barrier.wait(timeout=10)
        return store.submit_message(conversation.conversation_id, f"request-{index}", f"补充信息 {index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(submit, range(8)))
    assert sorted(receipt.event_id for receipt in receipts) == list(range(1, 9))
    view = store.get_conversation(conversation.conversation_id)
    assert len(view.messages) == 8 and view.last_event_id == 8
    assert [item.sequence for item in store.list_events(conversation.conversation_id)] == list(range(1, 9))


def test_postgres_directory_history_work_index_and_delete_keep_contract(store):
    owner = "a" * 64
    receipt = store.create_conversation("directory", owner_key=owner)
    store.submit_message(receipt.conversation_id, "question", "连接池排队")
    assert store.pending_conversations() == [receipt.conversation_id]
    directory = store.list_conversations(owner)
    assert directory.items[0].current_run.ordinal == 1
    assert directory.items[0].capabilities.can_send is True
    assert store.list_conversations(None).items == []
    store.fail_conversation(receipt.conversation_id)
    detail = store.read_conversation(receipt.conversation_id, history=True)
    assert any(item.type == "diagnosis.result" for item in detail.history)
    assert store.pending_conversations() == []
    store.request_delete(receipt.conversation_id, owner_key=owner)
    task = store.claim_cleanup()
    assert task["conversation_id"] == receipt.conversation_id
    assert store.claim_cleanup() is None
    store.finish_cleanup(receipt.conversation_id)
    with pytest.raises(AgentStoreError) as error:
        store.create_conversation("directory", owner_key=owner)
    assert error.value.code == "AGENT_CONVERSATION_NOT_FOUND"
