"""Lossless protocol projections for frozen S00 errors and envelopes."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import CLI_EXIT_BY_ERROR_CODE, ERROR_SPECS
from problem_locator.contracts.models import ApplicationError


def model_json(value: Any) -> Any:
    """Return the complete JSON-mode form of a frozen model or nested value."""

    if hasattr(value, "model_dump"):
        return value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            exclude_unset=False,
        )
    if isinstance(value, list):
        return [model_json(item) for item in value]
    if isinstance(value, tuple):
        return [model_json(item) for item in value]
    if isinstance(value, dict):
        return {key: model_json(item) for key, item in value.items()}
    return value


def success_envelope(data: Any) -> dict[str, Any]:
    """Build the S06 success envelope without changing frozen DTO fields."""

    return {"ok": True, "data": model_json(data), "error": None}


def error_envelope(error: ApplicationError) -> dict[str, Any]:
    """Build the S06 failure envelope from an already-classified S00 error."""

    return {"ok": False, "data": None, "error": model_json(error)}


def http_status_for(error: ApplicationError) -> int:
    """Return S00's frozen HTTP status without reclassifying the error."""

    return ERROR_SPECS[error.code].http_status


def cli_exit_for(error: ApplicationError) -> int:
    """Return S00's frozen CLI exit code without reclassifying the error."""

    return CLI_EXIT_BY_ERROR_CODE[error.code]


def validation_error(
    message: str = "Request validation failed.",
) -> ApplicationError:
    """Create the one adapter-owned validation rejection with safe details."""

    return ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=[],
        retryable=False,
    )


def validation_error_from(_error: ValidationError | ValueError | TypeError) -> ApplicationError:
    """Hide framework diagnostics while preserving the public error vocabulary."""

    return validation_error()


__all__ = [
    "cli_exit_for",
    "error_envelope",
    "http_status_for",
    "model_json",
    "success_envelope",
    "validation_error",
    "validation_error_from",
]
