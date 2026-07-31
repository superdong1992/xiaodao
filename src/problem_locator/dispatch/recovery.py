"""Replay-before-interrupt startup recovery coordination."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    ExecutionRecordStore,
    Job,
    JobControlPort,
    JobStatus,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    RecoveryProcessingRecord,
    RecoveryReceipt,
    RuntimeExecutionReceipt,
    StateFile,
    StateRepository,
)

from .backoff import (
    InterruptibleSubmissionBackoff,
    SubmissionBackoff,
    submission_backoff_delay,
)
from .dispatcher import InProcessDispatcher
from .runtime_epoch import RuntimeEpochContext, RuntimeEpochFactory
from .shutdown import SchedulerShutdownSignal


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    runtime_epoch: str | None
    completed: bool
    replayed_job_ids: tuple[str, ...]
    interrupted_job_ids: tuple[str, ...]
    pending_job_ids: tuple[str, ...]
    failure_type: str | None = None
    failure_code: ErrorCode | None = None


class RecoveryInvariantError(RuntimeError):
    pass


class RecoveryCoordinator:
    """Replay finalized records before the audited old-epoch interruption."""

    def __init__(
        self,
        repository: StateRepository,
        execution_records: ExecutionRecordStore,
        job_control: JobControlPort,
        dispatcher: InProcessDispatcher,
        epoch_factory: RuntimeEpochFactory,
        epoch_context: RuntimeEpochContext,
        shutdown_signal: SchedulerShutdownSignal | None = None,
        submission_backoff: SubmissionBackoff | None = None,
    ) -> None:
        self._repository = repository
        self._execution_records = execution_records
        self._job_control = job_control
        self._dispatcher = dispatcher
        self._epoch_factory = epoch_factory
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

    def recover(self) -> RecoveryResult:
        self._dispatcher.pause_claiming()
        replayed: list[str] = []
        replayed_receipts: list[RuntimeExecutionReceipt] = []
        interrupted: tuple[str, ...] = ()
        pending: list[str] = []
        runtime_epoch: str | None = self._epoch_context.current
        try:
            initial = self._repository.read_snapshot()
            runtime_epoch = self._epoch_factory.create(
                record.runtime_epoch for record in initial.runtime_epochs
            )
            self._epoch_context.install(runtime_epoch)

            for job_id in self._unprocessed_job_ids(initial):
                self._require_not_stopping()
                receipt = self._execution_records.read_published_outcome(job_id)
                if receipt is None:
                    continue
                self._require_published_receipt_matches_job(initial, job_id, receipt)
                if not self._submit_outcome(receipt):
                    raise RecoveryInvariantError(
                        "service shutdown interrupted Outcome replay"
                    )
                replayed.append(job_id)
                replayed_receipts.append(receipt)

            # A fresh generation separates finalized replay from interruption.
            self._require_not_stopping()
            replayed_state = self._repository.read_snapshot()
            self._require_replays_persisted(replayed_state, replayed_receipts)
            recovery_receipt = self._job_control.interrupt_previous_epoch(
                runtime_epoch,
                runtime_epoch,
            )
            if recovery_receipt.recovery_id != runtime_epoch:
                raise RecoveryInvariantError("recovery receipt changed recovery_id")

            recovered_state = self._repository.read_snapshot()
            processing_record = self._require_completed_recovery(
                recovered_state,
                replayed_state,
                runtime_epoch,
                recovery_receipt,
                replayed,
            )
            interrupted = tuple(processing_record.interrupted_job_ids)
            pending = list(processing_record.pending_job_ids)
            for job_id in pending:
                self._require_not_stopping()
                receipt = self._dispatcher.submit(job_id)
                if receipt.job_id != job_id:
                    raise RecoveryInvariantError(
                        "Dispatcher receipt changed the persisted PENDING Job ID"
                    )
                if not (receipt.accepted or receipt.duplicate):
                    raise RecoveryInvariantError("Dispatcher rejected a persisted PENDING Job")

            self._dispatcher.enable_claiming()
            return RecoveryResult(
                runtime_epoch=runtime_epoch,
                completed=True,
                replayed_job_ids=tuple(replayed),
                interrupted_job_ids=interrupted,
                pending_job_ids=tuple(pending),
            )
        except Exception as exc:
            self._dispatcher.pause_claiming()
            return RecoveryResult(
                runtime_epoch=runtime_epoch,
                completed=False,
                replayed_job_ids=tuple(replayed),
                interrupted_job_ids=interrupted,
                pending_job_ids=tuple(pending),
                failure_type=type(exc).__name__,
                failure_code=(
                    exc.error.code
                    if isinstance(exc, ApplicationPortError)
                    else None
                ),
            )

    def _submit_outcome(self, receipt: RuntimeExecutionReceipt) -> bool:
        failed_attempt = 0
        while not self._shutdown_signal.is_requested():
            try:
                outcome_receipt = self._job_control.submit_outcome(
                    receipt.job_outcome,
                    receipt.outcome_file_ref,
                )
                if (
                    outcome_receipt.case_view is not None
                    and outcome_receipt.case_view.case_id
                    != receipt.job_outcome.case_id
                ):
                    raise RecoveryInvariantError(
                        "Outcome replay receipt changed case_id"
                    )
                return True
            except ApplicationPortError as exc:
                if (
                    exc.error.code
                    not in JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES
                ):
                    raise
                if not self._wait_before_retry(failed_attempt):
                    return False
                failed_attempt += 1
        return False

    def _wait_before_retry(self, failed_attempt: int) -> bool:
        if self._shutdown_signal.is_requested():
            return False
        waited = self._submission_backoff.wait(
            submission_backoff_delay(failed_attempt)
        )
        return waited and not self._shutdown_signal.is_requested()

    def _require_not_stopping(self) -> None:
        if self._shutdown_signal.is_requested():
            raise RecoveryInvariantError("service shutdown interrupted recovery")

    @staticmethod
    def _unprocessed_job_ids(state: StateFile) -> list[str]:
        rows: list[tuple[str, str]] = []
        for case_id, aggregate in state.cases.items():
            processed_job_ids = {
                record.job_id for record in aggregate.outcome_processing_records.values()
            }
            rows.extend(
                (case_id, job_id)
                for job_id in aggregate.jobs
                if job_id not in processed_job_ids
            )
        return [job_id for _, job_id in sorted(rows)]

    @staticmethod
    def _pending_job_ids(state: StateFile) -> list[str]:
        return sorted(
            job_id
            for aggregate in state.cases.values()
            for job_id, job in aggregate.jobs.items()
            if job.status is JobStatus.PENDING
        )

    @staticmethod
    def _require_published_receipt_matches_job(
        state: StateFile,
        job_id: str,
        receipt: RuntimeExecutionReceipt,
    ) -> None:
        jobs: list[Job] = [
            aggregate.jobs[job_id]
            for aggregate in state.cases.values()
            if job_id in aggregate.jobs
        ]
        if len(jobs) != 1:
            raise RecoveryInvariantError("replay Job is missing from the snapshot")
        job = jobs[0]
        outcome = receipt.job_outcome
        if (
            outcome.job_id != job.job_id
            or outcome.case_id != job.case_id
            or outcome.job_type is not job.job_type
            or outcome.base_state_revision != job.base_state_revision
        ):
            raise RecoveryInvariantError(
                "published Runtime receipt does not match its persisted Job"
            )

    @staticmethod
    def _require_replays_persisted(
        state: StateFile,
        replayed_receipts: list[RuntimeExecutionReceipt],
    ) -> None:
        aggregates_by_case_id = {
            aggregate.case.case_id: aggregate
            for aggregate in state.cases.values()
        }
        for receipt in replayed_receipts:
            outcome = receipt.job_outcome
            aggregate = aggregates_by_case_id.get(outcome.case_id)
            processing = (
                None
                if aggregate is None
                else aggregate.outcome_processing_records.get(outcome.outcome_id)
            )
            if (
                processing is None
                or processing.job_id != outcome.job_id
                or processing.outcome_hash != receipt.outcome_file_ref.sha256
                or processing.outcome_file_ref != receipt.outcome_file_ref
            ):
                raise RecoveryInvariantError(
                    "successful Outcome replay was not exactly persisted before interruption"
                )
            job = aggregate.jobs.get(outcome.job_id)
            if job is None or job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
                raise RecoveryInvariantError(
                    "successful Outcome replay left its Job active"
                )

    @classmethod
    def _require_completed_recovery(
        cls,
        state: StateFile,
        pre_interrupt_state: StateFile,
        runtime_epoch: str,
        receipt: RecoveryReceipt,
        replayed_job_ids: list[str],
    ) -> RecoveryProcessingRecord:
        records = [
            record
            for record in state.runtime_epochs
            if record.runtime_epoch == runtime_epoch
        ]
        if len(records) != 1 or records[0].recovery_id != runtime_epoch:
            raise RecoveryInvariantError("current runtime epoch record is missing")
        if records[0].recovery_completed_at is None:
            raise RecoveryInvariantError("runtime epoch recovery is not completed")
        processing = state.recovery_processing_records.get(runtime_epoch)
        if processing is None:
            raise RecoveryInvariantError("recovery processing record is missing")
        if (
            processing.recovery_id != runtime_epoch
            or processing.current_runtime_epoch != runtime_epoch
            or processing.completed_at is None
            or processing.completed_at != records[0].recovery_completed_at
        ):
            raise RecoveryInvariantError("recovery processing record is not completed")
        if (
            receipt.interrupted_job_ids != processing.interrupted_job_ids
            or receipt.pending_job_ids != processing.pending_job_ids
        ):
            raise RecoveryInvariantError(
                "recovery receipt does not match the persisted processing record"
            )
        if set(replayed_job_ids) & (
            set(processing.interrupted_job_ids) | set(processing.pending_job_ids)
        ):
            raise RecoveryInvariantError(
                "a replayed Outcome Job remained in the recovery work lists"
            )
        if processing.pending_job_ids != cls._pending_job_ids(state):
            raise RecoveryInvariantError(
                "persisted recovery pending list does not match StateFile"
            )
        jobs = {
            job_id: job
            for aggregate in state.cases.values()
            for job_id, job in aggregate.jobs.items()
        }
        pre_interrupt_jobs = {
            job_id: job
            for aggregate in pre_interrupt_state.cases.values()
            for job_id, job in aggregate.jobs.items()
        }
        preexisting_processing = (
            pre_interrupt_state.recovery_processing_records.get(runtime_epoch)
        )
        if preexisting_processing is not None and (
            preexisting_processing.recovery_id != processing.recovery_id
            or preexisting_processing.current_runtime_epoch
            != processing.current_runtime_epoch
            or preexisting_processing.interrupted_job_ids
            != processing.interrupted_job_ids
            or preexisting_processing.pending_job_ids
            != processing.pending_job_ids
            or (
                preexisting_processing.completed_at is not None
                and preexisting_processing.completed_at
                != processing.completed_at
            )
        ):
            raise RecoveryInvariantError(
                "replayed recovery changed its persisted processing identity"
            )
        if preexisting_processing is None:
            expected_interrupted = sorted(
                job_id
                for job_id, job in pre_interrupt_jobs.items()
                if job.status is JobStatus.RUNNING
                and job.runtime_epoch != runtime_epoch
            )
            expected_pending = cls._pending_job_ids(pre_interrupt_state)
            if (
                processing.interrupted_job_ids != expected_interrupted
                or processing.pending_job_ids != expected_pending
            ):
                raise RecoveryInvariantError(
                    "new recovery record does not match the pre-interrupt snapshot"
                )
        if any(
            job_id not in jobs
            or jobs[job_id].status is not JobStatus.INTERRUPTED
            or jobs[job_id].runtime_epoch == runtime_epoch
            for job_id in processing.interrupted_job_ids
        ):
            raise RecoveryInvariantError(
                "persisted recovery interrupted list does not match StateFile"
            )
        if preexisting_processing is None and any(
            job_id not in pre_interrupt_jobs
            or pre_interrupt_jobs[job_id].status is not JobStatus.RUNNING
            or pre_interrupt_jobs[job_id].runtime_epoch == runtime_epoch
            for job_id in processing.interrupted_job_ids
        ):
            raise RecoveryInvariantError(
                "new recovery reported a Job that was not old-epoch RUNNING"
            )
        if any(
            job.status is JobStatus.RUNNING
            and job.runtime_epoch != runtime_epoch
            for job in jobs.values()
        ):
            raise RecoveryInvariantError(
                "completed recovery left an old-epoch RUNNING Job"
            )
        return processing
