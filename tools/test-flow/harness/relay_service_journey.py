from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time


MAX_SOURCE_BYTES = 64 * 1024 * 1024
POLL_SECONDS = 0.1


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


def _receipt(arguments: argparse.Namespace, *, status: str, code: str | None, count: int) -> None:
    _write_json_new(
        arguments.receipt,
        {
            "schema_version": 1,
            "status": status,
            "code": code,
            "source_event_count": count,
            "producer_id": arguments.producer_id,
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
    source = None
    source_bytes = 0
    tail = b""
    source_sequence = 0
    output_sequence = 0
    try:
        while True:
            if source is None and arguments.source.is_file():
                source = arguments.source.open("rb", buffering=0)
            chunk = b"" if source is None else source.read(65536)
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
                    envelope = {
                        "schema_version": 1,
                        "seq": output_sequence,
                        "timestamp_utc": event.get("timestamp"),
                        "run_id": arguments.run_id,
                        "producer_id": arguments.producer_id,
                        "producer_type": "service",
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
                            "level": event.get("level"),
                            "job_type": event.get("job_type"),
                            "duration_ms": event.get("duration_ms"),
                            "tool": event.get("tool"),
                            "ok": event.get("ok"),
                            "error_code": event.get("error_code"),
                            "tool_count": (
                                len(event.get("tools", []))
                                if isinstance(event.get("tools"), list)
                                else None
                            ),
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
