"""Pure receipt, replay, and active/stale classification for Job outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from problem_locator.contracts import (
    CandidateTarget,
    CaseAggregate,
    ErrorCode,
    ExecutionFileRef,
    JobOutcome,
    JobStatus,
    JobType,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    PublishedJobReceipt,
    RuntimeExecutionReceipt,
    canonical_json_bytes,
)


class OutcomeReplayDisposition(Enum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


class OutcomeActivity(Enum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    INVALID = "INVALID"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class PublishedOutcomeValidation:
    outcome: JobOutcome | None
    error_code: ErrorCode | None


@dataclass(frozen=True, slots=True)
class OutcomeActivityDecision:
    activity: OutcomeActivity
    job_id: str
    error_code: ErrorCode | None


def validate_published_outcome(
    expected_outcome: JobOutcome,
    expected_file_ref: ExecutionFileRef,
    published: RuntimeExecutionReceipt | None,
) -> PublishedOutcomeValidation:
    """Trust only the finalized record and compare the Worker receipt exactly."""

    if published is None:
        return PublishedOutcomeValidation(
            outcome=None,
            error_code=ErrorCode.OUTCOME_MISSING,
        )
    if (
        published.outcome_file_ref != expected_file_ref
        or canonical_json_bytes(published.job_outcome)
        != canonical_json_bytes(expected_outcome)
    ):
        return PublishedOutcomeValidation(
            outcome=None,
            error_code=ErrorCode.OUTCOME_INVALID,
        )
    return PublishedOutcomeValidation(
        outcome=published.job_outcome,
        error_code=None,
    )


def decide_outcome_replay(
    aggregate: CaseAggregate,
    outcome_id: str,
    outcome_hash: str,
) -> OutcomeReplayDisposition:
    """Classify the persisted natural key without changing revision."""

    existing = aggregate.outcome_processing_records.get(outcome_id)
    if existing is None:
        return OutcomeReplayDisposition.NEW
    if existing.outcome_hash == outcome_hash:
        return OutcomeReplayDisposition.DUPLICATE
    return OutcomeReplayDisposition.CONFLICT


def classify_outcome_activity(
    aggregate: CaseAggregate,
    outcome: JobOutcome,
) -> OutcomeActivityDecision:
    """Separate forged bindings from a valid but overtaken execution result."""

    job = aggregate.jobs.get(outcome.job_id)
    if job is None:
        return OutcomeActivityDecision(
            activity=OutcomeActivity.JOB_NOT_FOUND,
            job_id=outcome.job_id,
            error_code=ErrorCode.JOB_NOT_FOUND,
        )
    if (
        outcome.case_id != aggregate.case.case_id
        or outcome.case_id != job.case_id
        or outcome.job_type is not job.job_type
        or outcome.base_state_revision != job.base_state_revision
    ):
        return OutcomeActivityDecision(
            activity=OutcomeActivity.INVALID,
            job_id=job.job_id,
            error_code=ErrorCode.OUTCOME_INVALID,
        )
    if (
        job.status is not JobStatus.RUNNING
        or aggregate.case.active_job_id != job.job_id
        or aggregate.case.diagnosis_state.revision != outcome.base_state_revision
    ):
        return OutcomeActivityDecision(
            activity=OutcomeActivity.STALE,
            job_id=job.job_id,
            error_code=None,
        )
    if job.job_type is JobType.REVIEW:
        candidate = aggregate.case.diagnosis_state.candidate_conclusion
        current_target = (
            None
            if candidate is None
            else CandidateTarget(
                candidate_conclusion_id=candidate.conclusion_id,
                candidate_revision=candidate.revision,
                candidate_content_hash=candidate.content_hash,
            )
        )
        if current_target != job.review_target:
            return OutcomeActivityDecision(
                activity=OutcomeActivity.STALE,
                job_id=job.job_id,
                error_code=None,
            )
    return OutcomeActivityDecision(
        activity=OutcomeActivity.ACTIVE,
        job_id=job.job_id,
        error_code=None,
    )


def validate_published_job_recovery(
    receipt: PublishedJobReceipt,
    *,
    job_id: str,
    case_id: str,
    created_at: str,
) -> bool:
    """Validate the stable fields required before reusing old bindings."""

    job = receipt.job
    return (
        job.job_id == job_id
        and job.case_id == case_id
        and job.status is JobStatus.PENDING
        and job.created_at == created_at
        and job.started_at is None
        and job.finished_at is None
        and job.runtime_epoch is None
    )


def make_outcome_processing_record(
    outcome: JobOutcome,
    outcome_file_ref: ExecutionFileRef,
    *,
    disposition: OutcomeDisposition,
    processed_at: str,
    error_code: ErrorCode | None,
    accepted_evidence_ids: list[str],
    accepted_artifact_ids: list[str],
    created_job_id: str | None,
    reason: str,
) -> OutcomeProcessingRecord:
    """Construct the paired audit record from the canonical finalized bytes."""

    return OutcomeProcessingRecord(
        outcome_id=outcome.outcome_id,
        job_id=outcome.job_id,
        outcome_hash=outcome_file_ref.sha256,
        outcome_file_ref=outcome_file_ref,
        disposition=disposition,
        processed_at=processed_at,
        error_code=error_code,
        accepted_evidence_ids=accepted_evidence_ids,
        accepted_artifact_ids=accepted_artifact_ids,
        created_job_id=created_job_id,
        reason=reason,
    )


__all__ = [
    "OutcomeActivity",
    "OutcomeActivityDecision",
    "OutcomeReplayDisposition",
    "PublishedOutcomeValidation",
    "classify_outcome_activity",
    "decide_outcome_replay",
    "make_outcome_processing_record",
    "validate_published_job_recovery",
    "validate_published_outcome",
]
