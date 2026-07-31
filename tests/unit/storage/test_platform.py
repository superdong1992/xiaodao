from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

import pytest

from problem_locator.storage.platform import (
    FileInstanceLock,
    InstanceLockBackend,
    PlatformFileSync,
    PlatformReplaceOperation,
    PosixInstanceLockBackend,
    ReplaceOperation,
    WindowsInstanceLockBackend,
)


class _RecordingLockBackend:
    def __init__(self, acquire_failure: BaseException | None = None) -> None:
        self.acquire_failure = acquire_failure
        self.acquire_handles: list[BinaryIO] = []
        self.release_handles: list[BinaryIO] = []

    def acquire(self, handle: BinaryIO) -> None:
        self.acquire_handles.append(handle)
        if self.acquire_failure is not None:
            raise self.acquire_failure

    def release(self, handle: BinaryIO) -> None:
        self.release_handles.append(handle)


def test_posix_backend_uses_nonblocking_exclusive_flock(tmp_path: Path) -> None:
    calls: list[tuple[int, int]] = []
    backend = PosixInstanceLockBackend(
        flock_fn=lambda descriptor, operation: calls.append((descriptor, operation)),
        lock_ex=0x02,
        lock_nb=0x04,
        lock_un=0x08,
    )
    lock_file = tmp_path / ".instance.lock"
    lock_file.touch()

    with lock_file.open("r+b", buffering=0) as handle:
        backend.acquire(handle)
        backend.release(handle)
        assert calls == [
            (handle.fileno(), 0x02 | 0x04),
            (handle.fileno(), 0x08),
        ]


def test_simulated_windows_backend_locks_one_byte_and_unlocks(tmp_path: Path) -> None:
    calls: list[tuple[int, int, int]] = []
    backend = WindowsInstanceLockBackend(
        locking_fn=lambda descriptor, mode, count: calls.append(
            (descriptor, mode, count)
        ),
        nonblocking_mode=17,
        unlock_mode=23,
    )
    lock_file = tmp_path / ".instance.lock"
    lock_file.touch()

    with lock_file.open("r+b", buffering=0) as handle:
        backend.acquire(handle)
        assert lock_file.stat().st_size == 1
        backend.release(handle)
        assert calls == [
            (handle.fileno(), 17, 1),
            (handle.fileno(), 23, 1),
        ]


def test_file_instance_lock_holds_handle_and_never_unlinks(tmp_path: Path) -> None:
    backend = _RecordingLockBackend()
    lock_path = tmp_path / ".instance.lock"
    lock = FileInstanceLock(lock_path, backend)
    assert isinstance(backend, InstanceLockBackend)

    assert lock.acquire() is lock
    assert lock.is_acquired()
    assert len(backend.acquire_handles) == 1
    assert not backend.acquire_handles[0].closed
    with pytest.raises(RuntimeError, match="already acquired"):
        lock.acquire()

    lock.release()
    lock.release()
    assert not lock.is_acquired()
    assert backend.release_handles == backend.acquire_handles
    assert backend.release_handles[0].closed
    assert lock_path.is_file()


def test_file_instance_lock_closes_handle_when_backend_acquire_fails(
    tmp_path: Path,
) -> None:
    failure = BlockingIOError("locked")
    backend = _RecordingLockBackend(failure)
    lock = FileInstanceLock(tmp_path / ".instance.lock", backend)

    with pytest.raises(BlockingIOError, match="locked"):
        lock.acquire()
    assert not lock.is_acquired()
    assert backend.acquire_handles[0].closed
    assert not backend.release_handles


def test_file_instance_lock_requires_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        FileInstanceLock(Path("relative/.instance.lock"), _RecordingLockBackend())


@pytest.mark.skipif(os.name == "nt", reason="real flock test is POSIX-only")
def test_real_posix_lock_rejects_second_instance_then_allows_it_after_release(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / ".instance.lock"
    first = FileInstanceLock(lock_path, PosixInstanceLockBackend())
    second = FileInstanceLock(lock_path, PosixInstanceLockBackend())
    first.acquire()
    try:
        with pytest.raises(OSError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="symbolic links are unavailable on this platform",
)
def test_file_instance_lock_rejects_symbolic_link(tmp_path: Path) -> None:
    real = tmp_path / "real.lock"
    real.touch()
    link = tmp_path / ".instance.lock"
    link.symlink_to(real)
    lock = FileInstanceLock(link, _RecordingLockBackend())

    with pytest.raises(OSError):
        lock.acquire()


def test_platform_file_sync_accepts_open_handle_path_and_directory(
    tmp_path: Path,
) -> None:
    synced_descriptors: list[int] = []
    directory_calls: list[Path] = []
    file_sync = PlatformFileSync(
        fsync_fn=lambda descriptor: synced_descriptors.append(descriptor),
        directory_sync_fn=lambda path: directory_calls.append(path),
    )
    payload = tmp_path / "payload"

    with payload.open("wb") as handle:
        handle.write(b"durable")
        file_sync.sync_file(handle)
        assert synced_descriptors == [handle.fileno()]
    file_sync.sync_file(payload)
    assert len(synced_descriptors) == 2

    file_sync.sync_directory(tmp_path)
    assert directory_calls == [tmp_path]


@pytest.mark.skipif(os.name == "nt", reason="default directory fsync is POSIX-only")
def test_platform_file_sync_default_directory_path_is_injectable(
    tmp_path: Path,
) -> None:
    synced_descriptors: list[int] = []
    file_sync = PlatformFileSync(
        fsync_fn=lambda descriptor: synced_descriptors.append(descriptor)
    )

    file_sync.sync_directory(tmp_path)
    assert len(synced_descriptors) == 1


def test_platform_file_sync_propagates_injected_failures(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"bytes")
    failure = OSError("injected fsync failure")
    file_sync = PlatformFileSync(
        fsync_fn=lambda descriptor: (_ for _ in ()).throw(failure)
    )

    with pytest.raises(OSError, match="injected fsync failure"):
        file_sync.sync_file(payload)


def test_platform_file_sync_makes_files_and_directories_read_only(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "tree"
    directory.mkdir()
    payload = tmp_path / "payload"
    payload.write_bytes(b"bytes")
    file_sync = PlatformFileSync()

    try:
        file_sync.make_read_only(payload)
        file_sync.make_read_only(directory)
        assert stat.S_IMODE(payload.stat().st_mode) == 0o444
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
    finally:
        os.chmod(payload, 0o600)
        os.chmod(directory, 0o700)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="symbolic links are unavailable on this platform",
)
def test_platform_file_sync_rejects_read_only_symbolic_link(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"bytes")
    link = tmp_path / "link"
    link.symlink_to(payload)

    with pytest.raises(OSError, match="symbolic link"):
        PlatformFileSync().make_read_only(link)

    with pytest.raises(OSError):
        PlatformFileSync().sync_file(link)


def test_replace_operation_delegates_exact_paths_and_propagates_failure(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path | str, Path | str]] = []
    operation = PlatformReplaceOperation(
        lambda source, destination: calls.append((source, destination))
    )
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    assert isinstance(operation, ReplaceOperation)

    operation.replace(source, destination)
    assert calls == [(source, destination)]

    failing = PlatformReplaceOperation(
        lambda source, destination: (_ for _ in ()).throw(
            OSError("injected replace failure")
        )
    )
    with pytest.raises(OSError, match="injected replace failure"):
        failing.replace(source, destination)
