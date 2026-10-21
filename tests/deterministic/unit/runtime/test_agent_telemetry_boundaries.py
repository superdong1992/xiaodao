"""Tool auditing and unterminated frames fail closed without hiding valid replay."""
from __future__ import annotations

import json

import pytest

from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry


def _write(telemetry, event, ending=b"\n"):
    telemetry.write(json.dumps(event).encode("utf-8") + ending)


def _terminal():
    return {"type": "result", "subtype": "success", "is_error": False, "result": "{}"}


def _complete(name, fields):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-1", "name": name, **fields},
    ]}}


def _partial(name, fields):
    return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "tool-1", "name": name, **fields}}}


@pytest.mark.parametrize("policy", ["read_only", "all", "", None, False, 7, [], {}],
    ids=["typo", "unknown", "empty", "null", "boolean", "integer", "list", "object"])
@pytest.mark.parametrize("with_read", [False, True])
def test_unknown_policy_never_grants_tool_access(policy, with_read):
    telemetry = AgentStreamTelemetry()
    if with_read:
        _write(telemetry, _complete("Read", {"input": {"file_path": "inputs/report.md"}}))
    _write(telemetry, _terminal())
    assert telemetry.permits_file_access(policy) is False


@pytest.mark.parametrize("name", ["Read", "Grep", "StructuredOutput", "structuredoutput"])
@pytest.mark.parametrize("fields", [{}, {"input": None}, {"input": []}, {"input": "{}"},
    {"input": False}, {"input": 3}], ids=["missing", "null", "array", "string", "boolean", "number"])
@pytest.mark.parametrize("with_partial", [False, True])
def test_complete_tool_input_must_be_an_explicit_object_even_after_partial(name, fields, with_partial):
    telemetry = AgentStreamTelemetry()
    if with_partial:
        _write(telemetry, _partial(name, {"input": {}}))
    _write(telemetry, _complete(name, fields))
    _write(telemetry, _terminal())
    assert telemetry.permits_file_access("read-search") is False
    assert telemetry.final_result is None
    snapshot = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
    assert snapshot["stream_reason"] == "STREAM_JSON_MALFORMED"


@pytest.mark.parametrize("name", ["StructuredOutput", "structuredoutput"])
@pytest.mark.parametrize("fields", [{}, {"input": {}}], ids=["missing-placeholder", "empty-placeholder"])
@pytest.mark.parametrize("complete_first", [False, True])
@pytest.mark.parametrize("wrapped", [False, True], ids=["top-level", "wrapped"])
def test_valid_partial_and_complete_duplicates_keep_one_call(name, fields, complete_first, wrapped):
    telemetry = AgentStreamTelemetry()
    partial = _partial(name, fields)
    if not wrapped:
        partial = partial["event"]
    complete = _complete(name, {"input": {"answer": '中文与"引号"'}})
    for event in (complete, partial) if complete_first else (partial, complete):
        _write(telemetry, event)
    for event in (
        {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"answer":"ok"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ):
        _write(telemetry, {"type": "stream_event", "event": event} if wrapped else event)
    _write(telemetry, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "done"},
    ]}})
    _write(telemetry, complete)
    _write(telemetry, _terminal())
    assert telemetry.permits_file_access("none") is True
    assert telemetry.final_result == "{}"
    snapshot = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
    observed, = snapshot["tool_observations"]
    assert (observed["call_count"], observed["completed_count"], observed["incomplete_count"]) == (1, 1, 0)


@pytest.mark.parametrize("reset", [None, {"type": "content_block_stop", "index": 0},
    {"type": "message_stop"}, {"type": "message_start", "message": {"content": []}}],
    ids=["orphan-delta", "block-stopped", "message-stopped", "new-message"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["top-level", "wrapped"])
def test_top_level_partial_events_keep_the_same_tool_identity_boundary(reset, wrapped):
    telemetry = AgentStreamTelemetry()
    events = [] if reset is None else [_partial("StructuredOutput", {})["event"], reset]
    events.append({"type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "{}"}})
    for event in events:
        _write(telemetry, {"type": "stream_event", "event": event} if wrapped else event)
    _write(telemetry, _terminal())
    assert telemetry.permits_file_access("none") is False
    assert telemetry.final_result is None
    snapshot = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
    assert snapshot["stream_reason"] == "STREAM_JSON_MALFORMED"


@pytest.mark.parametrize("name", ["Read", "StructuredOutput"])
def test_a_tool_error_does_not_invalidate_a_later_successful_terminal(name):
    telemetry = AgentStreamTelemetry()
    _write(telemetry, _complete(name, {"input": {}}))
    _write(telemetry, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True, "content": "retry"},
    ]}})
    _write(telemetry, _terminal())
    assert telemetry.permits_file_access("read-search") is True
    assert telemetry.final_result == "{}"


@pytest.mark.parametrize("bad", [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "\ud800"}]}},
    {**_terminal(), "duration_ms": 10**309},
    {"type": []},
], ids=["unencodable-text", "numeric-overflow", "unhashable-event-type"])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""], ids=["lf", "crlf", "eof"])
@pytest.mark.parametrize("prior_terminal", [False, True])
def test_frame_processing_failure_is_sticky_and_never_escapes_finish(bad, ending, prior_terminal):
    telemetry = AgentStreamTelemetry()
    if prior_terminal:
        _write(telemetry, _terminal())
    _write(telemetry, bad, ending)
    for _ in range(2):
        assert telemetry.final_result is None
        assert telemetry.permits_file_access("none") is False
        snapshot = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")
        assert snapshot["stream_status"] == "UNAVAILABLE"
        assert snapshot["stream_reason"] == "TELEMETRY_INTERNAL_FAILURE"
