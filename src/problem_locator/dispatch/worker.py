"""Conditional claim and typed Runtime execution."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import time

from problem_locator.contracts import (
    ApplicationPortError,
    CancellationReason,
    CancellationSignal,
    ErrorCode,
    FailureReportDisposition,
    FailureReceipt,
    Job,
    JobControlPort,
    JobStatus,
    JobType,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    OutcomeDisposition,
    OutcomeReceipt,
    Runtime,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
)
from problem_locator.diagnostics import bind_diagnostics

from .backoff import (
    InterruptibleSubmissionBackoff,
    SubmissionBackoff,
    submission_backoff_delay,
)
from .cancellation import CancellationController
from .runtime_epoch import RuntimeEpochContext
from .shutdown import SchedulerShutdownSignal


_FAILURE_REPORT_RETRY_CODES = frozenset(
    {
        ErrorCode.STATE_WRITE_FAILED,
        ErrorCode.REVISION_CONFLICT,
    }
)


class SchedulerInvariantError(RuntimeError):
    """A successful Port receipt violated its frozen shape or binding."""


class DeliverySubmissionExpired(RuntimeError):
    """The bounded submission window ended; persistence is still unconfirmed."""

    def __init__(self, job_id: str, case_id: str, phase: str, error: ApplicationPortError,
                 secondary_error_code: ErrorCode | None = None):
        super().__init__("result submission window expired")
        self.job_id, self.case_id, self.phase = job_id, case_id, phase
        self.error_code = error.error.code
        self.secondary_error_code = secondary_error_code


class _TypedRuntimeWorker:
    job_type: JobType

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        if job.job_type is not self.job_type:
            raise SchedulerInvariantError(
                f"{type(self).__name__} cannot execute {job.job_type.value}"
            )
        return self._runtime.execute(job, cancellation)


class RoutingWorker(_TypedRuntimeWorker):
    job_type = JobType.ROUTE


class DiagnosisWorker(_TypedRuntimeWorker):
    job_type = JobType.DIAGNOSE


class ReviewWorker(_TypedRuntimeWorker):
    job_type = JobType.REVIEW


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    job_id: str
    claimed: bool
    runtime_called: bool
    delivery_completed: bool
    outcome_disposition: OutcomeDisposition | None = None
    failure_disposition: FailureReportDisposition | None = None


class JobWorker:
    """Execute once, then retry only the frozen delivery operations."""

    def __init__(
        self,
        job_control: JobControlPort,
        runtime: Runtime,
        epoch_context: RuntimeEpochContext,
        shutdown_signal: SchedulerShutdownSignal | None = None,
        submission_backoff: SubmissionBackoff | None = None,
        *,
        submission_window_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if submission_window_seconds <= 0:
            raise ValueError("submission window must be positive")
        self._submission_window_seconds = submission_window_seconds
        self._monotonic = monotonic
        self._job_control = job_control
        self._epoch_context = epoch_context
        self._shutdown_signal = (
            shutdown_signal
            if shutdown_signal is not None
            else SchedulerShutdownSignal()
        )
        self._submission_backoff = (
            submission_backoff
            if submission_backoff is not None
            else InterruptibleSubmissionBackoff()
        )
        self._typed_workers = {
            JobType.ROUTE: RoutingWorker(runtime),
            JobType.DIAGNOSE: DiagnosisWorker(runtime),
            JobType.REVIEW: ReviewWorker(runtime),
        }

    def execute_one(
        self,
        job_id: str,
        cancellation: CancellationController,
    ) -> WorkerRunResult:
        return self._execute_claimed(job_id, cancellation)

    def request_shutdown(self) -> bool:
        requested = self._shutdown_signal.request()
        if requested:
            self._submission_backoff.wake_for_shutdown()
        return requested

    def _execute_claimed(
        self,
        job_id: str,
        cancellation: CancellationController,
    ) -> WorkerRunResult:
        if self._shutdown_signal.is_requested():
            return WorkerRunResult(
                job_id=job_id,
                claimed=False,
                runtime_called=False,
                delivery_completed=False,
            )
        runtime_epoch = self._epoch_context.require()
        claim = self._job_control.claim_job(job_id, runtime_epoch)
        if not claim.claimed:
            return WorkerRunResult(
                job_id=job_id,
                claimed=False,
                runtime_called=False,
                delivery_completed=False,
            )

        job = claim.job
        if job is None:
            raise SchedulerInvariantError("claimed receipt omitted its Job")
        if job.job_id != job_id:
            raise SchedulerInvariantError("claimed Job ID differs from queued Job ID")
        if job.status is not JobStatus.RUNNING:
            raise SchedulerInvariantError("claimed Job must be RUNNING")
        if job.runtime_epoch != runtime_epoch:
            raise SchedulerInvariantError("claimed Job did not persist the current epoch")
        if self._shutdown_signal.is_requested():
            cancellation.cancel(CancellationReason.SERVICE_SHUTDOWN)
            cancellation.retire()
            return WorkerRunResult(
                job_id=job_id,
                claimed=True,
                runtime_called=False,
                delivery_completed=False,
            )

        infrastructure_error: RuntimeInfrastructureError | None = None
        try:
            with bind_diagnostics(
                case_id=job.case_id,
                job_id=job.job_id,
                job_type=job.job_type.value,
            ):
                receipt = self._typed_workers[job.job_type].execute(job, cancellation)
        except RuntimeInfrastructureError as exc:
            infrastructure_error = exc
        finally:
            cancellation.retire()

        if self._shutdown_signal.is_requested():
            return WorkerRunResult(
                job_id=job_id,
                claimed=True,
                runtime_called=True,
                delivery_completed=False,
            )

        if infrastructure_error is not None:
            failure_receipt = self._report_infrastructure_failure(
                job,
                runtime_epoch,
                infrastructure_error,
            )
            if failure_receipt is None:
                return WorkerRunResult(
                    job_id=job_id,
                    claimed=True,
                    runtime_called=True,
                    delivery_completed=False,
                )
            if failure_receipt.failure_id != infrastructure_error.failure_id:
                raise SchedulerInvariantError("failure receipt changed failure_id")
            if (
                failure_receipt.case_view is not None
                and failure_receipt.case_view.case_id != job.case_id
            ):
                raise SchedulerInvariantError("failure receipt changed case_id")
            return WorkerRunResult(
                job_id=job_id,
                claimed=True,
                runtime_called=True,
                delivery_completed=True,
                failure_disposition=failure_receipt.disposition,
            )

        self._validate_runtime_receipt(job, receipt)

        outcome_receipt = self._submit_outcome(receipt)
        if outcome_receipt is None:
            return WorkerRunResult(
                job_id=job_id,
                claimed=True,
                runtime_called=True,
                delivery_completed=False,
            )
        if (
            outcome_receipt.case_view is not None
            and outcome_receipt.case_view.case_id != job.case_id
        ):
            raise SchedulerInvariantError("outcome receipt changed case_id")
        return WorkerRunResult(
            job_id=job_id,
            claimed=True,
            runtime_called=True,
            delivery_completed=True,
            outcome_disposition=outcome_receipt.disposition,
        )

    def _submit_outcome(
        self,
        receipt: RuntimeExecutionReceipt,
    ) -> OutcomeReceipt | None:
        failed_attempt = 0
        deadline = self._monotonic() + self._submission_window_seconds
        last_error: ApplicationPortError | None = None
        while not self._shutdown_signal.is_requested():
            if last_error is not None and self._monotonic() >= deadline:
                raise DeliverySubmissionExpired(receipt.job_outcome.job_id,
                    receipt.job_outcome.case_id, "RESULT_DELIVERY", last_error) from None
            try:
                return self._job_control.submit_outcome(
                    receipt.job_outcome,
                    receipt.outcome_file_ref,
                )
            except ApplicationPortError as exc:
                if (
                    exc.error.code
                    not in JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES
                ):
                    raise
                last_error = exc
                if self._monotonic() >= deadline:
                    raise DeliverySubmissionExpired(receipt.job_outcome.job_id,
                        receipt.job_outcome.case_id, "RESULT_DELIVERY", exc) from None
                if not self._wait_before_retry(failed_attempt, deadline):
                    return None
                failed_attempt += 1
        return None

    def _report_infrastructure_failure(
        self,
        job: Job,
        runtime_epoch: str,
        infrastructure_error: RuntimeInfrastructureError,
    ) -> FailureReceipt | None:
        failed_attempt = 0
        deadline = self._monotonic() + self._submission_window_seconds
        last_error: ApplicationPortError | None = None
        while not self._shutdown_signal.is_requested():
            if last_error is not None and self._monotonic() >= deadline:
                raise DeliverySubmissionExpired(job.job_id, job.case_id, "FAILURE_REPORT", last_error,
                    infrastructure_error.execution_failure.code) from None
            try:
                return self._job_control.report_execution_infrastructure_failure(
                    job.job_id,
                    runtime_epoch,
                    infrastructure_error.failure_id,
                    infrastructure_error.execution_failure,
                )
            except ApplicationPortError as exc:
                if exc.error.code not in _FAILURE_REPORT_RETRY_CODES:
                    raise
                last_error = exc
                if self._monotonic() >= deadline:
                    raise DeliverySubmissionExpired(job.job_id, job.case_id, "FAILURE_REPORT", exc,
                        infrastructure_error.execution_failure.code) from None
                if not self._wait_before_retry(failed_attempt, deadline):
                    return None
                failed_attempt += 1
        return None

    def _wait_before_retry(self, failed_attempt: int, deadline: float | None = None) -> bool:
        if self._shutdown_signal.is_requested():
            return False
        delay = submission_backoff_delay(failed_attempt)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - self._monotonic()))
        # This bounds retries, not a synchronous Port call already in progress.
        waited = self._submission_backoff.wait(delay)
        return waited and not self._shutdown_signal.is_requested()

    @staticmethod
    def _validate_runtime_receipt(job: Job, receipt: RuntimeExecutionReceipt) -> None:
        outcome = receipt.job_outcome
        if (
            outcome.job_id != job.job_id
            or outcome.case_id != job.case_id
            or outcome.job_type is not job.job_type
            or outcome.base_state_revision != job.base_state_revision
        ):
            raise SchedulerInvariantError("Runtime receipt does not match the claimed Job")
