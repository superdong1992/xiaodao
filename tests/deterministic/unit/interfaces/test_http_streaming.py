from __future__ import annotations

import asyncio
import hashlib
import tracemalloc

import pytest

from problem_locator.interfaces.http_streaming import (
    AsyncRequestBinaryStream,
    iterate_binary_stream,
)
from tests.deterministic.contracts.fakes import InMemoryBinaryStream
from tests.deterministic.contracts.scenario_fakes import CountingBinaryStream
from tests.deterministic.unit.storage.fakes import FakeFileSync
from problem_locator.storage.streams import copy_binary_stream


def test_async_request_is_exposed_as_bounded_forward_only_stream() -> None:
    async def scenario() -> None:
        async def source():
            yield b"abc"
            yield b"defgh"

        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        chunks = [
            await asyncio.to_thread(stream.read, 2),
            await asyncio.to_thread(stream.read, 2),
            await asyncio.to_thread(stream.read, 4),
            await asyncio.to_thread(stream.read, 4),
        ]
        assert chunks == [b"ab", b"cd", b"efgh", b""]
        assert await asyncio.to_thread(stream.read, 1) == b""
        assert stream.max_buffered_bytes <= 8
        await stream.aclose()
        assert stream.closed
        with pytest.raises(ValueError, match="closed"):
            await asyncio.to_thread(stream.read, 1)

    asyncio.run(scenario())


def test_request_bridge_caps_reads_and_ignores_empty_asgi_frames() -> None:
    async def scenario() -> None:
        payload = b"x" * (200 * 1024)

        async def source():
            yield b""
            yield payload

        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.to_thread(stream.read, len(payload))
            if chunk == b"":
                break
            chunks.append(chunk)
        assert b"".join(chunks) == payload
        assert max(map(len, chunks)) == len(payload)
        assert stream.max_buffered_bytes == 2 * len(payload)
        await stream.aclose()

    asyncio.run(scenario())


def test_abort_unblocks_worker_before_async_source_close() -> None:
    async def scenario() -> None:
        source_entered = asyncio.Event()
        never = asyncio.Event()

        async def source():
            source_entered.set()
            await never.wait()
            yield b"unreachable"

        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        worker = asyncio.create_task(asyncio.to_thread(stream.read, 1024))
        await source_entered.wait()
        await stream.abort()
        with pytest.raises(ConnectionError, match="aborted"):
            await worker
        await stream.aclose()
        assert stream.closed

    asyncio.run(scenario())


def test_small_upload_frames_coalesce_without_retaining_a_previous_batch(tmp_path) -> None:
    async def scenario() -> None:
        frame = b"x" * 8192
        frame_count = 512

        async def source():
            for _ in range(frame_count):
                yield frame

        # Exclude executor initialization and the transport-owned frame.
        await asyncio.to_thread(lambda: None)
        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        tracemalloc.start()
        try:
            receipt = await asyncio.to_thread(copy_binary_stream, stream, tmp_path / "upload",
                file_sync=FakeFileSync(), byte_limit=frame_count * len(frame))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            await stream.aclose()
        assert receipt.size == frame_count * len(frame)
        expected = hashlib.sha256()
        for _ in range(frame_count):
            expected.update(frame)
        assert receipt.sha256 == expected.hexdigest()
        assert len(stream.read_requests) == 5  # Four 1 MiB batches plus EOF.
        assert stream.max_buffered_bytes <= 2 * 1024 * 1024
        # Payload buffers <= 2 MiB; allow 64 KiB for Python tasks, file objects,
        # futures and tracing metadata. Retaining the last batch adds 1 MiB.
        assert peak < 2 * 1024 * 1024 + 64 * 1024

    asyncio.run(scenario())


def test_request_stream_propagates_disconnect_and_closes() -> None:
    async def scenario() -> None:
        async def source():
            yield b"first"
            raise ConnectionError("client disconnected")

        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        with pytest.raises(ConnectionError, match="disconnected"):
            await asyncio.to_thread(stream.read, 10)
        await stream.aclose()
        assert stream.close_calls >= 1

    asyncio.run(scenario())


def test_download_iterator_closes_on_eof_and_midstream_failure() -> None:
    async def collect(source: InMemoryBinaryStream) -> bytes:
        return b"".join([chunk async for chunk in iterate_binary_stream(source, chunk_bytes=3)])

    complete = InMemoryBinaryStream(b"abcdefgh")
    assert asyncio.run(collect(complete)) == b"abcdefgh"
    assert complete.closed
    assert complete.read_requests == [3, 3, 3, 3]

    failed = InMemoryBinaryStream(b"abcdef", fail_on_read_number=2)
    with pytest.raises(OSError, match="injected"):
        asyncio.run(collect(failed))
    assert failed.closed


@pytest.mark.parametrize(
    "logical_size",
    [
        2_684_354_560,
        5_368_709_120,
    ],
)
def test_multi_gib_boundaries_use_counting_stream_without_large_allocation(
    logical_size: int,
) -> None:
    async def count(source: CountingBinaryStream) -> int:
        observed = 0
        async for chunk in iterate_binary_stream(source, chunk_bytes=1024 * 1024):
            observed += len(chunk)
        return observed

    stream = CountingBinaryStream(logical_size)
    assert asyncio.run(count(stream)) == logical_size
    assert stream.returned_logical_bytes == logical_size
    assert stream.read_calls == logical_size // (1024 * 1024) + 1
    assert stream.closed


@pytest.mark.parametrize("invalid", [0, -1, True])
def test_request_stream_rejects_invalid_read_sizes(invalid: int) -> None:
    async def scenario() -> None:
        async def source():
            yield b"value"

        stream = AsyncRequestBinaryStream(source(), loop=asyncio.get_running_loop())
        with pytest.raises(ValueError):
            await asyncio.to_thread(stream.read, invalid)
        await stream.aclose()

    asyncio.run(scenario())
