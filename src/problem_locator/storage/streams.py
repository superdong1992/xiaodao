"""Forward-only file streams and bounded staging copy primitives."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from problem_locator.contracts.ports import BinaryStream

from .atomic import FileSync, is_reparse_point, require_real_directory


_STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StreamCopyReceipt:
    size: int
    sha256: str


class FileBinaryStream:
    """BinaryStream backed by one already validated ordinary file."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path_metadata = path.lstat()
        if not stat.S_ISREG(path_metadata.st_mode) or is_reparse_point(path_metadata):
            raise ValueError("binary resource must be an ordinary file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode) or is_reparse_point(value):
                raise ValueError("binary resource must be an ordinary file")
            if (value.st_dev, value.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise OSError("binary resource changed while it was opened")
            final_path_metadata = path.lstat()
            if (
                not stat.S_ISREG(final_path_metadata.st_mode)
                or is_reparse_point(final_path_metadata)
            ) or (
                final_path_metadata.st_dev,
                final_path_metadata.st_ino,
            ) != (value.st_dev, value.st_ino):
                raise OSError("binary resource path changed while it was opened")
            self._handle: BinaryIO = os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise
        self._path = path
        self._initial_metadata = value
        self._closed = False
        self._lock = threading.Lock()

    def _validate_stable_at_eof(self) -> None:
        descriptor_metadata = os.fstat(self._handle.fileno())
        path_metadata = self._path.lstat()
        descriptor_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
        )
        # CPython on Windows can expose a handle ctime that differs from the
        # by-path ctime for the same stable file.  Keep ctime in the
        # descriptor-to-descriptor mutation check, but use only fields with
        # consistent path/handle semantics for the cross-view comparison.
        path_fields = tuple(
            field
            for field in descriptor_fields
            if not (os.name == "nt" and field == "st_ctime_ns")
        )
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or is_reparse_point(path_metadata)
            or any(
                getattr(descriptor_metadata, field)
                != getattr(self._initial_metadata, field)
                for field in descriptor_fields
            )
            or any(
                getattr(path_metadata, field)
                != getattr(descriptor_metadata, field)
                for field in path_fields
            )
        ):
            raise OSError("binary resource changed while it was read")

    def read(self, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
            value = self._handle.read(max_bytes)
            if not isinstance(value, bytes) or len(value) > max_bytes:
                raise OSError("file stream violated the BinaryStream contract")
            if not value:
                self._validate_stable_at_eof()
            return value

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.close()

    def __enter__(self) -> Self:
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def copy_binary_stream(
    stream: BinaryStream,
    destination: Path,
    *,
    file_sync: FileSync,
    byte_limit: int | None,
) -> StreamCopyReceipt:
    """Consume a BinaryStream exactly once into a synced staging file.

    At a finite limit, the next read is capped to ``remaining + 1`` so an
    oversized stream is rejected as soon as its first forbidden byte arrives.
    The caller retains ownership of and must close *stream*.
    """

    destination = Path(destination)
    require_real_directory(destination.parent)
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as handle:
        while True:
            request_size = _STREAM_CHUNK_BYTES
            if byte_limit is not None:
                remaining = byte_limit - size
                if remaining < 0:
                    raise ValueError("resource exceeds its byte limit")
                request_size = min(request_size, remaining + 1)
            chunk = stream.read(request_size)
            if not isinstance(chunk, bytes):
                raise TypeError("BinaryStream.read must return immutable bytes")
            if len(chunk) > request_size:
                raise ValueError("BinaryStream returned more than max_bytes")
            if not chunk:
                break
            size += len(chunk)
            if byte_limit is not None and size > byte_limit:
                raise ValueError("resource exceeds its byte limit")
            handle.write(chunk)
            digest.update(chunk)
        handle.flush()
        file_sync.sync_file(handle)
    return StreamCopyReceipt(size=size, sha256=digest.hexdigest())


def hash_file(path: Path) -> StreamCopyReceipt:
    """Hash a real ordinary file without following a final symlink."""

    digest = hashlib.sha256()
    size = 0
    with FileBinaryStream(path) as stream:
        while True:
            chunk = stream.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return StreamCopyReceipt(size=size, sha256=digest.hexdigest())


__all__ = ["FileBinaryStream", "StreamCopyReceipt", "copy_binary_stream", "hash_file"]
