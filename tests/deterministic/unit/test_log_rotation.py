from __future__ import annotations

import importlib.util
from argparse import Namespace
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.storage.log_rotation import (
    BoundedJsonlFile,
    JsonlFileHandler,
    bounded_json_line,
    jsonl_segment_paths,
)


def test_configuring_file_logging_creates_empty_log_and_preserves_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "journey.jsonl"
    writer = BoundedJsonlFile(target)
    assert target.is_file()
    assert target.read_bytes() == b""
    writer.write_line('{"event":"existing"}')
    original = target.read_bytes()
    handler = JsonlFileHandler(target)
    try:
        assert Path(handler.baseFilename) == target.absolute()
        assert target.read_bytes() == original
    finally:
        handler.close()


def test_utf8_bytes_and_backups_remain_bounded_after_many_rotations(tmp_path: Path) -> None:
    target = tmp_path / "debug.jsonl"
    writer = BoundedJsonlFile(target, max_bytes=1024, backup_count=4)
    for index in range(50):
        writer.write_line(json.dumps({"event": "测试", "index": index, "text": "中文" * 70},
                                    ensure_ascii=False))
    segments = jsonl_segment_paths(target)
    assert len(segments) == 5
    assert all(path.stat().st_size <= 1024 for path in segments)
    assert sum(path.stat().st_size for path in segments) <= 5 * 1024
    records = [json.loads(line) for path in segments for line in path.read_bytes().splitlines()]
    assert records[-1]["index"] == 49
    assert [record["index"] for record in records] == list(range(records[0]["index"], 50))


def test_oversized_event_remains_valid_json_with_identity_and_truncation(tmp_path: Path) -> None:
    target = tmp_path / "debug.jsonl"
    writer = BoundedJsonlFile(target, max_bytes=1024)
    original = {"event": "mcp.tool.started", "case_id": "00000000-0000-4000-8000-000000000001",
                "request_id": "request-1", "arguments": {"text": "中" * 100_000}}
    writer.write_line(json.dumps(original, ensure_ascii=False))
    record = json.loads(target.read_bytes())
    assert target.stat().st_size <= 1024
    assert record["case_id"] == original["case_id"]
    assert record["request_id"] == "request-1"
    assert record["event"] == "mcp.tool.started"
    assert record["log_truncation"]["truncated"] is True
    assert record["log_truncation"]["original_utf8_bytes"] > 1024
    assert len(record["log_truncation"]["sha256"]) == 64


def test_existing_unbounded_files_are_adopted_with_only_complete_tail_records(tmp_path: Path) -> None:
    target = tmp_path / "debug.jsonl"
    original = b"".join(json.dumps({"event": "old", "index": index, "text": "x" * 100}).encode() + b"\n"
                        for index in range(100))
    target.write_bytes(original)
    target.with_name("debug.jsonl.1").write_bytes(original)
    writer = BoundedJsonlFile(target, max_bytes=1024)
    assert writer.last_record()["index"] == 99
    for segment in jsonl_segment_paths(target):
        assert segment.stat().st_size <= 1024
        records = [json.loads(line) for line in segment.read_bytes().splitlines()]
        assert records[-1]["index"] == 99


def test_legacy_giant_line_leaves_explicit_loss_marker_instead_of_empty_log(tmp_path: Path) -> None:
    target = tmp_path / "debug.jsonl"
    original = json.dumps({"event": "old", "text": "x" * 20_000}).encode() + b"\n"
    target.write_bytes(original)
    writer = BoundedJsonlFile(target, max_bytes=1024)
    assert 0 < target.stat().st_size <= 1024
    marker = writer.last_record()
    assert marker["event"] == "logs.history.truncated"
    assert marker["log_truncation"]["original_utf8_bytes"] == len(original)
    assert marker["log_truncation"]["sequence_unavailable"] is True


def test_same_path_writers_serialize_rotation_across_threads(tmp_path: Path) -> None:
    target = tmp_path / "debug.jsonl"
    writers = [BoundedJsonlFile(target, max_bytes=4096) for _ in range(4)]
    barrier = threading.Barrier(4)

    def emit(worker: int) -> None:
        barrier.wait()
        for index in range(15):
            writers[worker].write_line(json.dumps({"worker": worker, "index": index, "text": "x" * 100}))

    threads = [threading.Thread(target=emit, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    records = [json.loads(line) for path in jsonl_segment_paths(target)
               for line in path.read_bytes().splitlines()]
    assert {(record["worker"], record["index"]) for record in records} == {
        (worker, index) for worker in range(4) for index in range(15)
    }
    assert len(records) == 60


def _relay_module():
    path = Path(__file__).resolve().parents[3] / "tools/test-flow/runtime-support/relay_service_journey.py"
    # tests/deterministic/unit is three parents below the repository root.
    spec = importlib.util.spec_from_file_location("rotation_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_testflow_relay_reads_every_segment_after_multiple_rotations(tmp_path: Path) -> None:
    module = _relay_module()
    target = tmp_path / "journey.jsonl"
    writer = BoundedJsonlFile(target, max_bytes=1024)
    writer.write_line(json.dumps({"sequence": 1, "text": "x" * 650}))
    source = module.RotatingSource(target)
    try:
        first = source.read(65536)
        assert source.read(65536) == b""
        for index in range(2, 5):
            writer.write_line(json.dumps({"sequence": index, "text": "x" * 650}))
        result = first
        while chunk := source.read(65536):
            result += chunk
        assert [json.loads(line)["sequence"] for line in result.splitlines()] == [1, 2, 3, 4]
    finally:
        source.close()


def test_testflow_relay_fails_if_unread_segments_were_evicted(tmp_path: Path) -> None:
    module = _relay_module()
    target = tmp_path / "debug.jsonl"
    writer = BoundedJsonlFile(target, max_bytes=1024)
    writer.write_line(json.dumps({"index": 0, "text": "x" * 650}))
    source = module.RotatingSource(target)
    try:
        source.read(65536)
        for index in range(1, 7):
            writer.write_line(json.dumps({"index": index, "text": "x" * 650}))
        with pytest.raises(module.SourceRotationGap):
            source.read(65536)
    finally:
        source.close()


def test_testflow_relay_pins_posix_inode_against_immediate_reuse(tmp_path: Path, monkeypatch) -> None:
    module = _relay_module()
    target = tmp_path / "debug.jsonl"
    original = SimpleNamespace(inode=101, data=b'{"index":0}\n', handles=0)
    retained = {target: original}
    descriptors = {}
    next_descriptor = iter(range(100_000, 200_000))
    real_open = Path.open
    real_fstat = module.os.fstat

    class OpenSegment(io.BytesIO):
        def __init__(self, segment):
            super().__init__(segment.data)
            self.segment = segment
            self.descriptor = next(next_descriptor)
            descriptors[self.descriptor] = segment
            segment.handles += 1

        def fileno(self):
            return self.descriptor

        def close(self):
            if not self.closed:
                self.segment.handles -= 1
                del descriptors[self.descriptor]
            super().close()

    def open_segment(path, *args, **kwargs):
        if path in retained:
            return OpenSegment(retained[path])
        return real_open(path, *args, **kwargs)

    def metadata(descriptor):
        segment = descriptors.get(descriptor)
        if segment is None:
            return real_fstat(descriptor)
        return SimpleNamespace(st_dev=1, st_ino=segment.inode, st_size=len(segment.data))

    monkeypatch.setattr(module, "PIN_SOURCE_INODE", True, raising=False)
    monkeypatch.setattr(Path, "open", open_segment)
    monkeypatch.setattr(module.os, "fstat", metadata)
    source = module.RotatingSource(target)
    monkeypatch.setattr(source, "_segment_snapshot", lambda: [
        (path, (1, segment.inode)) for path, segment in retained.items()
    ])
    try:
        assert source.read(65536) == original.data
        # Emulate ext4's immediate reuse after the last open descriptor closes.
        # The original implementation therefore mistakes this later segment
        # for the consumed one and silently reads from the old byte offset.
        replacement = SimpleNamespace(
            inode=original.inode if original.handles == 0 else original.inode + 1,
            data=b'{"index":6,"text":"new segment after eviction"}\n',
            handles=0,
        )
        retained[target] = replacement
        with pytest.raises(module.SourceRotationGap, match="no longer retained"):
            source.read(65536)
    finally:
        source.close()
    assert original.handles == 0
    assert not descriptors


def test_testflow_relay_resumes_partial_segment_at_same_offset_after_rotations(tmp_path: Path) -> None:
    module = _relay_module()
    target = tmp_path / "journey.jsonl"
    writer = BoundedJsonlFile(target, max_bytes=1024)
    lines = [json.dumps({"sequence": index, "text": "x" * 650}) for index in range(1, 5)]
    writer.write_line(lines[0])
    source = module.RotatingSource(target)
    try:
        result = source.read(37)
        assert result == (lines[0] + "\n").encode()[:37]
        for line in lines[1:]:
            writer.write_line(line)
        while chunk := source.read(37):
            result += chunk
        assert result == ("\n".join(lines) + "\n").encode()
    finally:
        source.close()


def test_testflow_relay_rejects_same_inode_truncated_below_read_offset(tmp_path: Path) -> None:
    module = _relay_module()
    target = tmp_path / "debug.jsonl"
    target.write_bytes(b'{"event":"old"}\n')
    source = module.RotatingSource(target)
    try:
        assert source.read(10) == b'{"event":"'
        previous = target.stat()
        with target.open("r+b") as stream:
            stream.truncate(0)
        current = target.stat()
        assert (current.st_dev, current.st_ino) == (previous.st_dev, previous.st_ino)
        with pytest.raises(module.SourceRotationGap, match="truncated before its read offset"):
            source.read(65536)
    finally:
        source.close()


def test_testflow_relay_rejects_truncated_evidence_instead_of_passing(tmp_path: Path, monkeypatch) -> None:
    module = _relay_module()
    target = tmp_path / "debug.jsonl"
    target.write_bytes(bounded_json_line(json.dumps({"event": "mcp.tool.started", "text": "x" * 100_000}),
                                         max_bytes=1024))
    arguments = Namespace(source=target, events=tmp_path / "events.jsonl", raw=tmp_path / "raw.jsonl",
                          receipt=tmp_path / "receipt.json", stop=tmp_path / "stop", run_id="run-1",
                          producer_id="service-1", mode="diagnostics", allow_empty=False)
    arguments.stop.touch()
    monkeypatch.setattr(module, "_arguments", lambda: arguments)
    assert module.main() == 1
    assert json.loads(arguments.receipt.read_bytes())["code"] == "SOURCE_EVENT_TRUNCATED"


def test_testflow_relay_rejects_mixed_rotation_snapshot_before_skipping_a_segment(tmp_path: Path, monkeypatch) -> None:
    module = _relay_module()
    source = module.RotatingSource(tmp_path / "debug.jsonl")
    snapshots = iter([
        [(tmp_path / "debug.jsonl.2", (1, 10)), (tmp_path / "debug.jsonl", (1, 12))],
        [(tmp_path / "debug.jsonl.2", (1, 10)), (tmp_path / "debug.jsonl.1", (1, 11)),
         (tmp_path / "debug.jsonl", (1, 12))],
    ])
    monkeypatch.setattr(source, "_segment_snapshot", lambda: next(snapshots))
    with pytest.raises(module.SourceRotationGap, match="complete segment snapshots"):
        source.read(65536)
    assert source.identity is None
    assert source.offset == 0
