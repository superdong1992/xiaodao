from __future__ import annotations

import json

from problem_locator.runtime.agent_telemetry import (
    AgentStreamTelemetry,
    TelemetryTeeSink,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.flushed = False

    def write(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        return


def _line(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def test_stream_json_metadata_is_bounded_and_content_free() -> None:
    clock = _Clock()
    telemetry = AgentStreamTelemetry(monotonic=clock)
    telemetry.prompt_started(len("private prompt".encode()))
    clock.value = 0.025
    telemetry.prompt_finished(completed=True)

    clock.value = 0.100
    system = _line({"type": "system", "subtype": "init", "session_id": "private-session"})
    telemetry.write(system[:7])
    telemetry.write(system[7:])
    clock.value = 0.200
    telemetry.write(
        _line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "private answer"},
                        {
                            "type": "tool_use",
                            "id": "private-call-id",
                            "name": "mcp__problem-locator__diagnose",
                            "input": {"secret": "private argument"},
                        },
                    ]
                },
            }
        )
    )
    clock.value = 0.550
    telemetry.write(
        _line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "private-call-id",
                            "content": "private result",
                        }
                    ]
                },
            }
        )
    )
    clock.value = 0.800
    telemetry.write(
        _line(
            {
                "type": "result",
                "duration_ms": 720,
                "duration_api_ms": 600,
                "num_turns": 2,
                "result": "private final body",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 5,
                },
            }
        )
    )

    snapshot = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")

    assert snapshot["stream_status"] == "COMPLETE"
    assert snapshot["stream_reason"] is None
    assert snapshot["prompt_write_ms"] == 25.0
    assert snapshot["cli_duration_ms"] == 720.0
    assert snapshot["model_api_duration_ms"] == 600.0
    assert snapshot["turn_count"] == 2
    assert snapshot["usage_counts"] == {
        "cache_creation": 3,
        "cache_read": 5,
        "input": 11,
        "output": 7,
    }
    assert snapshot["block_observations"]["thinking"]["block_count"] == 1
    assert snapshot["block_observations"]["text"]["block_count"] == 1
    assert snapshot["tool_observed_union_ms"] == 350.0
    assert snapshot["tool_observations"] == [
        {
            "name": "mcp__problem-locator__diagnose",
            "call_count": 1,
            "completed_count": 1,
            "incomplete_count": 0,
            "observed_duration_ms": 350.0,
            "max_call_ms": 350.0,
        }
    ]
    encoded = json.dumps(snapshot, ensure_ascii=False)
    for forbidden in (
        "private prompt",
        "private reasoning",
        "private answer",
        "private argument",
        "private result",
        "private final body",
        "private-call-id",
        "private-session",
    ):
        assert forbidden not in encoded


def test_stream_json_degrades_without_affecting_execution_sink() -> None:
    plain = AgentStreamTelemetry()
    sink = _Sink()
    tee = TelemetryTeeSink(sink, plain)
    tee.write(b"ordinary command output\n")
    tee.flush()
    unsupported = plain.snapshot(
        diagnosis_mode="SPECIALIZED",
        backend_status="SUCCESS",
    )

    assert bytes(sink.data) == b"ordinary command output\n"
    assert sink.flushed
    assert unsupported["stream_status"] == "UNAVAILABLE"
    assert unsupported["stream_reason"] == "UNSUPPORTED_STREAM_JSON"

    partial = AgentStreamTelemetry()
    partial.write(_line({"type": "system", "subtype": "init"}))
    partial.write(b"{broken}\n")
    summary = partial.snapshot(diagnosis_mode="SPECIALIZED", backend_status="FAILED")
    assert summary["stream_status"] == "PARTIAL"
    assert summary["stream_reason"] == "STREAM_JSON_MALFORMED"


def test_stream_json_line_limit_and_missing_terminal_are_explicit() -> None:
    limited = AgentStreamTelemetry(line_limit_bytes=16)
    limited.write(b"x" * 17 + b"\n")
    summary = limited.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
    assert summary["stream_status"] == "UNAVAILABLE"
    assert summary["stream_reason"] == "STREAM_JSON_LINE_LIMIT"

    missing = AgentStreamTelemetry()
    missing.write(_line({"type": "system", "subtype": "init"}))
    summary = missing.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
    assert summary["stream_status"] == "PARTIAL"
    assert summary["stream_reason"] == "TERMINAL_RESULT_MISSING"


def test_backend_not_started_is_distinct_from_unsupported_output() -> None:
    telemetry = AgentStreamTelemetry()
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="FAILED")
    assert summary["stream_status"] == "UNAVAILABLE"
    assert summary["stream_reason"] == "BACKEND_NOT_STARTED"
