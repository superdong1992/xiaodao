"""Frozen application-facing error construction helpers.

S03 never invents a second exception hierarchy.  Every modeled failure that
crosses an application or JobControl Port is carried by the frozen
``ApplicationPortError`` and uses S00's retryability table verbatim.
"""

from __future__ import annotations

from collections.abc import Iterable

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    ErrorCode,
)
from problem_locator.contracts.errors import ERROR_SPECS


def application_error(
    code: ErrorCode,
    message: str,
    *,
    details: Iterable[ApplicationErrorDetail] = (),
) -> ApplicationError:
    """Build one public error with the frozen retryability policy."""

    return ApplicationError(
        code=code,
        message=message,
        details=list(details),
        retryable=ERROR_SPECS[code].application_retryable,
    )


def port_error(
    code: ErrorCode,
    message: str,
    *,
    details: Iterable[ApplicationErrorDetail] = (),
) -> ApplicationPortError:
    """Wrap a modeled application error in S00's sole public exception."""

    return ApplicationPortError(application_error(code, message, details=details))


def raise_port_error(
    code: ErrorCode,
    message: str,
    *,
    details: Iterable[ApplicationErrorDetail] = (),
) -> None:
    """Raise a frozen typed Port failure without a private carrier."""

    raise port_error(code, message, details=details)


__all__ = ["application_error", "port_error", "raise_port_error"]
