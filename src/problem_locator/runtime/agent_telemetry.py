"""Bounded, content-free observations of Claude ``stream-json`` output."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from problem_locator.contracts.ports import AppendOnlyByteSink


_DEFAULT_LINE_LIMIT_BYTES = 1024 * 1024
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_STREAM_TYPES = frozenset({"system", "assistant", "user", "result"})


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        return None
    return result


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _utf8_size(value: object) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def _message_blocks(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, dict):
        return ()
    message = value.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(item for item in content if isinstance(item, dict))


def _union_duration(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    total = 0.0
    start, end = sorted(intervals)[0]
    for candidate_start, candidate_end in sorted(intervals)[1:]:
        if candidate_start <= end:
            end = max(end, candidate_end)
            continue
        total += max(0.0, end - start)
        start, end = candidate_start, candidate_end
    return total + max(0.0, end - start)


class AgentStreamTelemetry:
    """Observe redacted stdout without retaining prompt or model content."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        line_limit_bytes: int = _DEFAULT_LINE_LIMIT_BYTES,
    ) -> None:
        if line_limit_bytes <= 0:
            raise ValueError("line_limit_bytes must be positive")
        self._monotonic = monotonic
        self._origin = monotonic()
        self._line_limit = line_limit_bytes
        self._buffer = bytearray()
        self._discarding_line = False
        self._lock = threading.Lock()
        self._recognized = 0
        self._parsed_lines = 0
        self._output_bytes = 0
        self._malformed = False
        self._line_limited = False
        self._internal_failure = False
        self._terminal_result = False
        self._system_observed_ms: float | None = None
        self._result_observed_ms: float | None = None
        self._cli_duration_ms: float | None = None
        self._model_api_duration_ms: float | None = None
        self._turn_count: int | None = None
        self._usage_counts: dict[str, int] = {}
        self._blocks = {
            "thinking": {"count": 0, "utf8_bytes": 0, "first": None, "last": None},
            "text": {"count": 0, "utf8_bytes": 0, "first": None, "last": None},
        }
        self._pending_tools: dict[str, tuple[str, float]] = {}
        self._tool_counts: dict[str, int] = {}
        self._tool_intervals: dict[str, list[tuple[float, float]]] = {}
        self._tool_max_ms: dict[str, float] = {}
        self._tool_completed: dict[str, int] = {}
        self._prompt_bytes = 0
        self._prompt_started: float | None = None
        self._prompt_finished: float | None = None
        self._prompt_status = "NOT_STARTED"

    def _elapsed_ms(self) -> float:
        return max(0.0, (self._monotonic() - self._origin) * 1000)

    def prompt_started(self, size: int) -> None:
        with self._lock:
            self._prompt_bytes = max(0, int(size))
            self._prompt_started = self._elapsed_ms()
            self._prompt_status = "IN_PROGRESS"

    def prompt_finished(self, *, completed: bool) -> None:
        with self._lock:
            self._prompt_finished = self._elapsed_ms()
            self._prompt_status = "COMPLETE" if completed else "INCOMPLETE"

    def disable(self) -> None:
        with self._lock:
            self._internal_failure = True
            self._buffer.clear()
            self._discarding_line = True

    def write(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("write requires non-empty bytes")
        with self._lock:
            if self._internal_failure:
                return
            self._output_bytes += len(chunk)
            try:
                self._consume(chunk)
            except BaseException:
                self._internal_failure = True
                self._buffer.clear()
                self._discarding_line = True

    def _consume(self, chunk: bytes) -> None:
        for value in chunk:
            if self._discarding_line:
                if value == 0x0A:
                    self._discarding_line = False
                continue
            if value == 0x0A:
                line = bytes(self._buffer)
                self._buffer.clear()
                self._process_line(line)
                continue
            self._buffer.append(value)
            if len(self._buffer) > self._line_limit:
                self._line_limited = True
                self._buffer.clear()
                self._discarding_line = True

    def finish(self) -> None:
        with self._lock:
            if self._internal_failure or self._discarding_line:
                return
            if self._buffer:
                line = bytes(self._buffer)
                self._buffer.clear()
                self._process_line(line)

    def _process_line(self, raw: bytes) -> None:
        if not raw or raw.endswith(b"\r"):
            self._malformed = True
            return
        self._parsed_lines += 1
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._malformed = True
            return
        if not isinstance(payload, dict) or payload.get("type") not in _STREAM_TYPES:
            return
        self._recognized += 1
        observed_ms = self._elapsed_ms()
        event_type = payload["type"]
        if event_type == "system":
            self._system_observed_ms = (
                observed_ms
                if self._system_observed_ms is None
                else self._system_observed_ms
            )
        elif event_type == "assistant":
            self._observe_assistant(payload, observed_ms)
        elif event_type == "user":
            self._observe_user(payload, observed_ms)
        elif event_type == "result":
            self._observe_result(payload, observed_ms)

    def _observe_assistant(self, payload: Mapping[str, Any], observed_ms: float) -> None:
        for block in _message_blocks(payload):
            block_type = block.get("type")
            if block_type in self._blocks:
                state = self._blocks[block_type]
                state["count"] = int(state["count"]) + 1
                state["utf8_bytes"] = int(state["utf8_bytes"]) + _utf8_size(
                    block.get(block_type)
                )
                state["first"] = observed_ms if state["first"] is None else state["first"]
                state["last"] = observed_ms
                continue
            if block_type != "tool_use":
                continue
            raw_name = block.get("name")
            name = raw_name if isinstance(raw_name, str) and _SAFE_TOOL_NAME.fullmatch(raw_name) else "OTHER"
            self._tool_counts[name] = self._tool_counts.get(name, 0) + 1
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                self._pending_tools[tool_id] = (name, observed_ms)

    def _observe_user(self, payload: Mapping[str, Any], observed_ms: float) -> None:
        for block in _message_blocks(payload):
            if block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            pending = self._pending_tools.pop(tool_id, None) if isinstance(tool_id, str) else None
            if pending is None:
                continue
            name, started_ms = pending
            interval = (started_ms, max(started_ms, observed_ms))
            self._tool_intervals.setdefault(name, []).append(interval)
            duration = interval[1] - interval[0]
            self._tool_completed[name] = self._tool_completed.get(name, 0) + 1
            self._tool_max_ms[name] = max(self._tool_max_ms.get(name, 0.0), duration)

    def _observe_result(self, payload: Mapping[str, Any], observed_ms: float) -> None:
        self._terminal_result = True
        self._result_observed_ms = observed_ms
        self._cli_duration_ms = _nonnegative_number(payload.get("duration_ms"))
        self._model_api_duration_ms = _nonnegative_number(payload.get("duration_api_ms"))
        self._turn_count = _nonnegative_integer(payload.get("num_turns"))
        usage = payload.get("usage")
        if isinstance(usage, dict):
            fields = {
                "input": "input_tokens",
                "output": "output_tokens",
                "cache_creation": "cache_creation_input_tokens",
                "cache_read": "cache_read_input_tokens",
            }
            for safe_name, source_name in fields.items():
                count = _nonnegative_integer(usage.get(source_name))
                if count is not None:
                    self._usage_counts[safe_name] = count

    def snapshot(self, *, diagnosis_mode: str, backend_status: str) -> dict[str, Any]:
        self.finish()
        with self._lock:
            if self._internal_failure:
                status, reason = "UNAVAILABLE", "TELEMETRY_INTERNAL_FAILURE"
            elif (
                self._recognized == 0
                and self._output_bytes == 0
                and self._prompt_status == "NOT_STARTED"
                and backend_status == "FAILED"
            ):
                status, reason = "UNAVAILABLE", "BACKEND_NOT_STARTED"
            elif self._recognized == 0:
                status = "UNAVAILABLE"
                reason = (
                    "STREAM_JSON_LINE_LIMIT"
                    if self._line_limited
                    else "UNSUPPORTED_STREAM_JSON"
                )
            elif self._line_limited:
                status, reason = "PARTIAL", "STREAM_JSON_LINE_LIMIT"
            elif self._malformed:
                status, reason = "PARTIAL", "STREAM_JSON_MALFORMED"
            elif not self._terminal_result:
                status, reason = "PARTIAL", "TERMINAL_RESULT_MISSING"
            else:
                status, reason = "COMPLETE", None

            prompt_write_ms = None
            if self._prompt_started is not None and self._prompt_finished is not None:
                prompt_write_ms = max(0.0, self._prompt_finished - self._prompt_started)

            block_observations: dict[str, dict[str, Any]] = {}
            for name, state in self._blocks.items():
                first = state["first"]
                last = state["last"]
                block_observations[name] = {
                    "block_count": state["count"],
                    "utf8_bytes": state["utf8_bytes"],
                    "first_observed_ms": first,
                    "last_observed_ms": last,
                    "observed_window_ms": (
                        None if first is None or last is None else max(0.0, float(last) - float(first))
                    ),
                }

            tool_observations: list[dict[str, Any]] = []
            all_intervals: list[tuple[float, float]] = []
            for name in sorted(self._tool_counts):
                intervals = self._tool_intervals.get(name, [])
                all_intervals.extend(intervals)
                tool_observations.append(
                    {
                        "name": name,
                        "call_count": self._tool_counts[name],
                        "completed_count": self._tool_completed.get(name, 0),
                        "incomplete_count": max(
                            0,
                            self._tool_counts[name] - self._tool_completed.get(name, 0),
                        ),
                        "observed_duration_ms": _union_duration(intervals),
                        "max_call_ms": self._tool_max_ms.get(name, 0.0),
                    }
                )

            return {
                "diagnosis_mode": diagnosis_mode,
                "backend_status": backend_status,
                "stream_format": "claude-stream-json",
                "stream_status": status,
                "stream_reason": reason,
                "content_included": False,
                "observed_output_bytes": self._output_bytes,
                "parsed_line_count": self._parsed_lines,
                "recognized_event_count": self._recognized,
                "prompt_bytes": self._prompt_bytes,
                "prompt_write_status": self._prompt_status,
                "prompt_write_ms": prompt_write_ms,
                "system_observed_ms": self._system_observed_ms,
                "result_observed_ms": self._result_observed_ms,
                "cli_duration_ms": self._cli_duration_ms,
                "model_api_duration_ms": self._model_api_duration_ms,
                "turn_count": self._turn_count,
                "usage_unit": "tokens",
                "usage_counts": dict(sorted(self._usage_counts.items())),
                "block_observations": block_observations,
                "tool_observed_union_ms": _union_duration(all_intervals),
                "tool_observations": tool_observations,
            }


class TelemetryTeeSink:
    """Write redacted bytes to the execution log and best-effort telemetry."""

    def __init__(
        self,
        sink: AppendOnlyByteSink,
        telemetry: AgentStreamTelemetry,
    ) -> None:
        if not isinstance(sink, AppendOnlyByteSink):
            raise TypeError("sink must implement AppendOnlyByteSink")
        self._sink = sink
        self._telemetry = telemetry

    def write(self, chunk: bytes) -> None:
        self._sink.write(chunk)
        try:
            self._telemetry.write(chunk)
        except BaseException:
            self._telemetry.disable()

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        # Ownership remains with AgentBackend's _OwnedSink.
        return


__all__ = ["AgentStreamTelemetry", "TelemetryTeeSink"]
