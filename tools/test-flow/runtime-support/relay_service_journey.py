from __future__ import annotations

# Runtime support shared by the first-party CrossJob adapters.

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time


MAX_SOURCE_BYTES = 64 * 1024 * 1024
POLL_SECONDS = 0.1
PIN_SOURCE_INODE = os.name == "posix"


class SourceRotationGap(ValueError):
    """The next retained segment cannot be established without losing evidence."""


class RotatingSource:
    """Follow renamed JSONL segments in order, including rotations between polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.identity = None
        self.offset = 0
        self.closed = False
        self._pinned_source = None

    def _segment_snapshot(self):
        backups = []
        for candidate in self.path.parent.glob(self.path.name + ".*"):
            suffix = candidate.name[len(self.path.name) + 1:]
            if suffix.isdecimal() and int(suffix) > 0:
                backups.append((int(suffix), candidate))
        paths = [item[1] for item in sorted(backups, reverse=True)] + [self.path]
        result = []
        for path in paths:
            try:
                metadata = path.stat()
            except FileNotFoundError:
                continue
            result.append((path, (metadata.st_dev, metadata.st_ino)))
        if len({identity for _, identity in result}) != len(result):
            raise SourceRotationGap("rotation changed while reading segment identities")
        return result

    def _segments(self):
        first = self._segment_snapshot()
        if self._segment_snapshot() != first:
            raise SourceRotationGap("rotation changed between complete segment snapshots")
        return first

    def _select_segment(self, segments, index: int) -> None:
        path, identity = segments[index]
        # POSIX permits renaming open files. Pin the current inode so unlinking
        # it cannot let the filesystem recycle its number for a later segment.
        # Windows must close between reads to permit the writer's renames.
        replacement = path.open("rb", buffering=0) if PIN_SOURCE_INODE else None
        try:
            if replacement is not None:
                metadata = os.fstat(replacement.fileno())
                if (metadata.st_dev, metadata.st_ino) != identity:
                    raise SourceRotationGap("rotation changed before pinning the next segment")
            if self._segments() != segments:
                raise SourceRotationGap("rotation changed while selecting the next segment")
        except BaseException:
            if replacement is not None:
                replacement.close()
            raise
        previous = self._pinned_source
        self._pinned_source = replacement
        self.identity = identity
        self.offset = 0
        if previous is not None:
            previous.close()

    def _read_segment(self, path: Path, size: int) -> bytes:
        # A long-lived descriptor blocks rename on Windows. Keep the inode and
        # byte offset instead, and verify every short-lived open before reading.
        with path.open("rb", buffering=0) as source:
            metadata = os.fstat(source.fileno())
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                raise SourceRotationGap("rotation changed before opening the next segment")
            if metadata.st_size < self.offset:
                raise SourceRotationGap("source segment was truncated before its read offset")
            source.seek(self.offset)
            chunk = source.read(size)
            if os.fstat(source.fileno()).st_size < self.offset + len(chunk):
                raise SourceRotationGap("source segment was truncated while reading")
        self.offset += len(chunk)
        return chunk

    def read(self, size: int) -> bytes:
        if self.closed:
            raise ValueError("read from closed rotating source")
        while True:
            segments = self._segments()
            if self.identity is None:
                if not segments:
                    return b""
                self._select_segment(segments, 0)
            identities = [identity for _, identity in segments]
            if self.identity not in identities:
                raise SourceRotationGap("unread rotated log segments are no longer retained")
            path, _ = segments[identities.index(self.identity)]
            chunk = self._read_segment(path, size)
            if chunk:
                return chunk
            # Rotation can happen during a read. Establish the next segment
            # from another stable mapping, never from a stale filename order.
            segments = self._segments()
            identities = [identity for _, identity in segments]
            if self.identity not in identities:
                raise SourceRotationGap("unread rotated log segments are no longer retained")
            index = identities.index(self.identity)
            if index + 1 == len(segments):
                return b""
            self._select_segment(segments, index + 1)

    def close(self) -> None:
        self.closed = True
        if self._pinned_source is not None:
            self._pinned_source.close()
            self._pinned_source = None


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--producer-id", required=True)
    parser.add_argument(
        "--mode",
        choices=("journey", "diagnostics"),
        default="journey",
    )
    parser.add_argument("--allow-empty", action="store_true")
    return parser.parse_args()


def _write_json_new(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validation_error_facts(value: object) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    facts: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        location = entry.get("field", entry.get("loc", entry.get("location")))
        if isinstance(location, list):
            field = ".".join(str(part) for part in location)
        elif isinstance(location, str):
            field = location
        else:
            continue
        error_type = entry.get("type")
        if field and isinstance(error_type, str) and error_type:
            facts.add((field, error_type))
    return [
        {"field": field, "type": error_type}
        for field, error_type in sorted(facts)
    ]


def _receipt(arguments: argparse.Namespace, *, status: str, code: str | None, count: int) -> None:
    _write_json_new(
        arguments.receipt,
        {
            "schema_version": 2,
            "status": status,
            "code": code,
            "source_event_count": count,
            "producer_id": arguments.producer_id,
            "clock_domain": arguments.producer_id,
            "allow_empty": arguments.allow_empty,
            "raw_sha256": _sha256(arguments.raw),
            "events_sha256": _sha256(arguments.events),
        },
    )


def main() -> int:
    arguments = _arguments()
    arguments.events.parent.mkdir(parents=True, exist_ok=True)
    arguments.raw.parent.mkdir(parents=True, exist_ok=True)
    events_fd = os.open(
        arguments.events,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    raw_fd = os.open(
        arguments.raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    started = time.monotonic()
    source = RotatingSource(arguments.source)
    source_bytes = 0
    tail = b""
    source_sequence = 0
    output_sequence = 0
    try:
        while True:
            try:
                chunk = source.read(65536)
            except (SourceRotationGap, OSError):
                _receipt(arguments, status="FAIL", code="SOURCE_ROTATION_GAP", count=source_sequence)
                return 1
            if chunk:
                source_bytes += len(chunk)
                if source_bytes > MAX_SOURCE_BYTES:
                    _receipt(
                        arguments,
                        status="FAIL",
                        code="SOURCE_LIMIT_EXCEEDED",
                        count=source_sequence,
                    )
                    return 1
                tail += chunk
                while b"\n" in tail:
                    line, tail = tail.split(b"\n", 1)
                    if not line or b"\r" in line:
                        _receipt(
                            arguments,
                            status="FAIL",
                            code="SOURCE_FRAMING",
                            count=source_sequence,
                        )
                        return 1
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        _receipt(
                            arguments,
                            status="FAIL",
                            code="SOURCE_JSON",
                            count=source_sequence,
                        )
                        return 1
                    data = event.get("data")
                    truncation = event.get("log_truncation")
                    if not isinstance(truncation, dict) and isinstance(data, dict):
                        truncation = data.get("log_truncation")
                    if isinstance(truncation, dict) and truncation.get("truncated") is True:
                        _receipt(arguments, status="FAIL", code="SOURCE_EVENT_TRUNCATED", count=source_sequence)
                        return 1
                    expected = source_sequence + 1
                    if arguments.mode == "journey" and (
                        event.get("schema_version") != 1
                        or event.get("sequence") != expected
                    ):
                        _receipt(
                            arguments,
                            status="FAIL",
                            code="SOURCE_SEQUENCE",
                            count=source_sequence,
                        )
                        return 1
                    source_sequence = expected
                    output_sequence += 1
                    os.write(raw_fd, line + b"\n")
                    source_data = event.get("data")
                    if not isinstance(source_data, dict):
                        source_data = {}
                    usage_counts = source_data.get("usage_counts")
                    usage_total = (
                        sum(
                            value
                            for value in usage_counts.values()
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        )
                        if isinstance(usage_counts, dict)
                        else None
                    )
                    telemetry_tools = source_data.get("tool_observations")
                    tools = event.get("tools")
                    arguments_value = event.get("arguments")
                    canonical_arguments = (
                        json.dumps(
                            arguments_value,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("ascii")
                        if isinstance(arguments_value, dict)
                        else None
                    )
                    envelope = {
                        "schema_version": 2,
                        "seq": output_sequence,
                        "timestamp_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "source_timestamp_utc": event.get("timestamp"),
                        "run_id": arguments.run_id,
                        "producer_id": arguments.producer_id,
                        "producer_type": "service",
                        "clock_domain": arguments.producer_id,
                        "event_type": event.get("event"),
                        "stage_id": None,
                        "scenario": "CrossJob",
                        "monotonic_elapsed_ms": round(
                            (time.monotonic() - started) * 1000,
                            3,
                        ),
                        "correlation_id": event.get("correlation_id"),
                        "request_id": event.get("request_id"),
                        "case_id": event.get("case_id"),
                        "job_id": event.get("job_id"),
                        "data": {
                            "source_sequence": source_sequence,
                            "source_bytes": len(line) + 1,
                            "level": event.get("level"),
                            "job_type": event.get("job_type"),
                            "duration_ms": event.get("duration_ms"),
                            "tool": event.get("tool"),
                            "ok": event.get("ok"),
                            "error_code": event.get("error_code"),
                            "tool_count": (
                                len(tools)
                                if isinstance(tools, list)
                                else None
                            ),
                            "tool_names": (
                                [tool.get("name") for tool in tools]
                                if isinstance(tools, list)
                                else None
                            ),
                            "tool_schema_sha256": (
                                [tool.get("input_schema_sha256") for tool in tools]
                                if isinstance(tools, list)
                                else None
                            ),
                            "argument_names": (
                                sorted(arguments_value)
                                if isinstance(arguments_value, dict)
                                else None
                            ),
                            "arguments_sha256": (
                                hashlib.sha256(canonical_arguments).hexdigest()
                                if canonical_arguments is not None
                                else None
                            ),
                            "transport": event.get("transport"),
                            "server_version": event.get("server_version"),
                            "validation_errors": _validation_error_facts(
                                event.get("validation_errors")
                            ),
                            "semantic_stage": source_data.get("stage"),
                            "diagnosis_mode": source_data.get("diagnosis_mode"),
                            "backend_status": source_data.get("backend_status"),
                            "stream_status": source_data.get("stream_status"),
                            "stream_reason": source_data.get("stream_reason"),
                            "content_included": source_data.get("content_included"),
                            "cli_duration_ms": source_data.get("cli_duration_ms"),
                            "model_api_duration_ms": source_data.get(
                                "model_api_duration_ms"
                            ),
                            "prompt_bytes": source_data.get("prompt_bytes"),
                            "prompt_write_ms": source_data.get("prompt_write_ms"),
                            "turn_count": source_data.get("turn_count"),
                            "usage_unit": source_data.get("usage_unit"),
                            "usage_total": usage_total,
                            "telemetry_tool_count": (
                                len(telemetry_tools)
                                if isinstance(telemetry_tools, list)
                                else None
                            ),
                            "logparse_operation": source_data.get("operation"),
                            "logparse_phase": source_data.get("phase"),
                            "logparse_ordinal": source_data.get("ordinal"),
                        },
                    }
                    encoded = (
                        json.dumps(
                            envelope,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("ascii")
                    os.write(events_fd, encoded)
                    os.fsync(events_fd)
                    os.fsync(raw_fd)
                continue
            if arguments.stop.exists():
                if tail:
                    _receipt(
                        arguments,
                        status="FAIL",
                        code="SOURCE_PARTIAL_TAIL",
                        count=source_sequence,
                    )
                    return 1
                if source_sequence == 0:
                    if arguments.allow_empty:
                        _receipt(arguments, status="PASS", code=None, count=0)
                        return 0
                    _receipt(
                        arguments,
                        status="FAIL",
                        code="SOURCE_EMPTY",
                        count=0,
                    )
                    return 1
                _receipt(arguments, status="PASS", code=None, count=source_sequence)
                return 0
            time.sleep(POLL_SECONDS)
    finally:
        if source is not None:
            source.close()
        os.close(events_fd)
        os.close(raw_fd)


if __name__ == "__main__":
    raise SystemExit(main())
