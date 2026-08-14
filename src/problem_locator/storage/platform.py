"""Platform adapters for durable local-file storage.

All operating-system effects that need deterministic fault injection are kept
behind the small classes in this module.  They intentionally raise the native
built-in exceptions from the underlying operation; the frozen cross-Port
error channel is handled separately by the contract-change process.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self, runtime_checkable

from .atomic import is_reparse_point


@runtime_checkable
class InstanceLockBackend(Protocol):
    """Lock and unlock the first byte of an already-open lock file."""

    def acquire(self, handle: BinaryIO) -> None: ...

    def release(self, handle: BinaryIO) -> None: ...


class PosixInstanceLockBackend:
    """Non-blocking exclusive ``flock`` backend for Linux and macOS."""

    def __init__(
        self,
        *,
        flock_fn: Callable[[int, int], None] | None = None,
        lock_ex: int | None = None,
        lock_nb: int | None = None,
        lock_un: int | None = None,
    ) -> None:
        if (
            flock_fn is None
            or lock_ex is None
            or lock_nb is None
            or lock_un is None
        ):
            import fcntl

            flock_fn = flock_fn or fcntl.flock
            lock_ex = fcntl.LOCK_EX if lock_ex is None else lock_ex
            lock_nb = fcntl.LOCK_NB if lock_nb is None else lock_nb
            lock_un = fcntl.LOCK_UN if lock_un is None else lock_un
        self._flock = flock_fn
        self._lock_ex = lock_ex
        self._lock_nb = lock_nb
        self._lock_un = lock_un

    def acquire(self, handle: BinaryIO) -> None:
        self._flock(handle.fileno(), self._lock_ex | self._lock_nb)

    def release(self, handle: BinaryIO) -> None:
        self._flock(handle.fileno(), self._lock_un)


class WindowsInstanceLockBackend:
    """Non-blocking one-byte ``msvcrt.locking`` backend."""

    def __init__(
        self,
        *,
        locking_fn: Callable[[int, int, int], None] | None = None,
        nonblocking_mode: int | None = None,
        unlock_mode: int | None = None,
    ) -> None:
        if locking_fn is None or nonblocking_mode is None or unlock_mode is None:
            import msvcrt

            locking_fn = locking_fn or msvcrt.locking
            nonblocking_mode = (
                msvcrt.LK_NBLCK if nonblocking_mode is None else nonblocking_mode
            )
            unlock_mode = msvcrt.LK_UNLCK if unlock_mode is None else unlock_mode
        self._locking = locking_fn
        self._nonblocking_mode = nonblocking_mode
        self._unlock_mode = unlock_mode

    @staticmethod
    def _ensure_lock_byte(handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

    def acquire(self, handle: BinaryIO) -> None:
        self._ensure_lock_byte(handle)
        self._locking(handle.fileno(), self._nonblocking_mode, 1)

    def release(self, handle: BinaryIO) -> None:
        handle.seek(0)
        self._locking(handle.fileno(), self._unlock_mode, 1)


def _default_instance_lock_backend() -> InstanceLockBackend:
    if os.name == "nt":
        return WindowsInstanceLockBackend()
    return PosixInstanceLockBackend()


class FileInstanceLock:
    """Hold one backend lock for the lifetime of an opened installation.

    The lock file is deliberately never unlinked.  Unlinking would permit a
    second process to lock a new inode while the first process still owns the
    old one.
    """

    def __init__(
        self,
        lock_path: Path | str,
        backend: InstanceLockBackend | None = None,
    ) -> None:
        path = Path(lock_path)
        if not path.is_absolute():
            raise ValueError("instance lock path must be absolute")
        self.path = path
        self.backend = backend or _default_instance_lock_backend()
        self._handle: BinaryIO | None = None

    @staticmethod
    def _open_regular_file(path: Path) -> BinaryIO:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode) or is_reparse_point(
                descriptor_stat
            ):
                raise OSError("instance lock path is not a regular file")
            path_stat = os.lstat(path)
            if not stat.S_ISREG(path_stat.st_mode) or is_reparse_point(path_stat):
                raise OSError("instance lock path is not a regular file")
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise OSError("instance lock path changed while opening")
            return os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    def acquire(self) -> Self:
        if self._handle is not None:
            raise RuntimeError("instance lock is already acquired")
        handle = self._open_regular_file(self.path)
        try:
            self.backend.acquire(handle)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self.backend.release(handle)
        finally:
            handle.close()

    def is_acquired(self) -> bool:
        return self._handle is not None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _sync_windows_directory(
    path: Path,
    *,
    create_file_fn: Callable[..., object] | None = None,
    flush_file_buffers_fn: Callable[[object], object] | None = None,
    close_handle_fn: Callable[[object], object] | None = None,
    invalid_handle_value: object | None = None,
    get_last_error_fn: Callable[[], int] | None = None,
    win_error_fn: Callable[[int], BaseException] | None = None,
) -> None:
    """Flush a Windows directory handle using ``FlushFileBuffers``."""

    injected = (
        create_file_fn,
        flush_file_buffers_fn,
        close_handle_fn,
        invalid_handle_value,
        get_last_error_fn,
        win_error_fn,
    )
    if all(value is None for value in injected):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = [wintypes.HANDLE]
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        create_file_fn = create_file
        flush_file_buffers_fn = flush_file_buffers
        close_handle_fn = close_handle
        invalid_handle_value = ctypes.c_void_p(-1).value
        get_last_error_fn = ctypes.get_last_error
        win_error_fn = ctypes.WinError
    elif any(value is None for value in injected):
        raise TypeError("Windows directory sync injection must be complete")

    assert create_file_fn is not None
    assert flush_file_buffers_fn is not None
    assert close_handle_fn is not None
    assert get_last_error_fn is not None
    assert win_error_fn is not None

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read = 0x00000001
    share_write = 0x00000002
    share_delete = 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
    handle = create_file_fn(
        str(path),
        generic_read | generic_write,
        share_read | share_write | share_delete,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle == invalid_handle_value:
        raise win_error_fn(get_last_error_fn())
    failure: BaseException | None = None
    try:
        if not flush_file_buffers_fn(handle):
            failure = win_error_fn(get_last_error_fn())
    finally:
        if not close_handle_fn(handle) and failure is None:
            failure = win_error_fn(get_last_error_fn())
    if failure is not None:
        raise failure


def chmod_no_follow(path: Path | str, mode: int) -> None:
    """Apply permissions without accepting a final link or reparse-point swap."""

    try:
        os.chmod(path, mode, follow_symlinks=False)
    except NotImplementedError:
        if os.name != "nt":
            raise
        # CPython on Windows does not implement follow_symlinks=False for
        # chmod.  The caller rejects reparse points immediately before this
        # operation and verifies the same file identity immediately after it.
        target = Path(path)
        metadata = os.lstat(target)
        if stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata):
            raise OSError("chmod target must not be a symbolic link")
        os.chmod(target, mode)
        final_metadata = os.lstat(target)
        if (
            stat.S_ISLNK(final_metadata.st_mode)
            or is_reparse_point(final_metadata)
            or (
                final_metadata.st_dev,
                final_metadata.st_ino,
                stat.S_IFMT(final_metadata.st_mode),
            )
            != (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
            )
        ):
            raise OSError("chmod target changed while permissions were applied")


class PlatformFileSync:
    """Flush files/directories and apply immutable read-only permissions."""

    def __init__(
        self,
        *,
        fsync_fn: Callable[[int], None] = os.fsync,
        open_fn: Callable[..., int] = os.open,
        close_fn: Callable[[int], None] = os.close,
        chmod_fn: Callable[[Path | str, int], None] = chmod_no_follow,
        directory_sync_fn: Callable[[Path], None] | None = None,
    ) -> None:
        self._fsync = fsync_fn
        self._open = open_fn
        self._close = close_fn
        self._chmod = chmod_fn
        self._directory_sync = directory_sync_fn
        self._windows_finalized_files: set[tuple[int, int]] = set()

    def sync_file(self, path_or_handle: Path | str | int | BinaryIO) -> None:
        if isinstance(path_or_handle, int):
            self._fsync(path_or_handle)
            return
        if hasattr(path_or_handle, "fileno"):
            metadata = os.fstat(path_or_handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or is_reparse_point(metadata):
                raise OSError("file sync handle is not a regular file")
            flush = getattr(path_or_handle, "flush", None)
            if flush is not None:
                flush()
            self._fsync(path_or_handle.fileno())
            return

        path = Path(path_or_handle)
        pre_open_stat = os.lstat(path)
        pre_open_identity = (pre_open_stat.st_dev, pre_open_stat.st_ino)
        windows_finalized = (
            os.name == "nt"
            and pre_open_identity in self._windows_finalized_files
        )
        access = os.O_RDONLY if windows_finalized or os.name != "nt" else os.O_RDWR
        flags = access | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = self._open(path, flags)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode) or is_reparse_point(
                descriptor_stat
            ):
                raise OSError("file sync target is not a regular file")
            path_stat = os.lstat(path)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or is_reparse_point(path_stat)
            ) or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != (path_stat.st_dev, path_stat.st_ino):
                raise OSError("file sync target changed while opening")
            identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if windows_finalized and identity in self._windows_finalized_files:
                # make_read_only already flushed the writable handle after the
                # Windows read-only attribute was applied.
                self._windows_finalized_files.remove(identity)
                return
            self._fsync(descriptor)
        finally:
            self._close(descriptor)

    def sync_directory(self, path: Path | str) -> None:
        directory = Path(path)
        directory_metadata = os.lstat(directory)
        if not stat.S_ISDIR(directory_metadata.st_mode) or is_reparse_point(
            directory_metadata
        ):
            raise OSError("directory sync target is not a real directory")
        if self._directory_sync is not None:
            self._directory_sync(directory)
            return
        if os.name == "nt":
            _sync_windows_directory(directory)
            return

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = self._open(directory, flags)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(descriptor_stat.st_mode) or is_reparse_point(
                descriptor_stat
            ):
                raise OSError("directory sync target is not a directory")
            path_stat = os.lstat(directory)
            if (
                not stat.S_ISDIR(path_stat.st_mode)
                or is_reparse_point(path_stat)
            ) or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != (path_stat.st_dev, path_stat.st_ino):
                raise OSError("directory sync target changed while opening")
            self._fsync(descriptor)
        finally:
            self._close(descriptor)

    def make_read_only(self, path: Path | str) -> None:
        target = Path(path)
        target_stat = os.lstat(target)
        if stat.S_ISLNK(target_stat.st_mode) or is_reparse_point(target_stat):
            raise OSError("read-only target must not be a symbolic link")
        if stat.S_ISDIR(target_stat.st_mode):
            mode = (
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
        elif stat.S_ISREG(target_stat.st_mode):
            mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        else:
            raise OSError("read-only target must be a regular file or directory")
        descriptor: int | None = None
        already_read_only = not bool(stat.S_IMODE(target_stat.st_mode) & 0o222)
        if (
            os.name == "nt"
            and stat.S_ISREG(target_stat.st_mode)
            and not already_read_only
        ):
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = self._open(target, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or is_reparse_point(opened)
                or (opened.st_dev, opened.st_ino)
                != (target_stat.st_dev, target_stat.st_ino)
            ):
                self._close(descriptor)
                raise OSError("read-only target changed while opening")
            self._fsync(descriptor)
        try:
            if not (os.name == "nt" and already_read_only):
                self._chmod(target, mode)
            if descriptor is not None:
                self._fsync(descriptor)
        finally:
            if descriptor is not None:
                self._close(descriptor)
        final_stat = os.lstat(target)
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            stat.S_IFMT(final_stat.st_mode),
        ) != (
            target_stat.st_dev,
            target_stat.st_ino,
            stat.S_IFMT(target_stat.st_mode),
        ):
            raise OSError("read-only target changed while permissions were applied")
        if os.name == "nt" and stat.S_ISREG(final_stat.st_mode):
            self._windows_finalized_files.add((final_stat.st_dev, final_stat.st_ino))


@runtime_checkable
class ReplaceOperation(Protocol):
    """Injectable atomic replacement surface."""

    def replace(self, source: Path | str, destination: Path | str) -> None: ...


class PlatformReplaceOperation:
    """Production ``os.replace`` adapter."""

    def __init__(
        self,
        replace_fn: Callable[[Path | str, Path | str], None] = os.replace,
    ) -> None:
        self._replace = replace_fn

    def replace(self, source: Path | str, destination: Path | str) -> None:
        self._replace(source, destination)


__all__ = [
    "chmod_no_follow",
    "FileInstanceLock",
    "InstanceLockBackend",
    "PlatformFileSync",
    "PlatformReplaceOperation",
    "PosixInstanceLockBackend",
    "ReplaceOperation",
    "WindowsInstanceLockBackend",
]
