"""Small atomic filesystem primitives shared by the S02 adapters."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO, Protocol


class FileSync(Protocol):
    def sync_file(self, path_or_handle: Path | BinaryIO) -> None: ...

    def sync_directory(self, path: Path) -> None: ...

    def make_read_only(self, path: Path) -> None: ...


class Replacer(Protocol):
    def replace(self, source: Path, destination: Path) -> None: ...


def write_synced_file(path: Path, data: bytes, file_sync: FileSync) -> None:
    """Create one new ordinary file, flush it, and make its bytes durable."""

    if not isinstance(data, bytes):
        raise TypeError("data must be immutable bytes")
    path = Path(path)
    require_real_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("new storage node is not an ordinary file")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                written = handle.write(view[offset:])
                if written is None or written <= 0:
                    raise OSError("zero-length write while creating storage file")
                offset += written
            handle.flush()
            # Re-opening the unique path lets both real and recording sync
            # adapters retain the physical filename in their observations.
            # The caller validates the published bytes after replacement.
            file_sync.sync_file(path)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    destination: Path,
    data: bytes,
    *,
    temporary_path: Path,
    file_sync: FileSync,
    replacer: Replacer,
) -> None:
    """Durably write bytes and atomically replace one same-volume destination.

    The caller supplies the temporary name so Clock/IdGenerator based tests can
    keep every filesystem transition deterministic.  A failed operation leaves
    the temporary file available for the S02 retention cleaner.
    """

    destination = Path(destination)
    temporary_path = Path(temporary_path)
    if destination.parent.resolve(strict=False) != temporary_path.parent.resolve(
        strict=False
    ):
        raise ValueError("atomic replacement temporary file must share the destination directory")
    write_synced_file(temporary_path, data, file_sync)
    replacer.replace(temporary_path, destination)
    file_sync.sync_directory(destination.parent)


def require_ordinary_file(path: Path) -> os.stat_result:
    """Return lstat for one ordinary, non-link file."""

    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("storage node must be an ordinary file")
    return value


def require_real_directory(path: Path) -> os.stat_result:
    """Return lstat for one real directory, rejecting links."""

    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError("storage node must be a real directory")
    return value


def read_stable_file_bytes(path: Path) -> bytes:
    """Read one stable ordinary file without following its final path.

    The descriptor identity and metadata are compared before and after the
    read so callers never accept bytes from a path that was exchanged during
    validation.
    """

    path = Path(path)
    path_metadata = require_ordinary_file(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("storage node is not an ordinary file")
        if (before.st_dev, before.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise OSError("storage file changed while it was opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    final_path_metadata = require_ordinary_file(path)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise OSError("storage file changed while it was read")
    if (after.st_dev, after.st_ino) != (
        final_path_metadata.st_dev,
        final_path_metadata.st_ino,
    ):
        raise OSError("storage file path changed while it was read")
    payload = b"".join(chunks)
    if len(payload) != after.st_size:
        raise OSError("storage file size changed while it was read")
    return payload


def finalize_read_only_file(path: Path, file_sync: FileSync) -> None:
    """Idempotently apply the formal-file durability boundary."""

    require_ordinary_file(path)
    file_sync.make_read_only(path)
    file_sync.sync_file(path)


def finalize_read_only_tree(root: Path, file_sync: FileSync) -> None:
    """Finalize a directory resource from leaves to its root."""

    require_real_directory(root)
    directories = [root]
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in names:
            candidate = current_path / name
            require_real_directory(candidate)
            directories.append(candidate)
        for name in files:
            finalize_read_only_file(current_path / name, file_sync)
    for directory in sorted(
        directories,
        key=lambda value: len(value.relative_to(root).parts),
        reverse=True,
    ):
        file_sync.make_read_only(directory)
        file_sync.sync_directory(directory)


def is_read_only(path: Path) -> bool:
    """Return whether no write bit is present for user/group/other."""

    return not bool(path.stat(follow_symlinks=False).st_mode & 0o222)


__all__ = [
    "FileSync",
    "Replacer",
    "atomic_write_bytes",
    "finalize_read_only_file",
    "finalize_read_only_tree",
    "is_read_only",
    "read_stable_file_bytes",
    "require_ordinary_file",
    "require_real_directory",
    "write_synced_file",
]
