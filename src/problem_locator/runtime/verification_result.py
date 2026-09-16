"""Server-owned decision verification value shared by final result builders."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import (
    DecisionAuditV2, DiagnosisMode, DiagnosisOutcome, DiagnosisResolutionStatus,
    Job, JobType, ReviewPolicy,
)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One immutable audit decision plus its bounded raw-line transcript."""

    audit: DecisionAuditV2
    positive_gate_passed: bool
    decision_evidence_bytes: bytes
    partial_evidence_selected: bool = False
    finding_rule_ids: tuple[tuple[str, ...], ...] = ()
    evidence_validation_mode: str = "strict"

    def permits_missing_targets(self, job: Job, payload: object) -> bool:
        return (
            (self.partial_evidence_selected or self.evidence_validation_mode == "advisory")
            and self.positive_gate_passed
            and job.job_type is JobType.DIAGNOSE
            and job.diagnosis_mode is DiagnosisMode.SPECIALIZED
            and (job.review_policy is ReviewPolicy.NONE or self.evidence_validation_mode == "advisory")
            and isinstance(payload, DiagnosisOutcome)
            and payload.candidate_conclusion_draft is not None
            and payload.candidate_conclusion_draft.resolution_status is DiagnosisResolutionStatus.PARTIAL
        )


__all__ = ["VerificationResult"]
