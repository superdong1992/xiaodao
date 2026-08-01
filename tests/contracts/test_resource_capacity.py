from __future__ import annotations

import hashlib
import threading
import uuid

import pytest

from problem_locator.contracts.enums import ErrorCode, ResourceKind, ResourceType
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES
from problem_locator.contracts.models import PlannedResourceTarget

from tests.contracts.fakes import (
    InMemoryBinaryStream,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
)


CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000011"


def _target(
    name: str,
    size: int,
    sha256: str,
) -> PlannedResourceTarget:
    resource_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"resource-target:{name}"))
    return PlannedResourceTarget(
        case_id=CASE_ID,
        resource_type=ResourceType.ARTIFACT,
        resource_id=resource_id,
        final_storage_key=(
            f"resources/cases/{CASE_ID}/artifacts/{resource_id}/payload"
        ),
        resource_kind=ResourceKind.FILE,
        size=size,
        sha256=sha256,
    )


def _with_lease(
    guard: InMemoryPublicationCommitGuard,
    callback,
):
    lease = guard.acquire()
    try:
        return callback()
    finally:
        lease.release()


def test_capacity_counts_each_formal_key_once_across_all_accounting_classes() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    state_target = _target("a-state", 100, "a" * 64)
    outbox_target = _target("b-outbox", 200, "b" * 64)
    orphan_target = _target("c-orphan", 300, "c" * 64)
    quarantined_target = _target("d-quarantined", 400, "d" * 64)

    store.seed_formal_resource(state_target, state_reference_count=2)
    store.add_outbox_protections(state_target.final_storage_key)
    store.seed_formal_resource(outbox_target, outbox_reference_count=1)
    store.seed_formal_resource(orphan_target, ordinary_orphan=True)
    store.seed_formal_resource(quarantined_target, ordinary_orphan=True)
    assert store.quarantine_ordinary_orphan(
        quarantined_target.final_storage_key,
        "00000000-0000-0000-0000-000000000099",
    )

    usage = _with_lease(
        guard,
        lambda: store.validate_case_capacity(CASE_ID, []),
    )

    assert usage.current_bytes == 100 + 200 + 300
    assert usage.new_bytes == 0
    assert usage.total_bytes == 600
    assert usage.limit_bytes == MAX_CASE_RESOURCE_BYTES
    assert store.formal_resource_categories == {
        state_target.final_storage_key: ("STATE", "OUTBOX"),
        outbox_target.final_storage_key: ("OUTBOX",),
        orphan_target.final_storage_key: ("ORPHAN",),
    }
    assert quarantined_target.final_storage_key not in store.published_storage_keys
    assert len(store.quarantined_storage_keys) == 1


def test_same_key_replay_has_zero_delta_and_different_key_same_hash_counts_again() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    original = _target("a-original", 100, "a" * 64)
    same_bytes_new_key = _target("b-copy", 100, "a" * 64)
    store.seed_formal_resource(original, state_reference_count=1)

    same_key_usage = _with_lease(
        guard,
        lambda: store.validate_case_capacity(CASE_ID, [original]),
    )
    assert same_key_usage.current_bytes == 100
    assert same_key_usage.new_bytes == 0

    two_key_usage = _with_lease(
        guard,
        lambda: store.validate_case_capacity(
            CASE_ID,
            [original, same_bytes_new_key],
        ),
    )
    assert two_key_usage.current_bytes == 100
    assert two_key_usage.new_bytes == 100
    assert two_key_usage.total_bytes == 200

    conflicting_replay = original.model_copy(update={"sha256": "f" * 64})
    with pytest.raises(ApplicationPortError) as raised:
        _with_lease(
            guard,
            lambda: store.validate_case_capacity(CASE_ID, [conflicting_replay]),
        )
    assert raised.value.error.code is ErrorCode.RESOURCE_HASH_MISMATCH
    assert store.published_storage_keys == (original.final_storage_key,)


def test_over_limit_batch_is_rejected_before_any_partial_publish() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    existing = _target("a-existing", MAX_CASE_RESOURCE_BYTES - 5, "e" * 64)
    store.seed_formal_resource(existing, outbox_reference_count=1)

    first_payload = b"abc"
    second_payload = b"xyz"
    first_staged = store.stage_file(
        JOB_ID,
        "first",
        InMemoryBinaryStream(first_payload),
        expected_size=3,
        expected_sha256=hashlib.sha256(first_payload).hexdigest(),
    )
    second_staged = store.stage_file(
        JOB_ID,
        "second",
        InMemoryBinaryStream(second_payload),
        expected_size=3,
        expected_sha256=hashlib.sha256(second_payload).hexdigest(),
    )
    first_target = _target("b-first", first_staged.size, first_staged.sha256)
    second_target = _target("c-second", second_staged.size, second_staged.sha256)

    lease = guard.acquire()
    try:
        with pytest.raises(ApplicationPortError) as raised:
            store.validate_case_capacity(
                CASE_ID,
                [first_target, second_target],
            )
        assert raised.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
        assert store.publish_calls == []
        assert store.published_storage_keys == (existing.final_storage_key,)
        assert store.staged_resource_count == 2
    finally:
        lease.release()

    assert first_target.final_storage_key not in store.published_storage_keys
    assert second_target.final_storage_key not in store.published_storage_keys


def test_linked_store_requires_lease_for_capacity_and_publish() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    payload = b"payload"
    staged = store.stage_file(
        JOB_ID,
        "artifact",
        InMemoryBinaryStream(payload),
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    target = _target("artifact", staged.size, staged.sha256)

    with pytest.raises(RuntimeError, match="PublicationCommitLease"):
        store.validate_case_capacity(CASE_ID, [target])
    with pytest.raises(RuntimeError, match="PublicationCommitLease"):
        store.publish(staged, target.final_storage_key)

    lease = guard.acquire()
    try:
        usage = store.validate_case_capacity(CASE_ID, [target])
        assert usage.new_bytes == len(payload)
        published = store.publish(staged, target.final_storage_key)
    finally:
        lease.release()

    assert published.storage_key == target.final_storage_key
    assert store.formal_resource_categories[target.final_storage_key] == ("ORPHAN",)


def test_cleanup_uses_shared_lock_and_quarantine_is_not_adoptable_or_counted() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    orphan = _target("a-orphan", 123, "a" * 64)
    state_protected = _target("b-state", 234, "b" * 64)
    outbox_protected = _target("c-outbox", 345, "c" * 64)
    store.seed_formal_resource(orphan, ordinary_orphan=True)
    store.seed_formal_resource(state_protected, state_reference_count=1)
    store.seed_formal_resource(outbox_protected, outbox_reference_count=1)

    lease = guard.acquire()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    cleanup_result: list[bool] = []

    def cleanup() -> None:
        cleanup_started.set()
        cleanup_result.append(
            store.quarantine_ordinary_orphan(
                orphan.final_storage_key,
                "00000000-0000-0000-0000-000000000098",
            )
        )
        cleanup_finished.set()

    thread = threading.Thread(target=cleanup)
    thread.start()
    assert cleanup_started.wait(1)
    assert not cleanup_finished.wait(0.05)
    assert guard.held_by_current_thread()
    lease.release()

    assert cleanup_finished.wait(1)
    thread.join(1)
    assert not thread.is_alive()
    assert cleanup_result == [True]
    assert guard.events[0][0] == "acquire"
    assert guard.events[-1][0] == "release"
    assert store.cleanup_calls == [
        (
            orphan.final_storage_key,
            "00000000-0000-0000-0000-000000000098",
        )
    ]
    assert store.quarantine_events[0][0] == orphan.final_storage_key
    assert orphan.final_storage_key not in store.published_storage_keys

    assert not store.quarantine_ordinary_orphan(
        state_protected.final_storage_key,
        "00000000-0000-0000-0000-000000000097",
    )
    assert not store.quarantine_ordinary_orphan(
        outbox_protected.final_storage_key,
        "00000000-0000-0000-0000-000000000096",
    )

    usage = _with_lease(
        guard,
        lambda: store.validate_case_capacity(CASE_ID, []),
    )
    assert usage.current_bytes == state_protected.size + outbox_protected.size
    assert all(
        original_key != orphan.final_storage_key
        for original_key in store.published_storage_keys
    )
