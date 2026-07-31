from __future__ import annotations

import hashlib
import threading

import pytest

from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
)
from tests.contracts.fakes import (
    DeterministicIdGenerator as S00DeterministicIdGenerator,
    FakeClock as S00FakeClock,
)
from tests.contracts.scenario_fakes import (
    CountingBinaryStream as S00CountingBinaryStream,
)
from tests.unit.storage.fakes import (
    BarrierStorageCoordinationLock,
    CountingBinaryStream,
    DeterministicIdGenerator,
    FakeFileSync,
    FakeInstanceLockBackend,
    FaultInjectingReplace,
    FixedClock,
)


def _drain(stream: CountingBinaryStream, read_size: int = 4 * 1024 * 1024) -> int:
    total = 0
    while True:
        chunk = stream.read(read_size)
        if not chunk:
            return total
        total += len(chunk)


def test_fixed_clock_and_id_generator_reuse_the_s00_fakes() -> None:
    clock = FixedClock(
        "2026-07-31T01:02:03.000Z",
        scripted_values=["2026-07-31T01:02:04.000Z"],
    )
    assert isinstance(clock, S00FakeClock)
    assert clock.now() == "2026-07-31T01:02:04.000Z"
    assert clock.calls == 1

    assert DeterministicIdGenerator is S00DeterministicIdGenerator
    generator = DeterministicIdGenerator(seed="s02-fake-tests")
    first = generator.derive("cleanup", ["installation", "candidate"])
    second = generator.derive("cleanup", ["installation", "candidate"])
    assert first == second
    assert generator.derive_calls == [
        ("cleanup", ("installation", "candidate")),
        ("cleanup", ("installation", "candidate")),
    ]


def test_counting_stream_models_exact_2_5_gib_and_5_gib_with_bounded_memory() -> None:
    assert CountingBinaryStream is S00CountingBinaryStream
    assert len(CountingBinaryStream._CHUNK) == 1024 * 1024

    attachment = CountingBinaryStream(MAX_ATTACHMENT_BYTES + 1)
    assert _drain(attachment) == 2_684_354_561
    assert attachment.returned_logical_bytes == MAX_ATTACHMENT_BYTES + 1
    assert attachment.read_calls == 2_561 + 1  # 2,561 chunks plus EOF

    case = CountingBinaryStream(MAX_CASE_RESOURCE_BYTES)
    assert _drain(case) == 5_368_709_120
    assert case.returned_logical_bytes == MAX_CASE_RESOURCE_BYTES
    assert case.read_calls == 5_120 + 1  # 5,120 chunks plus EOF

    case.close()
    case.close()
    assert case.close_calls == 2
    with pytest.raises(ValueError, match="closed"):
        case.read(1)


def test_fake_file_sync_records_each_boundary_and_fails_exact_occurrence(
    tmp_path,
) -> None:
    fake = FakeFileSync()
    fake.fail_on("sync_file", 2, OSError("second file sync failed"))

    first_path = tmp_path / "first.tmp"
    first_path.write_bytes(b"first")
    with first_path.open("rb") as handle:
        fake.sync_file(handle)
    with pytest.raises(OSError, match="second file sync failed"):
        fake.fsync_file(tmp_path / "second.tmp")
    fake.sync_directory(tmp_path)
    fake.set_file_read_only(tmp_path / "payload")
    fake.set_directory_read_only(tmp_path / "tree")

    assert [event.operation for event in fake.events] == [
        "sync_file",
        "sync_file",
        "sync_directory",
        "readonly_file",
        "readonly_directory",
    ]
    assert [event.occurrence for event in fake.calls("sync_file")] == [1, 2]
    assert fake.count("sync_directory") == 1
    assert fake.events[-1].path == tmp_path / "tree"

    next_occurrence = fake.fail_next("readonly_file")
    assert next_occurrence == 2
    with pytest.raises(OSError, match="injected readonly_file failure #2"):
        fake.make_read_only(tmp_path / "second-payload")


def test_fake_instance_lock_backend_models_nonblocking_competition_and_lifetime(
    tmp_path,
) -> None:
    backend = FakeInstanceLockBackend()
    lock_path = tmp_path / ".instance.lock"

    first = backend.acquire(lock_path)
    assert backend.is_locked(lock_path)
    with pytest.raises(BlockingIOError, match="already held"):
        backend.acquire(lock_path)

    first.release()
    first.release()
    assert not backend.is_locked(lock_path)

    with backend.acquire(lock_path) as second:
        assert second.owner_id != first.owner_id
        assert backend.is_locked(lock_path)
    assert not backend.is_locked(lock_path)
    assert [event.action for event in backend.events] == [
        "acquired",
        "blocked",
        "released",
        "acquired",
        "released",
    ]


def test_fake_instance_lock_can_inject_a_specific_backend_failure(tmp_path) -> None:
    backend = FakeInstanceLockBackend()
    backend.fail_on_acquire(2, PermissionError("lock file denied"))
    first = backend.acquire(tmp_path / "one.lock")
    with pytest.raises(PermissionError, match="lock file denied"):
        backend.acquire(tmp_path / "two.lock")
    first.release()


def test_fake_instance_lock_matches_the_open_file_backend_protocol(tmp_path) -> None:
    backend = FakeInstanceLockBackend()
    lock_path = tmp_path / ".instance.lock"
    lock_path.touch()

    with lock_path.open("r+b", buffering=0) as first_handle, lock_path.open(
        "r+b", buffering=0
    ) as second_handle:
        assert backend.acquire(first_handle) is None
        with pytest.raises(BlockingIOError, match="already held"):
            backend.acquire(second_handle)
        backend.release(first_handle)
        assert backend.acquire(second_handle) is None
        backend.release(second_handle)

    assert [event.action for event in backend.events] == [
        "acquired",
        "blocked",
        "released",
        "acquired",
        "released",
    ]


def test_fault_injecting_replace_records_failure_then_delegates_real_replace(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"first")
    destination.write_bytes(b"old")

    replace = FaultInjectingReplace()
    replace.fail_next(OSError("replace interrupted"))
    with pytest.raises(OSError, match="replace interrupted"):
        replace(source, destination)
    assert source.read_bytes() == b"first"
    assert destination.read_bytes() == b"old"

    replace.replace(source, destination)
    assert not source.exists()
    assert destination.read_bytes() == b"first"
    assert replace.call_count == 2
    assert [event.occurrence for event in replace.events] == [1, 2]


def test_barrier_coordination_lock_is_reentrant_and_can_pause_while_held() -> None:
    lock = BarrierStorageCoordinationLock()
    lock.arm_checkpoint("after_acquire")
    worker_inside = threading.Event()
    worker_finished = threading.Event()

    def worker() -> None:
        with lock:
            worker_inside.set()
            with lock:
                assert lock.depth_for_current_thread() == 2
        worker_finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert lock.wait_for_checkpoint("after_acquire", timeout_seconds=1.0)
        assert not worker_inside.is_set()
        # The worker has acquired the underlying RLock but is parked at the
        # injected post-acquire checkpoint, so another thread cannot enter.
        assert lock.acquire(blocking=False) is False
        lock.release_checkpoint("after_acquire")
        thread.join(timeout=1.0)
    finally:
        lock.release_all_checkpoints()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert worker_inside.is_set()
    assert worker_finished.is_set()
    assert [event[0] for event in lock.events] == [
        "acquire",
        "acquire",
        "release",
        "release",
    ]


def test_barrier_supports_named_toctou_checkpoint_independent_of_lock_methods() -> None:
    lock = BarrierStorageCoordinationLock()
    lock.arm("after_reference_check")
    passed = threading.Event()

    def cleaner() -> None:
        with lock:
            lock.checkpoint("after_reference_check")
            passed.set()

    thread = threading.Thread(target=cleaner, daemon=True)
    thread.start()
    try:
        assert lock.wait_until_blocked("after_reference_check", 1.0)
        assert not passed.is_set()
        lock.release_barrier("after_reference_check")
        thread.join(timeout=1.0)
    finally:
        lock.release_all_checkpoints()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert passed.is_set()
    assert ("after_reference_check", thread.ident) in lock.checkpoint_events


def test_barrier_lock_tracks_publication_owned_reentrant_depth() -> None:
    lock = BarrierStorageCoordinationLock()
    assert lock.acquire()
    lock._mark_publication_acquired()
    assert lock.publication_held_by_current_thread()
    assert lock.publication_depth_for_current_thread() == 1

    # A raw release cannot consume the depth owned by a publication lease.
    with pytest.raises(RuntimeError, match="publication lease owns"):
        lock.release()
    lock._mark_publication_released()
    lock.release()
    assert not lock.held_by_current_thread()
    assert not lock.publication_held_by_current_thread()


def test_counting_stream_chunks_have_the_expected_repeatable_sha256() -> None:
    stream = CountingBinaryStream(2 * 1024 * 1024)
    first = stream.read(1024 * 1024)
    second = stream.read(1024 * 1024)
    assert first == CountingBinaryStream._CHUNK
    assert second == CountingBinaryStream._CHUNK
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
