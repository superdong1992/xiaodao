from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    CandidateMutationAction,
    CandidateStatus,
    JobType,
    ReviewAssessment,
    ReviewOutcomeTriggerPayload,
    ReviewVerdict,
    TriggerType,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnose_job,
    rebuild,
    review_job,
    review_outcome,
    runtime_bindings,
    snapshot_with_active,
    trigger,
)


def test_review_pass_is_the_only_resolution_gate() -> None:
    source = review_job()
    outcome = review_outcome()
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "RESOLVED"
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.action is CandidateMutationAction.SET_STATUS
    assert plan.candidate_mutation.target_status is CandidateStatus.ACCEPTED
    assert plan.final_result_target == source.review_target
    assert plan.next_job_spec is None
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


@pytest.mark.parametrize(
    ("verdict", "issues"),
    [
        (ReviewVerdict.NEED_MORE_EVIDENCE, {"missing_evidence": ["Need a retry trace."]}),
        (ReviewVerdict.REJECT, {"evidence_conflicts": ["The timestamps conflict."]}),
    ],
)
def test_non_pass_review_rejects_candidate_and_returns_to_diagnosis(
    verdict: ReviewVerdict,
    issues: dict[str, list[str]],
) -> None:
    source = review_job()
    base = review_outcome()
    assessment = base.payload
    assert isinstance(assessment, ReviewAssessment)
    values = assessment.model_dump(mode="python")
    values.update(
        {
            "verdict": verdict,
            "unsupported_findings": [],
            "evidence_conflicts": [],
            "missing_evidence": [],
            "stale_references": [],
            "recommendation": "Collect more evidence and reassess the timeout.",
        }
    )
    values.update(issues)
    changed = ReviewAssessment.model_validate(values)
    outcome = rebuild(base, payload=changed)
    snapshot = snapshot_with_active(source)
    next_diagnosis = diagnose_job()
    resources = continuation(
        incoming_outcome_id=outcome.outcome_id,
        job=source,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(next_diagnosis)},
        continuation_resources=resources,
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "RUNNING"
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.target_status is CandidateStatus.REJECTED
    assert plan.candidate_mutation.reason == changed.recommendation
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.DIAGNOSE
    assert plan.next_job_spec.target_state_revision == snapshot.case.diagnosis_state.revision + 1
    assert plan.next_job_spec.previous_outcome_refs == [outcome.outcome_id]
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
