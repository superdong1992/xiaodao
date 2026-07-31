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

from .atomic import FileSync, require_real_directory


_STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StreamCopyReceipt:
    size: int
    sha256: str


class FileBinaryStream:
    """BinaryStream backed by one already validated ordinary file."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode):
                raise ValueError("binary resource must be an ordinary file")
            self._handle: BinaryIO = os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise
        self._closed = False
        self._lock = threading.Lock()

    def read(self, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
            value = self._handle.read(max_bytes)
            if not isinstance(value, bytes) or len(value) > max_bytes:
                raise OSError("file stream violated the BinaryStream contract")
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
