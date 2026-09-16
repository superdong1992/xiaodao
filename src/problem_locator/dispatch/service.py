"""Public S05 service facade implementing the frozen Dispatcher Port."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

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
from problem_locator.diagnostics import log_event
from problem_locator.operational import OperationalState

from .backoff import InterruptibleSubmissionBackoff, SubmissionBackoff
from .dispatcher import InProcessDispatcher
from .recovery import RecoveryCoordinator, RecoveryResult
from .runtime_epoch import RuntimeEpochContext, RuntimeEpochFactory
from .shutdown import SchedulerShutdownSignal
from .worker import DeliverySubmissionExpired, JobWorker


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
        route_workers: int = 1,
        diagnose_workers: int = 2,
        operational_state: OperationalState | None = None,
        submission_window_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.operational_state = operational_state or OperationalState()
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
        epoch_context = RuntimeEpochContext()
        self._epoch_context = epoch_context
        worker = JobWorker(
            job_control,
            runtime,
            epoch_context,
            shutdown_signal=self._shutdown_signal,
            submission_backoff=self._backoff,
            submission_window_seconds=submission_window_seconds,
            monotonic=monotonic,
        )
        self._dispatcher = InProcessDispatcher(
            worker,
            job_identity=lambda key: self._identity(repository, key),
            route_workers=route_workers,
            diagnose_workers=diagnose_workers,
            on_fatal_worker_error=self._record_fatal_worker_error,
            operational_state=self.operational_state,
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

    @staticmethod
    def _identity(repository, key):
        job = repository.read_job(key)
        return job.case_id, job.job_type, job.status

    @property
    def ready(self) -> bool:
        with self._lock:
            return (
                self._recovery_result is not None
                and self._recovery_result.completed
                and self._fatal_worker_error_type is None
                and self._dispatcher.claiming_enabled
                and self.operational_state.accepting
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
        if result.runtime_epoch is not None:
            self.operational_state.install_epoch(result.runtime_epoch)
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

    def _record_fatal_worker_error(self, job_id: str, error: Exception) -> None:
        # Recovery can enable a queued worker before start() has returned.
        runtime_epoch = self._epoch_context.current
        if runtime_epoch is not None:
            self.operational_state.install_epoch(runtime_epoch)
        secondary_code = None
        if isinstance(error, DeliverySubmissionExpired):
            case_id, phase, code = error.case_id, error.phase, error.error_code
            secondary_code = error.secondary_error_code
        else:
            # The dispatcher already owns this identity; failure reporting must
            # not make another potentially failing synchronous storage read.
            case_id = self._dispatcher.running_case_id(job_id)
            phase = "WORKER_EXECUTION"
            code = error.error.code if isinstance(error, ApplicationPortError) else ErrorCode.DISPATCH_REJECTED
        # These accepted Jobs cannot be claimed while this process is paused.
        # Keep their actual PENDING state, but do not leave clients polling forever.
        for queued_job_id, queued_case_id in self._dispatcher.queued_job_identities:
            self.operational_state.record(case_id=queued_case_id, job_id=queued_job_id,
                phase="DISPATCH_PAUSED", error_code=ErrorCode.DISPATCH_REJECTED)
        self.operational_state.record(case_id=case_id, job_id=job_id, phase=phase, error_code=code,
            secondary_error_code=secondary_code)
        application_error = (
            error.error if isinstance(error, ApplicationPortError) else None
        )
        log_event(
            "worker.job.fatal_error",
            level=logging.ERROR,
            job_id=job_id,
            error_code=code,
            application_error=application_error,
            error=error,
        )
        with self._lock:
            self._fatal_worker_error_type = type(error).__name__
            self._fatal_worker_error_code = code if isinstance(error, (ApplicationPortError, DeliverySubmissionExpired)) else None
