from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from problem_locator.diagnostics import (
    bind_diagnostics,
    configure_diagnostics,
    log_event,
)


def test_json_diagnostics_include_context_arguments_and_traceback() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    uvicorn_state = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    original_access_disabled = logging.getLogger("uvicorn.access").disabled
    try:
        configure_diagnostics("DEBUG", stream=stream)
        with bind_diagnostics(correlation_id="correlation-1", transport="mcp"):
            try:
                raise ValueError("bad input")
            except ValueError as exc:
                log_event(
                    "mcp.tool.validation_failed",
                    level=logging.WARNING,
                    arguments={"wait_seconds": "30"},
                    error=exc,
                )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for name, (handlers, propagate) in uvicorn_state.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        logging.getLogger("uvicorn.access").disabled = original_access_disabled

    event = json.loads(stream.getvalue())
    assert event["event"] == "mcp.tool.validation_failed"
    assert event["correlation_id"] == "correlation-1"
    assert event["transport"] == "mcp"
    assert event["arguments"] == {"wait_seconds": "30"}
    assert event["exception_type"] == "ValueError"
    assert "ValueError: bad input" in event["traceback"]


def test_diagnostic_serialization_falls_back_for_arbitrary_values() -> None:
    class Unsupported:
        pass

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    from problem_locator.diagnostics import JsonDiagnosticFormatter

    handler.setFormatter(JsonDiagnosticFormatter())
    logger = logging.getLogger("test.diagnostics.serialization")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "serialize",
            extra={
                "dfx_event": "diagnostics.test",
                "dfx_fields": {"value": Unsupported()},
            },
        )
    finally:
        logger.handlers.clear()

    event = json.loads(stream.getvalue())
    assert event["event"] == "diagnostics.test"
    assert "Unsupported" in event["value"]


def test_diagnostics_append_to_configured_file_and_create_parent(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "nested" / "service.jsonl"
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    uvicorn_state = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    original_access_disabled = logging.getLogger("uvicorn.access").disabled
    try:
        configure_diagnostics("INFO", log_file=log_file)
        log_event("diagnostics.file.first", sequence=1)
        configure_diagnostics("INFO", log_file=log_file)
        log_event("diagnostics.file.second", sequence=2)
    finally:
        for handler in tuple(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for name, (handlers, propagate) in uvicorn_state.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        logging.getLogger("uvicorn.access").disabled = original_access_disabled

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [(event["event"], event["sequence"]) for event in events] == [
        ("diagnostics.file.first", 1),
        ("diagnostics.file.second", 2),
    ]


def test_diagnostics_reject_stream_and_file_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be configured together"):
        configure_diagnostics(
            "INFO",
            stream=io.StringIO(),
            log_file=tmp_path / "service.jsonl",
        )
