"""Bounded bridges between ASGI byte streams and S00 ``BinaryStream``."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
from types import TracebackType
from typing import Self

from problem_locator.contracts.ports import BinaryStream


HTTP_STREAM_CHUNK_BYTES = 1024 * 1024


class AsyncRequestBinaryStream:
    """Expose one async ASGI request iterator as a forward-only sync stream.

    The application command must execute in a worker thread.  Each synchronous
    ``read`` schedules exactly enough async iterator work back on the owning
    event loop and retains at most the unconsumed part of one ASGI chunk.
    """

    def __init__(
        self,
        source: AsyncIterator[bytes],
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._source = source.__aiter__()
        self._loop = loop
        self._buffer: memoryview | None = None
        self._buffer_offset = 0
        self._eof = False
        self._closed = False
        self._source_closed = False
        self._abort_event = asyncio.Event()
        self._read_lock = threading.Lock()
        self.read_requests: list[int] = []
        self.max_buffered_bytes = 0
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def _buffered_bytes(self) -> int:
        if self._buffer is None:
            return 0
        return len(self._buffer) - self._buffer_offset

    async def _next_chunk(self) -> bytes | None:
        next_chunk = asyncio.create_task(anext(self._source))
        aborted = asyncio.create_task(self._abort_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (next_chunk, aborted),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if aborted in done:
                next_chunk.cancel()
                with suppress(StopAsyncIteration, asyncio.CancelledError):
                    await next_chunk
                raise ConnectionError("request body stream was aborted")
            try:
                return next_chunk.result()
            except StopAsyncIteration:
                return None
        finally:
            aborted.cancel()
            with suppress(asyncio.CancelledError):
                await aborted

    async def _read_async(self, max_bytes: int) -> bytes:
        if self._eof:
            return b""
        bounded_max = min(max_bytes, HTTP_STREAM_CHUNK_BYTES)
        # Coalesce on the event loop: one thread handoff per MiB, independent
        # of how many tiny frames the HTTP transport emits. The bytearray and
        # returned immutable copy together occupy at most two MiB. The current
        # ASGI frame is borrowed as a memoryview, never copied in full.
        # Preallocate: bytearray.extend geometrically overallocates capacity.
        result = bytearray(bounded_max)
        result_view = memoryview(result)
        used = 0
        while used < bounded_max:
            if self._buffered_bytes() == 0:
                if self._eof:
                    break
                chunk = await self._next_chunk()
                if chunk is None:
                    self._eof = True
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("ASGI request stream yielded a non-bytes chunk")
                if not chunk:
                    continue
                self._buffer = memoryview(chunk)
                self._buffer_offset = 0
            count = min(bounded_max - used, self._buffered_bytes())
            end = self._buffer_offset + count
            result_view[used:used + count] = self._buffer[self._buffer_offset:end]
            used += count
            self._buffer_offset = end
            if end == len(self._buffer):
                self._buffer.release()
                self._buffer = None
                self._buffer_offset = 0
        self.max_buffered_bytes = max(self.max_buffered_bytes, bounded_max + used)
        return bytes(result_view[:used])

    def read(self, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with self._read_lock:
            if self._closed:
                raise ValueError("stream is closed")
            self.read_requests.append(max_bytes)
            future = asyncio.run_coroutine_threadsafe(
                self._read_async(max_bytes),
                self._loop,
            )
            return future.result()

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    async def abort(self) -> None:
        """Unblock an in-flight worker read after the ASGI task is cancelled."""

        self._abort_event.set()

    async def aclose(self) -> None:
        self.close()
        if self._source_closed:
            return
        self._source_closed = True
        close = getattr(self._source, "aclose", None)
        if close is not None:
            await close()

    def __enter__(self) -> Self:
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


async def iterate_binary_stream(
    stream: BinaryStream,
    *,
    chunk_bytes: int = HTTP_STREAM_CHUNK_BYTES,
) -> AsyncIterator[bytes]:
    """Read a frozen synchronous stream without blocking the ASGI event loop."""

    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    try:
        while True:
            chunk = await asyncio.to_thread(stream.read, chunk_bytes)
            if chunk == b"":
                break
            yield chunk
    finally:
        await asyncio.to_thread(stream.close)


__all__ = [
    "AsyncRequestBinaryStream",
    "HTTP_STREAM_CHUNK_BYTES",
    "iterate_binary_stream",
]
