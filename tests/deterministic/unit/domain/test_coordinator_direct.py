from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError, AttachmentRequirementConstraints, CaseStatus, DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload, DiagnosisStateDelta, FieldUpdateAction,
    GenericResultStatus, InputRequirementConstraints, JobOutcome, JobStatus,
    OutcomeResultType, PendingRequirement, RequirementKind, RequirementStatus,
    SupplementPolicy, TriggerType, validate_outcome_for_job, validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator
from tests.deterministic.contracts._specialized_direct_support import (
    REPORT, TIME, direct_job, direct_outcome,
)

from ._builders import continuation, diagnosis_outcome, snapshot_with_active, trigger


def _plan(job, outcome):
    snapshot = snapshot_with_active(job)
    request = trigger(
        snapshot, trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(incoming_outcome_id=outcome.outcome_id, job=job),
        occurred_at=outcome.produced_at,
    )
    return DomainCoordinator().plan(snapshot, request)


@pytest.mark.parametrize("status", list(GenericResultStatus))
def test_direct_diagnosis_delivers_exact_skill_report_without_candidate_or_review(status) -> None:
    job = direct_job()
    outcome = direct_outcome(job, status)
    plan = _plan(job, outcome)
    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus(status.value)
    assert plan.generic_result_v2_draft.report_markdown == REPORT
    assert plan.generic_result_v2_draft.status is status
    assert plan.selected_skill_update.action is FieldUpdateAction.SET
    assert plan.selected_skill_update.value == job.skill_ref
    assert plan.accepted_candidate_proposal_key is None
    assert plan.candidate_mutation is None
    assert plan.final_result_target is None
    assert plan.unresolved_result_draft is None
    assert plan.accepted_evidence_proposal_keys == []
    assert plan.accepted_artifact_proposal_keys == []
    assert plan.next_job_spec is None
    assert plan.job_updates[0].target_status is JobStatus.SUCCEEDED
    assert not any(plan.accepted_state_delta.model_dump(mode="python").values())
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_direct_diagnosis_rejects_legacy_candidate_evidence_result() -> None:
    plan = _plan(direct_job(), diagnosis_outcome())
    assert isinstance(plan, ApplicationError)
    assert plan.code.value == "VALIDATION_ERROR"


@pytest.mark.parametrize("kind", [RequirementKind.INPUT, RequirementKind.ATTACHMENT])
def test_direct_preflight_still_requests_missing_input_or_attachment(kind) -> None:
    job = direct_job()
    requirement_id = "00000000-0000-4000-8000-000000000804"
    is_input = kind is RequirementKind.INPUT
    requirement = PendingRequirement(
        requirement_id=requirement_id, kind=kind,
        name="problem_time" if is_input else "log_archive",
        prompt="请补充问题时间。" if is_input else "请上传日志。", required=True,
        constraints=(InputRequirementConstraints(
            value_type="STRING", min_utf8_bytes=1, max_utf8_bytes=64,
            pattern=None, allowed_values=[],
        ) if is_input else AttachmentRequirementConstraints(
            allowed_content_types=["application/zip"], min_count=1, max_count=1,
        )),
        status=RequirementStatus.OPEN, requested_by_job_id=job.job_id,
        fulfilled_by_refs=[], supplement_policy=SupplementPolicy.MISSING_ONLY,
    )
    delta = DiagnosisStateDelta(
        problem_spec_patch=None, add_user_facts=[], proposed_facts=[], add_active_hypotheses=[],
        update_hypotheses=[], reject_hypotheses=[], add_open_questions=[], resolve_questions=[],
        add_pending_requirements=[requirement], fulfill_requirements=[], add_evidence_bindings=[],
    )
    completed = direct_outcome(job)
    outcome = JobOutcome.model_validate({
        **completed.model_dump(mode="python"),
        "result_type": OutcomeResultType.NEED_INPUT if is_input else OutcomeResultType.NEED_ATTACHMENT,
        "payload": DiagnosisOutcome(
            findings=[], state_delta=delta,
            requested_input=[requirement_id] if is_input else [],
            requested_attachments=[] if is_input else [requirement_id],
            candidate_conclusion_draft=None, recommended_next_step=requirement.prompt,
        ),
    })
    assert validate_outcome_for_job(job, outcome) is outcome
    plan = _plan(job, outcome)
    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is (
        CaseStatus.WAITING_INPUT if is_input else CaseStatus.WAITING_ATTACHMENT
    )
    assert plan.accepted_state_delta.add_pending_requirements == [requirement]
    assert plan.generic_result_v2_draft is None
    assert plan.candidate_mutation is None
    assert plan.next_job_spec is None
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
