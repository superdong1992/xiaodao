"""Canonical Runtime outcome publication and the sole infrastructure escape."""

from __future__ import annotations

from problem_locator.contracts import (
    Clock,
    ErrorCode,
    ExecutionFailure,
    ExecutionRecordStore,
    ExecutionStage,
    IdGenerator,
    Job,
    JobOutcome,
    OutcomeResultType,
    RuntimeExecutionReceipt,
    RuntimeInfrastructureError,
    canonical_json_bytes,
    validate_outcome_for_job,
)


_EXECUTION_RECORD_FAILURE = ExecutionFailure(
    stage=ExecutionStage.EXECUTION_RECORD,
    code=ErrorCode.EXECUTION_RECORD_FAILED,
    message="Execution record could not be published.",
    retryable=True,
    details=[],
)


class OutcomePublisher:
    """Finalize success or a normalized system failure as canonical bytes."""

    def __init__(
        self,
        execution_records: ExecutionRecordStore,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._execution_records = execution_records
        self._clock = clock
        self._id_generator = id_generator

    def failure_outcome(
        self,
        job: Job,
        failure: ExecutionFailure,
    ) -> JobOutcome:
        outcome = JobOutcome(
            outcome_id=self._id_generator.new("job_outcome"),
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            base_state_revision=job.base_state_revision,
            result_type=OutcomeResultType.FAILED,
            payload=None,
            consumed_evidence_refs=[],
            proposed_evidence=[],
            proposed_artifacts=[],
            error=failure,
            produced_at=self._clock.now(),
        )
        return validate_outcome_for_job(job, outcome)

    def publish_failure(
        self,
        job: Job,
        failure: ExecutionFailure,
    ) -> RuntimeExecutionReceipt:
        outcome = self.failure_outcome(job, failure)
        return self._publish_or_raise(job, outcome)

    def publish_success(
        self,
        job: Job,
        outcome: JobOutcome,
    ) -> RuntimeExecutionReceipt:
        validate_outcome_for_job(job, outcome)
        try:
            return self._publish(job, outcome)
        except Exception:
            # A success record that cannot publish is itself an execution-
            # record failure.  Attempt the normal replayable failure Outcome;
            # only failure of that second publication crosses the Runtime Port.
            return self.publish_failure(job, _EXECUTION_RECORD_FAILURE)

    def _publish_or_raise(
        self,
        job: Job,
        outcome: JobOutcome,
    ) -> RuntimeExecutionReceipt:
        try:
            return self._publish(job, outcome)
        except Exception:
            failure_id = self._id_generator.new("execution_failure")
            raise RuntimeInfrastructureError(
                failure_id,
                _EXECUTION_RECORD_FAILURE,
            ) from None

    def _publish(
        self,
        job: Job,
        outcome: JobOutcome,
    ) -> RuntimeExecutionReceipt:
        data = canonical_json_bytes(outcome)
        file_ref = self._execution_records.publish_outcome_bytes(job.job_id, data)
        return RuntimeExecutionReceipt(
            job_outcome=outcome,
            outcome_file_ref=file_ref,
        )


__all__ = ["OutcomePublisher"]
