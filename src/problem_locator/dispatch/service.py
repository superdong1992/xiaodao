"""Public S05 service facade implementing the frozen Dispatcher Port."""

from __future__ import annotations

import threading

from problem_locator.contracts import (
    ApplicationPortError,
    CancelReceipt,
    DispatchReceipt,
    ExecutionRecordStore,
    ErrorCode,
    IdGenerator,
    JobControlPort,
    Runtime,
    StateRepository,
)

from .backoff import InterruptibleSubmissionBackoff, SubmissionBackoff
from .dispatcher import InProcessDispatcher
from .execution_lease import ExecutionPermit
from .recovery import RecoveryCoordinator, RecoveryResult
from .runtime_epoch import RuntimeEpochContext, RuntimeEpochFactory
from .shutdown import SchedulerShutdownSignal
from .worker import JobWorker


class SchedulerService:
    """Own the worker, Dispatcher, epoch, recovery readiness, and shutdown."""

    def __init__(
        self,
        repository: StateRepository,
        execution_records: ExecutionRecordStore,
        job_control: JobControlPort,
        runtime: Runtime,
        id_generator: IdGenerator,
        *,
        submission_backoff: SubmissionBackoff | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._recovery_result: RecoveryResult | None = None
        self._fatal_worker_error_type: str | None = None
        self._fatal_worker_error_code: ErrorCode | None = None
        self._backoff = (
            submission_backoff
            if submission_backoff is not None
            else InterruptibleSubmissionBackoff()
        )
        self._shutdown_signal = SchedulerShutdownSignal()
        self._execution_permit = ExecutionPermit()
        epoch_context = RuntimeEpochContext()
        worker = JobWorker(
            job_control,
            runtime,
            epoch_context,
            self._execution_permit,
            self._shutdown_signal,
            self._backoff,
        )
        self._dispatcher = InProcessDispatcher(
            worker,
            on_fatal_worker_error=self._record_fatal_worker_error,
        )
        self._recovery = RecoveryCoordinator(
            repository,
            execution_records,
            job_control,
            self._dispatcher,
            RuntimeEpochFactory(id_generator),
            epoch_context,
            self._shutdown_signal,
            self._backoff,
        )

    @property
    def ready(self) -> bool:
        with self._lock:
            return (
                self._recovery_result is not None
                and self._recovery_result.completed
                and self._fatal_worker_error_type is None
                and self._dispatcher.claiming_enabled
            )

    @property
    def recovery_result(self) -> RecoveryResult | None:
        with self._lock:
            return self._recovery_result

    @property
    def fatal_worker_error_type(self) -> str | None:
        with self._lock:
            return self._fatal_worker_error_type

    @property
    def fatal_worker_error_code(self) -> ErrorCode | None:
        with self._lock:
            return self._fatal_worker_error_code

    def start(self) -> RecoveryResult:
        self._dispatcher.start()
        result = self._recovery.recover()
        with self._lock:
            self._recovery_result = result
        return result

    def submit(self, job_id: str) -> DispatchReceipt:
        return self._dispatcher.submit(job_id)

    def cancel(self, job_id: str) -> CancelReceipt:
        return self._dispatcher.cancel(job_id)

    def shutdown(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        return self._dispatcher.shutdown(timeout_seconds)

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        return self._dispatcher.wait_until_idle(timeout_seconds)

    def _record_fatal_worker_error(self, _job_id: str, error: Exception) -> None:
        with self._lock:
            self._fatal_worker_error_type = type(error).__name__
            self._fatal_worker_error_code = (
                error.error.code
                if isinstance(error, ApplicationPortError)
                else None
            )
