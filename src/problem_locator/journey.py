"""Process-wide semantic Journey events for one service instance."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field

from problem_locator.contracts.enums import ExecutionStage, JobType
from problem_locator.contracts.models import (
    ExecutionFailure,
    NonEmptyText,
    OpaqueId,
    UtcTimestamp,
)
from problem_locator.diagnostics import current_diagnostics_context, log_event


JOURNEY_SCHEMA_VERSION = 1
JOURNEY_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class JourneyEvent(BaseModel):
    """Strict, versioned envelope persisted as one JSONL record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    sequence: Annotated[int, Field(gt=0, strict=True)]
    timestamp: UtcTimestamp
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    event: NonEmptyText
    correlation_id: NonEmptyText | None
    request_id: NonEmptyText | None
    case_id: OpaqueId | None
    job_id: OpaqueId | None
    job_type: Literal["ROUTE", "DIAGNOSE", "REVIEW"] | None
    outcome_id: OpaqueId | None
    duration_ms: Annotated[
        float,
        Field(ge=0, allow_inf_nan=False, strict=True),
    ] | None
    data: dict[str, Any]


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except BaseException as exc:  # pragma: no cover - hostile diagnostic value
        return f"<unrepresentable {type(value).__name__}: {type(exc).__name__}>"


def journey_json_value(value: Any, *, seen: set[int] | None = None) -> Any:
    """Project contract and Python values to a JSON-compatible representation."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return journey_json_value(value.value, seen=seen)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256_not_computed": True}
    if hasattr(value, "model_dump"):
        return journey_json_value(value.model_dump(mode="json"), seen=seen)

    active = seen if seen is not None else set()
    identity = id(value)
    if identity in active:
        return "<recursive>"
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            return {
                str(key): journey_json_value(item, seen=active)
                for key, item in value.items()
            }
        if isinstance(value, (set, frozenset)):
            projected = [journey_json_value(item, seen=active) for item in value]
            return sorted(
                projected,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=_safe_repr,
                ),
            )
        if isinstance(value, (list, tuple)):
            return [journey_json_value(item, seen=active) for item in value]
        return _safe_repr(value)
    except BaseException:
        return _safe_repr(value)
    finally:
        active.remove(identity)


class _JourneyWriter:
    def __init__(self, stream: TextIO, *, close_stream: bool) -> None:
        self.stream = stream
        self.close_stream = close_stream
        self.sequence = 0

    def close(self) -> None:
        if self.close_stream:
            self.stream.close()

    def emit(
        self,
        *,
        timestamp: str,
        level: str,
        event: str,
        correlation_id: str | None,
        request_id: str | None,
        case_id: str | None,
        job_id: str | None,
        job_type: JobType | str | None,
        outcome_id: str | None,
        duration_ms: float | None,
        data: Mapping[str, Any],
    ) -> JourneyEvent:
        sequence = self.sequence + 1
        payload = JourneyEvent.model_validate(
            {
                "schema_version": JOURNEY_SCHEMA_VERSION,
                "sequence": sequence,
                "timestamp": timestamp,
                "level": level,
                "event": event,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "case_id": case_id,
                "job_id": job_id,
                "job_type": (
                    job_type.value if isinstance(job_type, JobType) else job_type
                ),
                "outcome_id": outcome_id,
                "duration_ms": duration_ms,
                "data": journey_json_value(data),
            },
            strict=True,
        )
        line = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.stream.write(line + "\n")
        self.stream.flush()
        self.sequence = sequence
        return payload


_LOCK = threading.RLock()
_WRITER: _JourneyWriter | None = None
_WRITE_FAILURE_REPORTED = False


def configure_journey(
    *,
    stream: TextIO | None = None,
    log_file: Path | str | None = None,
) -> None:
    """Configure or disable the process-wide Journey writer."""

    if stream is not None and log_file is not None:
        raise ValueError("stream and log_file cannot be configured together")

    writer: _JourneyWriter | None = None
    if stream is not None:
        writer = _JourneyWriter(stream, close_stream=False)
    elif log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8", newline="\n")
        writer = _JourneyWriter(handle, close_stream=True)

    global _WRITER, _WRITE_FAILURE_REPORTED
    with _LOCK:
        previous = _WRITER
        _WRITER = writer
        _WRITE_FAILURE_REPORTED = False
        if previous is not None:
            previous.close()


def journey_enabled() -> bool:
    with _LOCK:
        return _WRITER is not None


def _timestamp_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _context_text(context: Mapping[str, Any], key: str) -> str | None:
    value = context.get(key)
    return value if isinstance(value, str) and value else None


def record_journey_event(
    event: str,
    *,
    level: int = logging.INFO,
    timestamp: str | None = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
    case_id: str | None = None,
    job_id: str | None = None,
    job_type: JobType | str | None = None,
    outcome_id: str | None = None,
    duration_ms: float | int | None = None,
    data: Mapping[str, Any] | None = None,
) -> JourneyEvent | None:
    """Append a semantic event without allowing observability to break business work."""

    context = current_diagnostics_context()
    normalized_level = logging.getLevelName(level)
    if normalized_level not in JOURNEY_LEVELS:
        normalized_level = "INFO"
    normalized_duration = (
        None if duration_ms is None else round(float(duration_ms), 3)
    )

    failure: Exception | None = None
    report_failure = False
    result: JourneyEvent | None = None
    global _WRITER, _WRITE_FAILURE_REPORTED
    with _LOCK:
        writer = _WRITER
        if writer is None:
            return None
        try:
            result = writer.emit(
                timestamp=timestamp or _timestamp_now(),
                level=normalized_level,
                event=event,
                correlation_id=(
                    correlation_id
                    if correlation_id is not None
                    else _context_text(context, "correlation_id")
                ),
                request_id=(
                    request_id
                    if request_id is not None
                    else _context_text(context, "request_id")
                ),
                case_id=(
                    case_id
                    if case_id is not None
                    else _context_text(context, "case_id")
                ),
                job_id=(
                    job_id
                    if job_id is not None
                    else _context_text(context, "job_id")
                ),
                job_type=(
                    job_type
                    if job_type is not None
                    else _context_text(context, "job_type")
                ),
                outcome_id=outcome_id,
                duration_ms=normalized_duration,
                data={} if data is None else data,
            )
        except Exception as exc:  # pragma: no branch - the failure path is tiny
            failure = exc
            _WRITER = None
            try:
                writer.close()
            except Exception:
                pass
            if not _WRITE_FAILURE_REPORTED:
                _WRITE_FAILURE_REPORTED = True
                report_failure = True
    if report_failure:
        assert failure is not None
        log_event(
            "journey.write_failed",
            level=logging.ERROR,
            journey_event=event,
            error=failure,
        )
    return result


def record_stage_started(
    stage: ExecutionStage,
    *,
    data: Mapping[str, Any] | None = None,
) -> float:
    started = time.perf_counter()
    record_journey_event(
        "job.stage.started",
        data={"stage": stage.value, **({} if data is None else dict(data))},
    )
    return started


def record_stage_completed(
    stage: ExecutionStage,
    started: float,
    *,
    data: Mapping[str, Any] | None = None,
) -> None:
    record_journey_event(
        "job.stage.completed",
        duration_ms=(time.perf_counter() - started) * 1000,
        data={"stage": stage.value, **({} if data is None else dict(data))},
    )


def record_stage_failed(failure: ExecutionFailure) -> None:
    record_journey_event(
        "job.stage.failed",
        level=logging.ERROR,
        data={
            "stage": failure.stage.value,
            "code": failure.code.value,
            "message": failure.message,
            "retryable": failure.retryable,
            "details": failure.details,
        },
    )


__all__ = [
    "JOURNEY_SCHEMA_VERSION",
    "JourneyEvent",
    "configure_journey",
    "journey_enabled",
    "journey_json_value",
    "record_journey_event",
    "record_stage_completed",
    "record_stage_failed",
    "record_stage_started",
]
