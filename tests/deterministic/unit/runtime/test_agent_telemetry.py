from __future__ import annotations

import json
import pytest

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

    snapshot = telemetry.snapshot(
        diagnosis_mode="GENERIC",
        backend_status="SUCCESS",
        backend_phase="GENERIC",
        backend_invocation_id="call-1",
    )

    assert snapshot["stream_status"] == "COMPLETE"
    assert snapshot["backend_phase"] == "GENERIC"
    assert snapshot["backend_invocation_id"] == "call-1"
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


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_large_successful_result_survives_telemetry_sampling_limit(ending):
    answer = json.dumps({"report": "证据" * (256 * 1024)}, ensure_ascii=False)
    event = _line({"type": "result", "subtype": "success", "is_error": False, "result": answer})[:-1] + ending
    assert len(event) > 1024 * 1024
    telemetry = AgentStreamTelemetry(output_limit_bytes=len(event))
    for offset in range(0, len(event), 4093):
        telemetry.write(event[offset:offset + 4093])
    assert telemetry.final_result == answer
    assert telemetry.permits_file_access("none")
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")
    assert summary["stream_reason"] == "STREAM_JSON_LINE_LIMIT"
    assert summary["recognized_event_count"] == 1
    assert "证据" not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.parametrize("name,allowed", [("Read", True), ("Write", False)])
def test_large_assistant_event_preserves_tool_audit_even_when_text_is_not_sampled(name, allowed):
    telemetry = AgentStreamTelemetry(line_limit_bytes=128)
    telemetry.write(_line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "private text" * 100},
        {"type": "tool_use", "id": "tool-1", "name": name, "input": {"path": "private-path"}},
    ]}}))
    telemetry.write(_line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "private result"},
    ]}}))
    assert telemetry.permits_file_access("read-only") is allowed
    assert not telemetry.permits_file_access("none")
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")
    assert summary["block_observations"]["text"]["block_count"] == 0
    assert summary["tool_observations"][0]["name"] == name
    assert summary["tool_observations"][0]["completed_count"] == 1


def test_stream_phase_output_budget_invalidates_previously_received_terminal():
    event = _line({"type": "result", "subtype": "success", "is_error": False, "result": "{}"})
    telemetry = AgentStreamTelemetry(output_limit_bytes=len(event))
    telemetry.write(event)
    assert telemetry.final_result == "{}"
    telemetry.write(b"x")
    assert telemetry.output_limit_exceeded
    assert telemetry.final_result is None
    assert not telemetry.permits_file_access("read-only")
    assert telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="FAILED")["stream_reason"] == "STREAM_JSON_OUTPUT_LIMIT"


@pytest.mark.parametrize("events", [
    [{"subtype": "success", "is_error": True, "result": "{}"}],
    [{"subtype": "error", "is_error": False, "result": "{}"}],
    [{"subtype": "success", "is_error": False, "result": {}}],
    [{"subtype": "success", "is_error": False, "result": "{}"}] * 2,
    [{"subtype": "error", "is_error": True}, {"subtype": "success", "is_error": False, "result": "{}"}],
])
def test_only_one_unambiguous_successful_terminal_is_accepted(events):
    telemetry = AgentStreamTelemetry(line_limit_bytes=16)
    for event in events:
        telemetry.write(_line({"type": "result", **event}).replace(b"\n", b"\r\n"))
    assert telemetry.final_result is None


def test_malformed_or_disabled_stream_cannot_claim_clean_file_access_audit():
    telemetry = AgentStreamTelemetry()
    telemetry.write(b'{"type":"assistant","type":"result"}\n')
    assert not telemetry.permits_file_access("none")
    disabled = AgentStreamTelemetry()
    disabled.disable()
    assert not disabled.permits_file_access("read-only")


def _partial_tool(name="Read", tool_id="tool-1"):
    return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}}


def _successful_terminal():
    return {"type": "result", "subtype": "success", "is_error": False, "result": "{}"}


@pytest.mark.parametrize("name,allowed", [("Read", True), ("Write", False)])
@pytest.mark.parametrize("wrapped", [True, False])
def test_partial_tool_events_cannot_bypass_file_access_audit(name, allowed, wrapped):
    telemetry = AgentStreamTelemetry(line_limit_bytes=32)
    event = _partial_tool(name)
    telemetry.write(_line(event if wrapped else event["event"]))
    telemetry.write(_line(_successful_terminal()))
    assert telemetry.final_result == "{}"
    assert not telemetry.permits_file_access("none")
    assert telemetry.permits_file_access("read-only") is allowed
    tools = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")["tool_observations"]
    assert len(tools) == 1 and tools[0]["name"] == name and tools[0]["call_count"] == 1


@pytest.mark.parametrize("complete_first", [False, True])
def test_partial_and_complete_tool_events_count_one_call_and_preserve_first_timing(complete_first):
    clock = _Clock()
    telemetry = AgentStreamTelemetry(monotonic=clock)
    partial = _partial_tool()
    complete = {"type": "assistant", "message": {"content": [partial["event"]["content_block"]]}}
    for time_value, event in zip((0.1, 0.2), (complete, partial) if complete_first else (partial, complete)):
        clock.value = time_value
        telemetry.write(_line(event))
    telemetry.write(_line({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"private-path"}'}}}))
    clock.value = 0.4
    telemetry.write(_line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "private-result"}]}}))
    telemetry.write(_line(complete))  # A repeated full message cannot reopen a completed call.
    telemetry.write(_line(_successful_terminal()))
    assert telemetry.permits_file_access("read-only")
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")
    tool, = summary["tool_observations"]
    assert (tool["call_count"], tool["completed_count"], tool["incomplete_count"]) == (1, 1, 0)
    assert tool["observed_duration_ms"] == pytest.approx(300.0)
    assert "private" not in json.dumps(summary) and "tool-1" not in json.dumps(summary)


@pytest.mark.parametrize("bad", [
    {"type": "assistant", "message": {"content": {"type": "tool_use", "name": "Write"}}},
    {"type": "assistant", "message": {"content": [None]}},
    {"type": "assistant", "message": {"content": [{"name": "Write", "id": "tool-1"}]}},
    {"type": "stream_event", "event": {"type": "content_block_start", "content_block": []}},
    {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}}},
    {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "tool-1", "name": None}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "{}"}}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "unseen", "content": "output"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Write"}]}},
])
def test_structurally_unreliable_tool_events_cannot_claim_clean_audit(bad):
    telemetry = AgentStreamTelemetry()
    telemetry.write(_line(bad))
    telemetry.write(_line(_successful_terminal()))
    assert not telemetry.permits_file_access("none")
    assert not telemetry.permits_file_access("read-only")
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")
    assert summary["stream_reason"] == "STREAM_JSON_MALFORMED"


def test_duplicate_tool_id_cannot_change_name_from_read_to_write():
    telemetry = AgentStreamTelemetry()
    telemetry.write(_line(_partial_tool("Read")))
    telemetry.write(_line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-1", "name": "Write", "input": {}}]}}))
    assert not telemetry.permits_file_access("read-only")


def test_ordinary_metadata_progress_and_text_deltas_remain_compatible():
    telemetry = AgentStreamTelemetry()
    for event in [
        {"type": "system", "subtype": "init"},
        {"type": "tool_progress", "tool_use_id": "metadata-only", "elapsed_time_seconds": 1},
        {"type": "future_metadata", "progress": 0.5},
        {"type": "user", "message": {"content": "literal text containing tool_use and Write"}},
        {"type": "stream_event", "event": {"type": "message_start", "message": {"content": []}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": 'literal {"type":"tool_use","name":"Write"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 1}}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        _successful_terminal(),
    ]:
        telemetry.write(_line(event))
    assert telemetry.permits_file_access("none") and telemetry.final_result == "{}"
    summary = telemetry.snapshot(diagnosis_mode="SPECIALIZED", backend_status="SUCCESS")
    assert summary["stream_status"] == "COMPLETE" and summary["tool_observations"] == []
