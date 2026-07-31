"""Frozen public error vocabulary and retryability policy."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Iterable
from typing import Mapping

from pydantic import TypeAdapter

from .enums import ErrorCode, ExecutionStage
from .models import ApplicationError, ApplicationErrorDetail, ExecutionFailure, OpaqueId


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    application_retryable: bool
    cli_exit_code: int


_HTTP_STATUS = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.CASE_NOT_FOUND: 404,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.JOB_CASE_MISMATCH: 409,
    ErrorCode.ATTACHMENT_NOT_FOUND: 404,
    ErrorCode.ARTIFACT_NOT_FOUND: 404,
    ErrorCode.RESOURCE_NOT_FOUND: 500,
    ErrorCode.INVALID_CASE_STATE: 409,
    ErrorCode.ACTIVE_JOB_EXISTS: 409,
    ErrorCode.NEW_CASE_REQUIRED: 409,
    ErrorCode.REVISION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.RESOURCE_CASE_MISMATCH: 409,
    ErrorCode.ATTACHMENT_NOT_READY: 409,
    ErrorCode.UPLOAD_INCOMPLETE: 409,
    ErrorCode.RESOURCE_HASH_MISMATCH: 422,
    ErrorCode.RESOURCE_SIZE_MISMATCH: 422,
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: 413,
    ErrorCode.PATH_VIOLATION: 400,
    ErrorCode.CONTEXT_LIMIT: 422,
    ErrorCode.ASSET_VERSION_UNAVAILABLE: 422,
    ErrorCode.OUTCOME_MISSING: 422,
    ErrorCode.OUTCOME_INVALID: 422,
    ErrorCode.BACKEND_START_FAILED: 500,
    ErrorCode.BACKEND_CANCELLED: 409,
    ErrorCode.BACKEND_TIMEOUT: 504,
    ErrorCode.BACKEND_OUTPUT_LIMIT: 422,
    ErrorCode.BACKEND_EXIT_FAILED: 502,
    ErrorCode.WORKSPACE_LIMIT: 422,
    ErrorCode.WORKSPACE_PREPARE_FAILED: 500,
    ErrorCode.RESOURCE_STAGE_FAILED: 500,
    ErrorCode.EXECUTION_RECORD_FAILED: 500,
    ErrorCode.LOGPARSE_FAILED: 422,
    ErrorCode.LOGPARSE_OUTPUT_INVALID: 422,
    ErrorCode.DISPATCH_REJECTED: 503,
    ErrorCode.CLAIM_REJECTED: 409,
    ErrorCode.INSTANCE_LOCKED: 503,
    ErrorCode.STATE_CORRUPT: 503,
    ErrorCode.STATE_SCHEMA_UNSUPPORTED: 503,
    ErrorCode.STATE_WRITE_FAILED: 500,
    ErrorCode.RESOURCE_PUBLISH_FAILED: 500,
    ErrorCode.CONFIG_INVALID: 500,
    ErrorCode.NO_CAPABILITY: 422,
}

APPLICATION_ERROR_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.REVISION_CONFLICT,
        ErrorCode.ATTACHMENT_NOT_READY,
        ErrorCode.UPLOAD_INCOMPLETE,
        ErrorCode.DISPATCH_REJECTED,
        ErrorCode.INSTANCE_LOCKED,
        ErrorCode.STATE_WRITE_FAILED,
        ErrorCode.RESOURCE_PUBLISH_FAILED,
    }
)

EXECUTION_FAILURE_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.BACKEND_START_FAILED,
        ErrorCode.BACKEND_CANCELLED,
        ErrorCode.BACKEND_TIMEOUT,
        ErrorCode.BACKEND_EXIT_FAILED,
        ErrorCode.WORKSPACE_PREPARE_FAILED,
        ErrorCode.RESOURCE_STAGE_FAILED,
        ErrorCode.EXECUTION_RECORD_FAILED,
        ErrorCode.LOGPARSE_FAILED,
    }
)

# Exit 3 is deliberately narrow: it identifies a process that cannot safely
# open the configured installation/state.  Runtime and persistence-operation
# failures are exit 4; deterministic request/domain rejections are exit 2.
CLI_CONFIG_OR_STATE_CORRUPT_CODES = frozenset(
    {
        ErrorCode.RESOURCE_NOT_FOUND,
        ErrorCode.INSTANCE_LOCKED,
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        ErrorCode.CONFIG_INVALID,
    }
)
CLI_RUNTIME_FAILURE_CODES = frozenset(
    {
        ErrorCode.CONTEXT_LIMIT,
        ErrorCode.ASSET_VERSION_UNAVAILABLE,
        ErrorCode.OUTCOME_MISSING,
        ErrorCode.OUTCOME_INVALID,
        ErrorCode.BACKEND_START_FAILED,
        ErrorCode.BACKEND_CANCELLED,
        ErrorCode.BACKEND_TIMEOUT,
        ErrorCode.BACKEND_OUTPUT_LIMIT,
        ErrorCode.BACKEND_EXIT_FAILED,
        ErrorCode.WORKSPACE_LIMIT,
        ErrorCode.WORKSPACE_PREPARE_FAILED,
        ErrorCode.RESOURCE_STAGE_FAILED,
        ErrorCode.EXECUTION_RECORD_FAILED,
        ErrorCode.LOGPARSE_FAILED,
        ErrorCode.LOGPARSE_OUTPUT_INVALID,
        ErrorCode.DISPATCH_REJECTED,
        ErrorCode.STATE_WRITE_FAILED,
        ErrorCode.RESOURCE_PUBLISH_FAILED,
    }
)
CLI_REQUEST_OR_STATE_CONFLICT_CODES = frozenset(ErrorCode) - (
    CLI_CONFIG_OR_STATE_CORRUPT_CODES | CLI_RUNTIME_FAILURE_CODES
)


def _cli_exit_code(code: ErrorCode) -> int:
    if code in CLI_CONFIG_OR_STATE_CORRUPT_CODES:
        return 3
    if code in CLI_RUNTIME_FAILURE_CODES:
        return 4
    return 2

ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = MappingProxyType(
    {
        code: ErrorSpec(
            http_status=_HTTP_STATUS[code],
            application_retryable=code in APPLICATION_ERROR_RETRYABLE_CODES,
            cli_exit_code=_cli_exit_code(code),
        )
        for code in ErrorCode
    }
)
ERROR_CODES = tuple(code.value for code in ErrorCode)

CLI_EXIT_SUCCESS = 0
CLI_EXIT_REQUEST_OR_STATE_CONFLICT = 2
CLI_EXIT_CONFIG_OR_STATE_CORRUPT = 3
CLI_EXIT_RUNTIME_FAILURE = 4
CLI_EXIT_BY_ERROR_CODE: Mapping[ErrorCode, int] = MappingProxyType(
    {code: ERROR_SPECS[code].cli_exit_code for code in ErrorCode}
)


@dataclass(frozen=True, slots=True)
class DeterministicFailureSpec:
    stage: ExecutionStage
    message: str


DETERMINISTIC_OUTCOME_FAILURE_SPECS: Mapping[ErrorCode, DeterministicFailureSpec] = (
    MappingProxyType(
        {
            ErrorCode.OUTCOME_MISSING: DeterministicFailureSpec(
                ExecutionStage.OUTCOME_VALIDATE,
                "Job outcome validation failed.",
            ),
            ErrorCode.OUTCOME_INVALID: DeterministicFailureSpec(
                ExecutionStage.OUTCOME_VALIDATE,
                "Job outcome validation failed.",
            ),
            ErrorCode.EXECUTION_RECORD_FAILED: DeterministicFailureSpec(
                ExecutionStage.EXECUTION_RECORD,
                "Execution record validation failed.",
            ),
            ErrorCode.RESOURCE_LIMIT_EXCEEDED: DeterministicFailureSpec(
                ExecutionStage.RESOURCE_STAGE,
                "Case resource capacity exceeded.",
            ),
            ErrorCode.RESOURCE_HASH_MISMATCH: DeterministicFailureSpec(
                ExecutionStage.RESOURCE_STAGE,
                "Resource publication validation failed.",
            ),
        }
    )
)


def deterministic_outcome_failure(
    code: ErrorCode,
    details: Iterable[ApplicationErrorDetail] = (),
) -> ExecutionFailure:
    """Construct one of S00's five byte-stable technical rejections."""

    try:
        spec = DETERMINISTIC_OUTCOME_FAILURE_SPECS[code]
    except KeyError as exc:
        raise ValueError(f"{code.value} is not a deterministic Outcome rejection") from exc
    ordered = sorted(
        details,
        key=lambda item: (
            item.field or "",
            item.resource_type or "",
            item.resource_id or "",
        ),
    )
    if code is ErrorCode.RESOURCE_LIMIT_EXCEEDED and not any(
        item.limit == 5_368_709_120
        and item.observed is not None
        and item.observed > item.limit
        for item in ordered
    ):
        raise ValueError(
            "RESOURCE_LIMIT_EXCEEDED requires limit=5368709120 and observed bytes"
        )
    return ExecutionFailure(
        stage=spec.stage,
        code=code,
        message=spec.message,
        retryable=False,
        details=ordered,
    )


class RuntimeInfrastructureError(Exception):
    """The sole typed exception raised when execution records cannot publish."""

    failure_id: OpaqueId
    execution_failure: ExecutionFailure

    def __init__(self, failure_id: OpaqueId, execution_failure: ExecutionFailure) -> None:
        TypeAdapter(OpaqueId).validate_python(failure_id)
        if execution_failure.stage is not ExecutionStage.EXECUTION_RECORD:
            raise ValueError("RuntimeInfrastructureError stage must be EXECUTION_RECORD")
        if execution_failure.code is not ErrorCode.EXECUTION_RECORD_FAILED:
            raise ValueError("RuntimeInfrastructureError code must be EXECUTION_RECORD_FAILED")
        self.failure_id = failure_id
        self.execution_failure = execution_failure
        super().__init__(execution_failure.message)


__all__ = [
    "APPLICATION_ERROR_RETRYABLE_CODES",
    "ApplicationError",
    "ApplicationErrorDetail",
    "CLI_EXIT_CONFIG_OR_STATE_CORRUPT",
    "CLI_EXIT_BY_ERROR_CODE",
    "CLI_EXIT_REQUEST_OR_STATE_CONFLICT",
    "CLI_EXIT_RUNTIME_FAILURE",
    "CLI_EXIT_SUCCESS",
    "CLI_CONFIG_OR_STATE_CORRUPT_CODES",
    "CLI_REQUEST_OR_STATE_CONFLICT_CODES",
    "CLI_RUNTIME_FAILURE_CODES",
    "DETERMINISTIC_OUTCOME_FAILURE_SPECS",
    "DeterministicFailureSpec",
    "ERROR_CODES",
    "ERROR_SPECS",
    "EXECUTION_FAILURE_RETRYABLE_CODES",
    "ErrorCode",
    "ErrorSpec",
    "ExecutionFailure",
    "RuntimeInfrastructureError",
    "deterministic_outcome_failure",
]
