"""Private failure plumbing for the S04 runtime implementation.

The public vocabulary remains the frozen S00 :class:`ExecutionFailure` DTO.
This exception only moves that DTO between S04's internal components; it does
not add a second wire model or error code.
"""

from __future__ import annotations

from collections.abc import Iterable

from problem_locator.contracts.enums import ErrorCode, ExecutionStage
from problem_locator.contracts.models import ApplicationErrorDetail, ExecutionFailure


class RuntimeExecutionError(Exception):
    """Stop one execution with an already-normalized S00 failure."""

    def __init__(self, failure: ExecutionFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


def runtime_failure(
    *,
    stage: ExecutionStage,
    code: ErrorCode,
    message: str,
    retryable: bool = False,
    details: Iterable[ApplicationErrorDetail] = (),
) -> RuntimeExecutionError:
    """Build an internal exception carrying the frozen public failure DTO."""

    return RuntimeExecutionError(
        ExecutionFailure(
            stage=stage,
            code=code,
            message=message,
            retryable=retryable,
            details=list(details),
        )
    )


__all__ = ["RuntimeExecutionError", "runtime_failure"]
