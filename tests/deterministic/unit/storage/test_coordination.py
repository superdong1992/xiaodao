from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from problem_locator.contracts import (
    AttachmentUploadGuard as AttachmentUploadGuardPort,
    AttachmentUploadLease as AttachmentUploadLeasePort,
    PublicationCommitGuard as PublicationCommitGuardPort,
    PublicationCommitLease as PublicationCommitLeasePort,
)
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)


ATTACHMENT_A = "00000000-0000-0000-0000-0000000000a1"
ATTACHMENT_B = "00000000-0000-0000-0000-0000000000b2"


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(0.002)


def _run_in_thread(function: Callable[[], None]) -> BaseException | None:
    observed: list[BaseException] = []

    def invoke() -> None:
        try:
            function()
        except BaseException as exc:  # test helper deliberately captures the exact failure
            observed.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    return observed[0] if observed else None


def test_coordination_lock_is_reentrant_and_tracked_per_thread() -> None:
    lock = StorageCoordinationLock()

    assert not lock.held_by_current_thread()
    assert lock.depth_for_current_thread() == 0
    with lock:
        assert lock.held_by_current_thread()
        assert lock.depth_for_current_thread() == 1
        with lock:
            assert lock.depth_for_current_thread() == 2
        assert lock.depth_for_current_thread() == 1

    assert not lock.held_by_current_thread()
    assert lock.depth_for_current_thread() == 0
    with pytest.raises(RuntimeError, match="not held"):
        lock.release()


def test_coordination_lock_nonblocking_acquisition_observes_other_owner() -> None:
    lock = StorageCoordinationLock()
    with lock:
        result: list[bool] = []
        thread = threading.Thread(
            target=lambda: result.append(lock.acquire(blocking=False))
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == [False]


def test_publication_guard_marks_only_lease_owned_depths() -> None:
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)

    with lock:
        assert lock.depth_for_current_thread() == 1
        assert not lock.publication_held_by_current_thread()
        outer = guard.acquire()
        assert lock.depth_for_current_thread() == 2
        assert lock.publication_depth_for_current_thread() == 1
        with lock:
            assert lock.depth_for_current_thread() == 3
            assert lock.publication_depth_for_current_thread() == 1
        nested = guard.acquire()
        assert lock.depth_for_current_thread() == 3
        assert lock.publication_depth_for_current_thread() == 2
        nested.release()
        assert lock.depth_for_current_thread() == 2
        assert lock.publication_depth_for_current_thread() == 1
        outer.release()
        assert lock.depth_for_current_thread() == 1
        assert not lock.publication_held_by_current_thread()


def test_publication_lease_is_idempotent_thread_bound_and_owns_lock_depth() -> None:
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)
    lease = guard.acquire()

    with pytest.raises(RuntimeError, match="publication lease owns"):
        lock.release()
    cross_thread_error = _run_in_thread(lease.release)
    assert isinstance(cross_thread_error, RuntimeError)
    assert "cannot cross threads" in str(cross_thread_error)
    assert guard.held_by_current_thread()

    lease.release()
    lease.release()
    assert lease.is_released()
    assert not lock.held_by_current_thread()
    assert not lock.publication_held_by_current_thread()


def test_publication_guard_excludes_another_thread_until_release() -> None:
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)
    first = guard.acquire()
    started = threading.Event()
    acquired = threading.Event()

    def contender() -> None:
        started.set()
        lease = guard.acquire()
        acquired.set()
        lease.release()

    thread = threading.Thread(target=contender)
    thread.start()
    assert started.wait(1)
    assert not acquired.wait(0.05)
    first.release()
    assert acquired.wait(1)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_publication_implementation_conforms_to_frozen_ports() -> None:
    guard = InProcessPublicationCommitGuard(StorageCoordinationLock())
    assert isinstance(guard, PublicationCommitGuardPort)
    lease = guard.acquire()
    try:
        assert isinstance(lease, PublicationCommitLeasePort)
    finally:
        lease.release()


def test_attachment_guard_is_fifo_for_the_same_id() -> None:
    registry = AttachmentUploadRegistry()
    guard = InProcessAttachmentUploadGuard(registry)
    first = guard.acquire(ATTACHMENT_A)
    order: list[int] = []

    def contender(ordinal: int) -> None:
        lease = guard.acquire(ATTACHMENT_A)
        registry.validate_lease(ATTACHMENT_A, lease)
        order.append(ordinal)
        lease.release()

    one = threading.Thread(target=contender, args=(1,))
    two = threading.Thread(target=contender, args=(2,))
    one.start()
    _wait_until(lambda: registry.waiting_count(ATTACHMENT_A) == 1)
    two.start()
    _wait_until(lambda: registry.waiting_count(ATTACHMENT_A) == 2)

    first.release()
    one.join(timeout=2)
    two.join(timeout=2)
    assert not one.is_alive() and not two.is_alive()
    assert order == [1, 2]
    assert registry.active_attachment_ids() == ()


def test_attachment_guard_allows_different_ids_in_parallel() -> None:
    registry = AttachmentUploadRegistry()
    guard = InProcessAttachmentUploadGuard(registry)
    rendezvous = threading.Barrier(3, timeout=2)
    failures: list[BaseException] = []

    def acquire_and_wait(attachment_id: str) -> None:
        try:
            lease = guard.acquire(attachment_id)
            rendezvous.wait()
            rendezvous.wait()
            lease.release()
        except BaseException as exc:
            failures.append(exc)

    one = threading.Thread(target=acquire_and_wait, args=(ATTACHMENT_A,))
    two = threading.Thread(target=acquire_and_wait, args=(ATTACHMENT_B,))
    one.start()
    two.start()
    rendezvous.wait()
    assert registry.active_attachment_ids() == (ATTACHMENT_A, ATTACHMENT_B)
    rendezvous.wait()
    one.join(timeout=2)
    two.join(timeout=2)
    assert not failures
    assert not one.is_alive() and not two.is_alive()


class _ForgedLease:
    attachment_id = ATTACHMENT_A

    def is_released(self) -> bool:
        return False

    def release(self) -> None:
        return None


def test_attachment_registry_rejects_forged_wrong_registry_and_wrong_id_leases() -> None:
    registry = AttachmentUploadRegistry()
    other_registry = AttachmentUploadRegistry()
    lease = registry.acquire(ATTACHMENT_A)
    other_lease = other_registry.acquire(ATTACHMENT_A)
    try:
        with pytest.raises(ValueError, match="not issued"):
            registry.validate_lease(ATTACHMENT_A, _ForgedLease())
        with pytest.raises(ValueError, match="not issued"):
            registry.validate_lease(ATTACHMENT_A, other_lease)
        with pytest.raises(ValueError, match="ID mismatch"):
            registry.validate_lease(ATTACHMENT_B, lease)
    finally:
        lease.release()
        other_lease.release()


def test_attachment_registry_rejects_released_and_cross_thread_use() -> None:
    registry = AttachmentUploadRegistry()
    lease = registry.acquire(ATTACHMENT_A)

    validate_error = _run_in_thread(
        lambda: registry.validate_lease(ATTACHMENT_A, lease)
    )
    release_error = _run_in_thread(lease.release)
    assert isinstance(validate_error, RuntimeError)
    assert isinstance(release_error, RuntimeError)
    assert registry.require_current_thread_lease(ATTACHMENT_A) is lease

    lease.release()
    assert lease.is_released()
    with pytest.raises(ValueError, match="released"):
        registry.validate_lease(ATTACHMENT_A, lease)
    with pytest.raises(RuntimeError, match="no active"):
        registry.require_current_thread_lease(ATTACHMENT_A)


def test_attachment_guard_and_lease_conform_to_frozen_ports() -> None:
    guard = InProcessAttachmentUploadGuard()
    assert isinstance(guard, AttachmentUploadGuardPort)
    lease = guard.acquire(ATTACHMENT_A)
    try:
        assert isinstance(lease, AttachmentUploadLeasePort)
    finally:
        lease.release()


def test_attachment_guard_rejects_non_contract_id() -> None:
    guard = InProcessAttachmentUploadGuard()
    with pytest.raises(ValueError):
        guard.acquire("not-a-uuid")
