"""Process-local admission and operational faults; never a persisted Case verdict."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import threading

from problem_locator.contracts import ApplicationError, ApplicationErrorDetail, ApplicationPortError, ErrorCode
from problem_locator.contracts.errors import ERROR_SPECS


@dataclass(frozen=True, slots=True)
class OperationalFault:
    runtime_epoch: str | None
    case_id: str | None
    job_id: str | None
    phase: str
    error_code: ErrorCode
    occurred_at: str
    secondary_error_code: ErrorCode | None = None
    persistence: str = "UNKNOWN"

    def as_error(self) -> ApplicationError:
        values = {"runtime_epoch": self.runtime_epoch, "case_id": self.case_id,
            "job_id": self.job_id, "phase": self.phase, "persistence": self.persistence,
            "cause_code": self.error_code.value, "occurred_at": self.occurred_at,
            "secondary_error_code": None if self.secondary_error_code is None else self.secondary_error_code.value}
        message = {
            "ARCHIVE_STATUS_COMMIT": "报告已生成，但归档状态暂时无法确认。",
            "DISPATCH_PAUSED": "服务异常，排队任务已暂停，最终状态尚未确认。",
        }.get(self.phase, "任务交付异常，最终状态暂时无法确认。")
        return ApplicationError(code=ErrorCode.DISPATCH_REJECTED, message=message,
            retryable=ERROR_SPECS[ErrorCode.DISPATCH_REJECTED].application_retryable,
            details=[ApplicationErrorDetail(field=name, actual=value, resource_type=None,
                resource_id=None, resource_ref=None, expected=None, limit=None, observed=None)
                for name, value in values.items() if value is not None])


class OperationalState:
    """Shared fail-closed admission latch and safe evidence for this process epoch."""

    def __init__(self, now: Callable[[], str] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        self._lock = threading.RLock()
        self._accepting = True
        self._runtime_epoch: str | None = None
        self._faults: dict[tuple[str | None, str | None, str], OperationalFault] = {}
        self._confirmed_jobs: set[str] = set()

    def install_epoch(self, runtime_epoch: str) -> None:
        with self._lock:
            if self._runtime_epoch not in {None, runtime_epoch}:
                raise RuntimeError("operational state cannot cross process epochs")
            self._runtime_epoch = runtime_epoch

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def stop_accepting(self) -> None:
        with self._lock:
            self._accepting = False

    def require_accepting(self) -> None:
        with self._lock:
            if self._accepting:
                return
        # Admission does not disclose another Case's operational identifiers.
        error = ApplicationError(code=ErrorCode.DISPATCH_REJECTED,
            message="服务已暂停接收新任务。", details=[],
            retryable=ERROR_SPECS[ErrorCode.DISPATCH_REJECTED].application_retryable)
        raise ApplicationPortError(error)

    def record(self, *, case_id: str | None, job_id: str | None, phase: str,
               error_code: ErrorCode, secondary_error_code: ErrorCode | None = None) -> OperationalFault:
        with self._lock:
            fault = OperationalFault(self._runtime_epoch, case_id, job_id, phase,
                error_code, self._now(), secondary_error_code)
            self._faults[(case_id, job_id, phase)] = fault
            self._accepting = False
            return fault

    @property
    def faults(self) -> tuple[OperationalFault, ...]:
        with self._lock:
            return tuple(self._faults.values())

    @property
    def latest_error(self) -> ApplicationError | None:
        faults = self.faults
        if not faults:
            return None
        # Missed queue signals are consequences; readiness should retain the
        # underlying delivery/storage fault when this process has one.
        primary = next((fault for fault in reversed(faults) if fault.phase != "DISPATCH_PAUSED"), faults[-1])
        return primary.as_error()

    def confirm_finished_job(self, job_id: str) -> None:
        """A fresh durable Job terminal confirms delivery without reopening admission."""
        with self._lock:
            if any(fault.job_id == job_id for fault in self._faults.values()):
                self._confirmed_jobs.add(job_id)

    def error_for_case(self, case_id: str, case_status: str | None,
                       archive_status: str | None = None, *, include_archive_faults: bool = True) -> ApplicationError | None:
        """Read confirmed state first; never replace a committed result with UNKNOWN."""
        terminal = case_status in {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED"}
        with self._lock:
            faults = tuple(self._faults.values())
            confirmed_jobs = self._confirmed_jobs.copy()
        for fault in reversed(faults):
            if fault.case_id not in {None, case_id}:
                continue
            if fault.phase == "ARCHIVE_STATUS_COMMIT":
                if include_archive_faults and archive_status in {None, "PENDING"}:
                    return fault.as_error()
            elif not terminal and fault.job_id not in confirmed_jobs:
                return fault.as_error()
        return None
