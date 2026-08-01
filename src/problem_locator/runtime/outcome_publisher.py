"""Canonical Runtime outcome publication and the sole infrastructure escape."""

from __future__ import annotations

from typing import NoReturn

from problem_locator.contracts import (
    ApplicationPortError,
    CaseAggregate,
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
    WorkspaceInputManifest,
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
        resource_context: WorkspaceInputManifest | CaseAggregate | None = None,
    ) -> RuntimeExecutionReceipt:
        outcome = self.failure_outcome(job, failure)
        published = self._try_publish(job, outcome)
        if published is not None:
            return published
        recovered = self._recover_after_publish_failure(
            job,
            outcome,
            resource_context,
        )
        if recovered is not None:
            return recovered
        self._raise_infrastructure_error()

    def publish_success(
        self,
        job: Job,
        outcome: JobOutcome,
        resource_context: WorkspaceInputManifest | CaseAggregate | None = None,
    ) -> RuntimeExecutionReceipt:
        validate_outcome_for_job(job, outcome, resource_context)
        published = self._try_publish(job, outcome)
        if published is not None:
            return published

        # A failed publish may have crossed the store's atomic replace point.
        # Resolve that ambiguity through the frozen read Port before creating
        # different bytes for the same durable-outbox path.
        recovered = self._recover_after_publish_failure(
            job,
            outcome,
            resource_context,
        )
        if recovered is not None:
            return recovered
        return self.publish_failure(
            job,
            _EXECUTION_RECORD_FAILURE,
            resource_context,
        )

    def _try_publish(
        self,
        job: Job,
        outcome: JobOutcome,
    ) -> RuntimeExecutionReceipt | None:
        try:
            return self._publish(job, outcome)
        except ApplicationPortError:
            return None
        except Exception:
            # Invalid receipts and non-conforming adapter failures are not a
            # success signal.  Do not inspect their text or retain them on the
            # public RuntimeInfrastructureError exception chain.
            return None

    def _try_read_published(
        self,
        job: Job,
    ) -> tuple[bool, RuntimeExecutionReceipt | None]:
        try:
            receipt = self._execution_records.read_published_outcome(job.job_id)
        except ApplicationPortError:
            return False, None
        except Exception:
            return False, None
        if receipt is not None and not isinstance(receipt, RuntimeExecutionReceipt):
            return False, None
        return True, receipt

    def _recover_after_publish_failure(
        self,
        job: Job,
        outcome: JobOutcome,
        resource_context: WorkspaceInputManifest | CaseAggregate | None,
    ) -> RuntimeExecutionReceipt | None:
        readable, existing = self._try_read_published(job)
        if not readable:
            self._raise_infrastructure_error()
        if existing is None:
            return None

        expected_bytes = canonical_json_bytes(outcome)
        existing_bytes = canonical_json_bytes(existing.job_outcome)
        if existing_bytes != expected_bytes:
            valid_existing = True
            try:
                validate_outcome_for_job(
                    job,
                    existing.job_outcome,
                    resource_context,
                )
            except Exception:
                valid_existing = False
            if not valid_existing:
                self._raise_infrastructure_error()

        # Re-adopt the authoritative bytes through publish so the store can
        # finish chmod/fsync work after an earlier replace-stage failure.  A
        # different valid existing Outcome wins the immutable outbox path.
        republished = self._try_publish(job, existing.job_outcome)
        if republished is not None:
            return republished
        self._raise_infrastructure_error()

    def _raise_infrastructure_error(self) -> NoReturn:
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
