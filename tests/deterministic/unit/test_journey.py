from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from problem_locator.contracts import JobType
from problem_locator.diagnostics import bind_diagnostics
from problem_locator.journey import (
    JourneyEvent,
    configure_journey,
    journey_enabled,
    record_journey_event,
)
from problem_locator.storage import log_rotation


CASE_ID = "00000000-0000-4000-8000-000000000001"
JOB_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _reset_journey() -> None:
    configure_journey()
    yield
    configure_journey()


def test_journey_uses_fixed_envelope_context_and_contiguous_sequence() -> None:
    stream = io.StringIO()
    configure_journey(stream=stream)

    with bind_diagnostics(
        correlation_id="correlation-1",
        request_id="request-1",
    ):
        record_journey_event(
            "case.created",
            case_id=CASE_ID,
            data={"问题": "库存 RPC 超时"},
        )
        record_journey_event(
            "job.queued",
            case_id=CASE_ID,
            job_id=JOB_ID,
            job_type=JobType.ROUTE,
            duration_ms=1,
            data={"accepted": True},
        )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["sequence"] for event in events] == [1, 2]
    assert set(events[0]) == {
        "schema_version",
        "sequence",
        "timestamp",
        "level",
        "event",
        "correlation_id",
        "request_id",
        "case_id",
        "job_id",
        "job_type",
        "outcome_id",
        "duration_ms",
        "data",
    }
    assert events[0]["correlation_id"] == "correlation-1"
    assert events[0]["request_id"] == "request-1"
    assert events[0]["data"] == {"问题": "库存 RPC 超时"}
    assert events[1]["duration_ms"] == 1.0


def test_journey_file_is_utf8_jsonl_and_parent_is_created(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "journey.jsonl"
    configure_journey(log_file=target)

    record_journey_event("case.created", case_id=CASE_ID, data={"值": "中文"})
    configure_journey()

    assert target.read_bytes().endswith(b"\n")
    assert json.loads(target.read_text(encoding="utf-8"))["data"] == {"值": "中文"}


def test_journey_writer_serializes_concurrent_threads_without_gaps() -> None:
    stream = io.StringIO()
    configure_journey(stream=stream)
    barrier = threading.Barrier(8)

    def emit(worker: int) -> None:
        barrier.wait()
        for index in range(50):
            record_journey_event(
                "job.stage.completed",
                case_id=CASE_ID,
                job_id=JOB_ID,
                job_type="DIAGNOSE",
                data={"worker": worker, "index": index},
            )

    threads = [threading.Thread(target=emit, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(events) == 400
    assert [event["sequence"] for event in events] == list(range(1, 401))


def test_runtime_write_failure_disables_journey_without_raising() -> None:
    class FailingStream(io.StringIO):
        def write(self, value: str) -> int:
            raise OSError("write failed")

    configure_journey(stream=FailingStream())

    assert record_journey_event("case.created", case_id=CASE_ID) is None
    assert not journey_enabled()
    assert record_journey_event("case.created", case_id=CASE_ID) is None


def test_journey_rejects_stream_and_file_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be configured together"):
        configure_journey(stream=io.StringIO(), log_file=tmp_path / "journey.jsonl")


def test_journey_rotation_preserves_sequence_on_reconfiguration_and_giant_records(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(log_rotation, "LOG_FILE_MAX_BYTES", 1024)
    target = tmp_path / "journey.jsonl"
    configure_journey(log_file=target)
    for index in range(12):
        event = record_journey_event("job.stage.completed", case_id=CASE_ID, job_id=JOB_ID,
                                     data={"index": index, "text": "中" * 20_000})
        assert event is not None
        assert event.data["log_truncation"]["truncated"] is True
    configure_journey(log_file=target)
    final = record_journey_event("case.status.changed", case_id=CASE_ID,
                                 data={"to_status": "COMPLETED"})
    configure_journey()
    assert final.sequence == 13
    segments = log_rotation.jsonl_segment_paths(target)
    assert len(segments) == 5
    assert all(segment.stat().st_size <= 1024 for segment in segments)
    events = [JourneyEvent.model_validate_json(line, strict=True)
              for segment in segments for line in segment.read_bytes().splitlines()]
    assert [event.sequence for event in events] == list(range(events[0].sequence, 14))
    assert all(event.case_id == CASE_ID for event in events)


def test_unrecoverable_legacy_giant_journey_never_restarts_at_sequence_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(log_rotation, "LOG_FILE_MAX_BYTES", 1024)
    target = tmp_path / "journey.jsonl"
    target.write_bytes((json.dumps({"sequence": 87, "data": {"text": "x" * 20_000}}) + "\n").encode())
    with pytest.raises(ValueError, match="无法恢复事件序号"):
        configure_journey(log_file=target)
    assert not journey_enabled()
    marker = json.loads(target.read_bytes())
    assert "sequence" not in marker
    assert marker["log_truncation"]["sequence_unavailable"] is True
    with pytest.raises(ValueError, match="无法恢复事件序号"):
        configure_journey(log_file=target)
    assert json.loads(target.read_bytes()) == marker
