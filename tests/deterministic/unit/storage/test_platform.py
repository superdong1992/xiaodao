from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

import problem_locator.storage.platform as storage_platform
from problem_locator.storage.platform import (
    FileInstanceLock,
    InstanceLockBackend,
    PlatformFileSync,
    PlatformReplaceOperation,
    PosixInstanceLockBackend,
    ReplaceOperation,
    WindowsInstanceLockBackend,
    _sync_windows_directory,
    chmod_no_follow,
)
from tests.deterministic.unit.storage.platform_support import symlink_or_skip


def _chmod_os(
    monkeypatch: pytest.MonkeyPatch, *, name: str = "posix"
) -> SimpleNamespace:
    """Exercise unsupported-platform behavior without changing the host OS."""

    metadata = SimpleNamespace(st_dev=7, st_ino=11, st_mode=stat.S_IFREG | 0o600)
    proxy = SimpleNamespace(
        name=name,
        supports_follow_symlinks=set(),
        O_RDONLY=0,
        O_NOFOLLOW=0x100,
        O_NONBLOCK=0x200,
        O_CLOEXEC=0x400,
        metadata=metadata,
        opened_metadata=metadata,
        native_failure=NotImplementedError("chmod: follow_symlinks unavailable"),
        chmod_calls=[],
        open_calls=[],
        fchmod_calls=[],
        close_calls=[],
    )

    def chmod(path: Path | str, mode: int, *, follow_symlinks: bool = True) -> None:
        proxy.chmod_calls.append((path, mode, follow_symlinks))
        if not follow_symlinks and proxy.native_failure is not None:
            raise proxy.native_failure
        proxy.metadata.st_mode = stat.S_IFMT(proxy.metadata.st_mode) | mode

    def open_file(path: Path | str, flags: int) -> int:
        proxy.open_calls.append((path, flags))
        return 41

    def fchmod(descriptor: int, mode: int) -> None:
        proxy.fchmod_calls.append((descriptor, mode))
        proxy.opened_metadata.st_mode = (
            stat.S_IFMT(proxy.opened_metadata.st_mode) | mode
        )

    proxy.chmod = chmod
    proxy.lstat = lambda path: proxy.metadata
    proxy.open = open_file
    proxy.fstat = lambda descriptor: proxy.opened_metadata
    proxy.fchmod = fchmod
    proxy.close = lambda descriptor: proxy.close_calls.append(descriptor)
    monkeypatch.setattr(storage_platform, "os", proxy)
    return proxy


def test_chmod_no_follow_attempts_native_call_before_capability_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.native_failure = None

    chmod_no_follow("payload", 0o444)

    assert stat.S_IMODE(proxy.metadata.st_mode) == 0o444
    assert proxy.chmod_calls == [("payload", 0o444, False)]
    assert not proxy.open_calls


@pytest.mark.parametrize(
    "node_type,mode", [(stat.S_IFREG, 0o444), (stat.S_IFDIR, 0o555)]
)
def test_chmod_no_follow_falls_back_on_posix_without_follow_symlink_support(
    monkeypatch: pytest.MonkeyPatch, node_type: int, mode: int
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.metadata.st_mode = node_type | 0o700

    chmod_no_follow("payload", mode)

    assert stat.S_IMODE(proxy.metadata.st_mode) == mode
    assert proxy.chmod_calls == [("payload", mode, False)]
    assert proxy.fchmod_calls == [(41, mode)]
    assert proxy.open_calls[0][1] & proxy.O_NOFOLLOW
    assert proxy.open_calls[0][1] & proxy.O_NONBLOCK
    assert proxy.close_calls == [41]


def test_chmod_no_follow_preserves_not_implemented_when_capability_is_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.supports_follow_symlinks.add(proxy.chmod)

    with pytest.raises(NotImplementedError) as error:
        chmod_no_follow("payload", 0o444)

    assert error.value is proxy.native_failure
    assert proxy.chmod_calls == [("payload", 0o444, False)]
    assert not proxy.open_calls


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("I/O failure")])
def test_chmod_no_follow_does_not_fallback_for_other_native_errors(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.native_failure = failure

    with pytest.raises(OSError) as error:
        chmod_no_follow("payload", 0o444)

    assert error.value is failure
    assert proxy.chmod_calls == [("payload", 0o444, False)]
    assert not proxy.open_calls


@pytest.mark.parametrize("missing_capability", ["O_NOFOLLOW", "fchmod"])
def test_chmod_no_follow_refuses_unsafe_posix_fallback(
    monkeypatch: pytest.MonkeyPatch, missing_capability: str
) -> None:
    proxy = _chmod_os(monkeypatch)
    delattr(proxy, missing_capability)

    with pytest.raises(NotImplementedError) as error:
        chmod_no_follow("payload", 0o444)

    assert error.value is proxy.native_failure
    assert proxy.chmod_calls == [("payload", 0o444, False)]
    assert not proxy.open_calls


@pytest.mark.parametrize("name", ["posix", "nt"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "reparse"])
def test_chmod_no_follow_fallback_rejects_link_before_changing_permissions(
    monkeypatch: pytest.MonkeyPatch, name: str, unsafe_kind: str
) -> None:
    proxy = _chmod_os(monkeypatch, name=name)
    if unsafe_kind == "symlink":
        proxy.metadata.st_mode = stat.S_IFLNK | 0o777
    else:
        proxy.metadata.st_file_attributes = 0x00000400

    with pytest.raises(OSError):
        chmod_no_follow("payload", 0o444)

    assert proxy.chmod_calls == [("payload", 0o444, False)]
    assert not proxy.open_calls
    assert not proxy.fchmod_calls


def test_chmod_no_follow_posix_fallback_rejects_special_file_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.metadata.st_mode = stat.S_IFIFO | 0o600

    with pytest.raises(OSError):
        chmod_no_follow("payload", 0o444)

    assert not proxy.open_calls
    assert not proxy.fchmod_calls


@pytest.mark.parametrize("replacement", ["inode", "type", "reparse"])
def test_chmod_no_follow_posix_fallback_rejects_replaced_opened_target_and_closes(
    monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    proxy = _chmod_os(monkeypatch)
    proxy.opened_metadata = SimpleNamespace(**vars(proxy.metadata))
    if replacement == "inode":
        proxy.opened_metadata.st_ino += 1
    elif replacement == "type":
        proxy.opened_metadata.st_mode = stat.S_IFDIR | 0o700
    else:
        proxy.opened_metadata.st_file_attributes = 0x00000400

    with pytest.raises(OSError):
        chmod_no_follow("payload", 0o444)

    assert not proxy.fchmod_calls
    assert proxy.close_calls == [41]
    assert stat.S_IMODE(proxy.metadata.st_mode) == 0o600


def test_chmod_no_follow_posix_fallback_closes_descriptor_on_fchmod_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _chmod_os(monkeypatch)
    failure = PermissionError("descriptor permission denied")

    def fail_fchmod(descriptor: int, mode: int) -> None:
        raise failure

    proxy.fchmod = fail_fchmod

    with pytest.raises(PermissionError) as error:
        chmod_no_follow("payload", 0o444)

    assert error.value is failure
    assert proxy.close_calls == [41]
    assert proxy.chmod_calls == [("payload", 0o444, False)]


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_chmod_no_follow_fallback_detects_path_replacement_after_chmod(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    proxy = _chmod_os(monkeypatch, name=name)
    original = proxy.metadata
    apply_mode = proxy.fchmod if name == "posix" else proxy.chmod

    def swap_after_chmod(*args: object, **kwargs: object) -> None:
        apply_mode(*args, **kwargs)
        proxy.metadata = SimpleNamespace(
            st_dev=7, st_ino=12, st_mode=stat.S_IFLNK | 0o777
        )

    if name == "posix":
        proxy.fchmod = swap_after_chmod
    else:
        proxy.chmod = swap_after_chmod

    with pytest.raises(OSError):
        chmod_no_follow("payload", 0o444)

    assert stat.S_IMODE(original.st_mode) == 0o444
    assert stat.S_IMODE(proxy.metadata.st_mode) == 0o777
    assert proxy.close_calls == ([41] if name == "posix" else [])


def test_chmod_no_follow_preserves_windows_checked_path_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _chmod_os(monkeypatch, name="nt")

    chmod_no_follow("payload", 0o444)

    assert stat.S_IMODE(proxy.metadata.st_mode) == 0o444
    assert proxy.chmod_calls == [
        ("payload", 0o444, False),
        (Path("payload"), 0o444, True),
    ]
    assert not proxy.open_calls


@pytest.mark.skipif(os.name == "nt", reason="real no-follow descriptors require POSIX")
@pytest.mark.parametrize("directory", [False, True])
def test_chmod_no_follow_posix_fallback_changes_real_file_and_directory_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, directory: bool
) -> None:
    target = tmp_path / "payload"
    if directory:
        target.mkdir()
    else:
        target.write_bytes(b"payload")
    proxy = _chmod_os(monkeypatch)
    for attribute in (
        "open", "close", "fstat", "lstat", "fchmod",
        "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC",
    ):
        setattr(proxy, attribute, getattr(os, attribute))
    mode = 0o555 if directory else 0o444

    try:
        chmod_no_follow(target, mode)
        assert stat.S_IMODE(target.stat().st_mode) == mode
    finally:
        os.chmod(target, 0o700 if directory else 0o600)


@pytest.mark.skipif(os.name == "nt", reason="real no-follow descriptors require POSIX")
@pytest.mark.parametrize("swap_point", ["before_open", "after_open"])
def test_chmod_no_follow_posix_fallback_never_changes_symlink_target_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, swap_point: str
) -> None:
    target = tmp_path / "payload"
    original = tmp_path / "original"
    external = tmp_path / "external"
    target.write_bytes(b"payload")
    external.write_bytes(b"external")
    os.chmod(external, 0o600)
    proxy = _chmod_os(monkeypatch)
    for attribute in (
        "close", "fstat", "lstat", "fchmod",
        "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC",
    ):
        setattr(proxy, attribute, getattr(os, attribute))
    closed: list[int] = []

    def replace_with_symlink() -> None:
        target.rename(original)
        target.symlink_to(external)

    def open_at_swap(path: Path | str, flags: int) -> int:
        if swap_point == "before_open":
            replace_with_symlink()
        descriptor = os.open(path, flags)
        if swap_point == "after_open":
            replace_with_symlink()
        return descriptor

    def close_descriptor(descriptor: int) -> None:
        closed.append(descriptor)
        os.close(descriptor)

    proxy.open = open_at_swap
    proxy.close = close_descriptor

    try:
        with pytest.raises(OSError):
            chmod_no_follow(target, 0o444)
        assert stat.S_IMODE(external.stat().st_mode) == 0o600
        assert len(closed) == (1 if swap_point == "after_open" else 0)
    finally:
        if original.exists():
            os.chmod(original, 0o600)


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


def test_windows_directory_sync_requests_write_access_and_closes_handle(
    tmp_path: Path,
) -> None:
    create_calls: list[tuple[object, ...]] = []
    flush_calls: list[object] = []
    close_calls: list[object] = []
    handle = object()

    _sync_windows_directory(
        tmp_path,
        create_file_fn=lambda *arguments: (
            create_calls.append(arguments),
            handle,
        )[1],
        flush_file_buffers_fn=lambda value: flush_calls.append(value) or True,
        close_handle_fn=lambda value: close_calls.append(value) or True,
        invalid_handle_value=-1,
        get_last_error_fn=lambda: 5,
        win_error_fn=lambda code: OSError(code, "simulated Win32 failure"),
    )

    assert create_calls[0][1] == 0x80000000 | 0x40000000
    assert create_calls[0][2] == 0x00000001 | 0x00000002 | 0x00000004
    assert create_calls[0][5] == 0x02000000
    assert flush_calls == [handle]
    assert close_calls == [handle]


@pytest.mark.parametrize("failure_point", ["create", "flush", "close"])
def test_windows_directory_sync_propagates_each_win32_failure(
    tmp_path: Path,
    failure_point: str,
) -> None:
    handle = object()
    close_calls: list[object] = []

    with pytest.raises(OSError, match="simulated Win32 failure"):
        _sync_windows_directory(
            tmp_path,
            create_file_fn=lambda *arguments: -1
            if failure_point == "create"
            else handle,
            flush_file_buffers_fn=lambda value: failure_point != "flush",
            close_handle_fn=lambda value: (
                close_calls.append(value),
                failure_point != "close",
            )[1],
            invalid_handle_value=-1,
            get_last_error_fn=lambda: 5,
            win_error_fn=lambda code: OSError(code, "simulated Win32 failure"),
        )

    assert close_calls == ([] if failure_point == "create" else [handle])


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
    symlink_or_skip(link, real)
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
    symlink_or_skip(link, payload)

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
