"""Conditional claim and typed Runtime execution."""

from __future__ import annotations

from dataclasses import dataclass

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

from .backoff import (
    InterruptibleSubmissionBackoff,
    SubmissionBackoff,
    submission_backoff_delay,
)
from .cancellation import CancellationController
from .execution_lease import ExecutionPermit
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
        execution_permit: ExecutionPermit | None = None,
        shutdown_signal: SchedulerShutdownSignal | None = None,
        submission_backoff: SubmissionBackoff | None = None,
    ) -> None:
        self._job_control = job_control
        self._epoch_context = epoch_context
        self._execution_permit = (
            execution_permit if execution_permit is not None else ExecutionPermit()
        )
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
        with self._execution_permit.hold():
            return self._execute_one_with_permit(job_id, cancellation)

    def request_shutdown(self) -> bool:
        requested = self._shutdown_signal.request()
        if requested:
            self._submission_backoff.wake_for_shutdown()
        return requested

    def _execute_one_with_permit(
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
        while not self._shutdown_signal.is_requested():
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
                if not self._wait_before_retry(failed_attempt):
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
        while not self._shutdown_signal.is_requested():
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
                if not self._wait_before_retry(failed_attempt):
                    return None
                failed_attempt += 1
        return None

    def _wait_before_retry(self, failed_attempt: int) -> bool:
        if self._shutdown_signal.is_requested():
            return False
        waited = self._submission_backoff.wait(
            submission_backoff_delay(failed_attempt)
        )
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
