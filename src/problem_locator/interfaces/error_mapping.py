"""Lossless protocol projections for frozen S00 errors and envelopes."""

from __future__ import annotations

import json
from typing import Any

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import CLI_EXIT_BY_ERROR_CODE, ERROR_SPECS
from problem_locator.contracts.models import ApplicationError, ApplicationErrorDetail


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
    *,
    details: list[ApplicationErrorDetail] | None = None,
) -> ApplicationError:
    """Create the adapter-owned validation rejection."""

    return ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=[] if details is None else details,
        retryable=False,
    )


def _field_path(location: tuple[Any, ...]) -> str:
    if not location:
        return "$"
    result = ""
    for item in location:
        if isinstance(item, int):
            result += f"[{item}]"
        else:
            result += ("." if result else "") + str(item)
    return result or "$"


def _public_actual(value: Any) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
    except (TypeError, ValueError):
        return repr(value)


def validation_diagnostics(
    error: ValidationError | RequestValidationError | ValueError | TypeError,
) -> list[dict[str, Any]]:
    if isinstance(error, ValidationError):
        return error.errors(include_url=False, include_input=True)
    if isinstance(error, RequestValidationError):
        return error.errors()
    return [
        {
            "type": type(error).__name__,
            "loc": (),
            "msg": str(error),
            "input": None,
        }
    ]


def validation_error_from(
    error: ValidationError | RequestValidationError | ValueError | TypeError,
) -> ApplicationError:
    """Project framework diagnostics into the existing public detail grammar."""

    details = [
        ApplicationErrorDetail(
            field=_field_path(tuple(item.get("loc", ()))),
            resource_type=None,
            resource_id=None,
            resource_ref=None,
            expected=(
                f"{item.get('type', type(error).__name__)}: "
                f"{item.get('msg', str(error))}"
            ),
            actual=_public_actual(item.get("input")),
            limit=None,
            observed=None,
        )
        for item in validation_diagnostics(error)
    ]
    details.sort(key=lambda item: (item.field or "", str(item.expected)))
    return validation_error(details=details)


__all__ = [
    "cli_exit_for",
    "error_envelope",
    "http_status_for",
    "model_json",
    "success_envelope",
    "validation_diagnostics",
    "validation_error",
    "validation_error_from",
]
