"""Structured diagnostic events for the service and its managed workers."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TextIO


_LOGGER_NAME = "problem_locator.dfx"
_HANDLER_MARKER = "_problem_locator_dfx_handler"
_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "problem_locator_diagnostics_context",
    default={},
)


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except BaseException as exc:  # pragma: no cover - hostile diagnostic value
        return f"<unrepresentable {type(value).__name__}: {type(exc).__name__}>"


def _json_value(value: Any, *, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value, seen=seen)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value), "hex": value.hex()}
    if hasattr(value, "model_dump"):
        try:
            return _json_value(value.model_dump(mode="json"), seen=seen)
        except Exception:
            return _safe_repr(value)

    active = seen if seen is not None else set()
    identity = id(value)
    if identity in active:
        return "<recursive>"
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            try:
                return {
                    str(key): _json_value(item, seen=active)
                    for key, item in value.items()
                }
            except BaseException:
                return _safe_repr(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            try:
                return [_json_value(item, seen=active) for item in value]
            except BaseException:
                return _safe_repr(value)
        return _safe_repr(value)
    finally:
        active.remove(identity)


class JsonDiagnosticFormatter(logging.Formatter):
    """Render one complete event per line without allowing logging to fail."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload: dict[str, Any] = {
                "timestamp": datetime.fromtimestamp(
                    record.created,
                    timezone.utc,
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "level": record.levelname,
                "event": getattr(record, "dfx_event", record.name),
                "message": record.getMessage(),
                "logger": record.name,
                "process_id": record.process,
                "thread": record.threadName,
            }
            payload.update(_json_value(getattr(record, "dfx_context", {})))
            payload.update(_json_value(getattr(record, "dfx_fields", {})))
            if record.exc_info is not None:
                payload["traceback"] = self.formatException(record.exc_info)
            return json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except BaseException as exc:  # pragma: no cover - last-resort diagnostics
            return json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "level": "ERROR",
                    "event": "diagnostics.serialization_failed",
                    "message": _safe_repr(exc),
                    "original_event": str(getattr(record, "dfx_event", record.name)),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )


class _CurrentStderr:
    """Resolve stderr at emit time so test/capture stream swaps remain valid."""

    def write(self, value: str) -> int:
        return sys.stderr.write(value)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_diagnostics(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    log_file: Path | str | None = None,
) -> None:
    """Install one process-wide JSON handler for stderr or an append-only file."""

    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"unsupported diagnostic log level: {level}")
    if stream is not None and log_file is not None:
        raise ValueError("stream and log_file cannot be configured together")

    if log_file is None:
        handler: logging.Handler = logging.StreamHandler(
            stream if stream is not None else _CurrentStderr()
        )
    else:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(
            log_path,
            mode="a",
            encoding="utf-8",
        )

    root = logging.getLogger()
    for existing in tuple(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
            existing.close()

    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(JsonDiagnosticFormatter())
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def new_correlation_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def bind_diagnostics(**fields: Any) -> Iterator[None]:
    combined = dict(_CONTEXT.get())
    combined.update(fields)
    token = _CONTEXT.set(combined)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_diagnostics_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    error: BaseException | None = None,
    **fields: Any,
) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    exc_info = None
    if error is not None:
        fields.setdefault("exception_type", type(error).__name__)
        fields.setdefault("exception_message", str(error))
        exc_info = (type(error), error, error.__traceback__)
    logger.log(
        level,
        message or event,
        extra={
            "dfx_event": event,
            "dfx_context": current_diagnostics_context(),
            "dfx_fields": fields,
        },
        exc_info=exc_info,
    )


class HttpDiagnosticsMiddleware:
    """Non-buffering ASGI diagnostics, including the raw MCP route."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = new_correlation_id()
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        query_string = bytes(scope.get("query_string", b"")).decode(
            "latin-1",
            errors="replace",
        )
        headers = [
            [
                bytes(name).decode("latin-1", errors="replace"),
                bytes(value).decode("latin-1", errors="replace"),
            ]
            for name, value in scope.get("headers", [])
        ]
        status_code: int | None = None
        started = time.perf_counter()

        async def diagnostics_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
                response_headers.append(
                    (
                        b"x-problem-locator-correlation-id",
                        correlation_id.encode("ascii"),
                    )
                )
                message = {**message, "headers": response_headers}
            await send(message)

        with bind_diagnostics(
            correlation_id=correlation_id,
            transport="http",
            http_method=method,
            http_path=path,
        ):
            log_event(
                "http.request.started",
                query_string=query_string,
                headers=headers,
            )
            try:
                await self._app(scope, receive, diagnostics_send)
            except asyncio.CancelledError as exc:
                log_event(
                    "http.request.cancelled",
                    level=logging.WARNING,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    error=exc,
                )
                raise
            except Exception as exc:
                log_event(
                    "http.request.unhandled_error",
                    level=logging.ERROR,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    error=exc,
                )
                raise
            else:
                log_event(
                    "http.request.completed",
                    level=(
                        logging.ERROR
                        if status_code is not None and status_code >= 500
                        else logging.INFO
                    ),
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )


__all__ = [
    "HttpDiagnosticsMiddleware",
    "JsonDiagnosticFormatter",
    "bind_diagnostics",
    "configure_diagnostics",
    "current_diagnostics_context",
    "log_event",
    "new_correlation_id",
]
