from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    CandidateMutationAction,
    CandidateStatus,
    ReviewAssessment,
    ReviewOutcomeTriggerPayload,
    ReviewVerdict,
    TriggerType,
    UnresolvedReasonCode,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    rebuild,
    review_job,
    review_outcome,
    snapshot_with_active,
    trigger,
    unresolved_user_result_proposal,
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
def test_non_pass_review_rejects_candidate_and_terminates_unresolved(
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
    outcome = rebuild(
        base,
        payload=changed,
        proposed_artifacts=[unresolved_user_result_proposal(source)],
    )
    snapshot = snapshot_with_active(source)
    resources = continuation(
        incoming_outcome_id=outcome.outcome_id,
        job=source,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=resources,
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "UNRESOLVED"
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.target_status is CandidateStatus.REJECTED
    assert plan.candidate_mutation.reason == changed.recommendation
    assert plan.next_job_spec is None
    assert plan.unresolved_result_draft is not None
    assert plan.unresolved_result_draft.user_result_proposal_key == "user_result"
    assert plan.accepted_artifact_proposal_keys == ["user_result"]
    expected_reason = (
        UnresolvedReasonCode.INVALID_NEED_MORE_REQUEST
        if verdict is ReviewVerdict.NEED_MORE_EVIDENCE
        else UnresolvedReasonCode.SEMANTIC_REVIEW_REJECTED
    )
    assert plan.unresolved_result_draft.reason_code is expected_reason
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
