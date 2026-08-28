from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from problem_locator.application.formalization import (
    apply_diagnosis_state_delta,
    build_methods_reviewer_outcome_v2,
    build_methods_specialist_terminal_outcome_v2,
)
from problem_locator.application.mutations import (
    apply_transition_plan_to_case,
    build_state_mutation,
)
from problem_locator.application.projection import project_case_components
from problem_locator.contracts import (
    ApplicationError,
    CaseStatus,
    DiagnosisOutcomeTriggerPayload,
    ErrorCode,
    JobStatus,
    JobType,
    MethodsTerminalProjectionV2,
    OutcomeResultType,
    ReviewOutcomeTriggerPayload,
    TriggerType,
    validate_outcome_for_job,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    fail_method_state_v2,
    finalize_reviewer_consensus_v2,
    record_model_execution_failure_v2,
    record_protocol_error_v2,
    record_semantic_invalid_v2,
    start_method_state_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
    evaluate_method_role_v2,
    resolve_method_consensus_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_outcome_v2 import (
    build_method_terminal_result_v2,
    project_method_terminal_result_v2,
)

from ._builders import (
    continuation,
    rebuild,
    review_outcome,
    snapshot_with_active,
    trigger,
)
from .test_methods_v2_blind_review_seam import (
    EVALUATION_ID,
    REVIEW_OUTCOME_ID,
    _flow_inputs,
    _plan_and_review_job,
)


EARLY_OUTCOME_ID = "00000000-0000-0000-0000-000000000081"


@dataclass(frozen=True)
class _ReviewTerminalFlow:
    source: object
    handoff_snapshot: object
    handoff_outcome: object
    handoff_transition: object
    diagnosis_state: object
    review_job: object
    plan: object
    terminal_state: object
    projection: MethodsTerminalProjectionV2
    outcome: object


def _role(plan, role: str, verdicts: tuple[str, ...]):
    return evaluate_method_role_v2(
        role=role,
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": verdict,
                "reason": f"private {role.lower()} reason {index}",
            }
            for index, (item, verdict) in enumerate(
                zip(plan.evaluations, verdicts, strict=True),
                start=1,
            )
        ],
        attempt="PRIMARY",
    )


def _terminal_projection(state, plan, graph):
    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        limitations=("server-observed limitation",),
        reasons=("server terminal summary",),
    )
    return project_method_terminal_result_v2(result)


def _review_terminal(
    tmp_path,
    *,
    specialist_verdicts: tuple[str, ...],
    reviewer_verdicts: tuple[str, ...],
):
    inputs = _flow_inputs(tmp_path)
    source = inputs[0]
    (
        handoff_snapshot,
        handoff_outcome,
        handoff_transition,
        diagnosis_state,
        review_job,
        graph,
        plan,
    ) = _plan_and_review_job(inputs)
    specialist = _role(plan, "SPECIALIST", specialist_verdicts)
    reviewer = _role(plan, "REVIEWER", reviewer_verdicts)
    pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(evaluation_id=EVALUATION_ID, plan=plan),
        evaluation=specialist,
    )
    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )
    terminal = finalize_reviewer_consensus_v2(
        state=pending,
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )
    projection = _terminal_projection(terminal, plan, graph)
    outcome = build_methods_reviewer_outcome_v2(
        review_job,
        outcome_id=REVIEW_OUTCOME_ID,
        evaluation=reviewer,
        terminal_projection=projection,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    return _ReviewTerminalFlow(
        source=source,
        handoff_snapshot=handoff_snapshot,
        handoff_outcome=handoff_outcome,
        handoff_transition=handoff_transition,
        diagnosis_state=diagnosis_state,
        review_job=review_job,
        plan=plan,
        terminal_state=terminal,
        projection=projection,
        outcome=outcome,
    )


@pytest.mark.parametrize(
    ("specialist_verdicts", "reviewer_verdicts", "status", "reason_code"),
    [
        (("CONFIRMED", "REJECTED"), ("CONFIRMED", "REJECTED"), "RESOLVED", None),
        (
            ("CONFIRMED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            "UNRESOLVED",
            "SPECIALIST_REVIEWER_DISAGREEMENT",
        ),
        (
            ("UNKNOWN", "REJECTED"),
            ("UNKNOWN", "REJECTED"),
            "UNRESOLVED",
            "INCOMPLETE_EVALUATION",
        ),
        (
            ("REJECTED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            "UNRESOLVED",
            "NO_CONFIRMED_METHOD",
        ),
    ],
)
def test_production_consensus_reaches_candidate_free_case_projection(
    tmp_path,
    specialist_verdicts,
    reviewer_verdicts,
    status,
    reason_code,
) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=specialist_verdicts,
        reviewer_verdicts=reviewer_verdicts,
    )
    diagnosis_state = flow.diagnosis_state
    job = flow.review_job
    terminal = flow.terminal_state
    projection = flow.projection
    outcome = flow.outcome
    assert terminal.status == status
    assert projection.status == status
    assert projection.reason_code == reason_code
    assert projection.schema_version == 2
    assert all("private" not in reason for reason in projection.reasons)
    assert "private" not in projection.model_dump_json()
    assert validate_outcome_for_job(job, outcome) is outcome

    snapshot = snapshot_with_active(
        job,
        status=CaseStatus.REVIEWING,
        state=diagnosis_state,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )
    transition = DomainCoordinator().plan(snapshot, request)
    assert not isinstance(transition, ApplicationError)
    assert validate_transition_plan_for_outcome(transition, outcome) is transition
    assert transition.target_case_status is CaseStatus(status)
    assert transition.target_case_status is not CaseStatus.PARTIALLY_RESOLVED
    assert transition.methods_terminal_projection == projection
    assert transition.candidate_mutation is None
    assert transition.accepted_candidate_proposal_key is None
    assert transition.final_result_target is None
    assert transition.unresolved_result_draft is None
    assert transition.next_job_spec is None

    target_state = apply_diagnosis_state_delta(
        diagnosis_state,
        transition.accepted_state_delta,
        evidence_ids_by_proposal_key={},
    )
    case = apply_transition_plan_to_case(
        snapshot.case,
        transition,
        target_state,
        created_job=None,
        processed_at=outcome.produced_at,
    )
    view = project_case_components(case, None, [])
    mutation = build_state_mutation(
        upsert_case=case,
        job_lifecycle_updates=transition.job_updates,
        insert_outcomes=[outcome],
    )
    assert mutation.upsert_case == case
    assert case.methods_result == projection
    assert case.final_result is None
    assert case.unresolved_result is None
    assert case.diagnosis_state.candidate_conclusion is None
    assert view.methods_result == projection
    assert view.final_result is None
    assert view.unresolved_result is None
    assert view.artifacts == []


def _target_without_match() -> FrozenTargetLogV1:
    content = b"there is no matching marker\n"
    return FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _specialist_terminal(tmp_path, reason_code: str):
    source, skill, graph, plan = _flow_inputs(tmp_path)
    state = start_method_state_v2(evaluation_id=EVALUATION_ID, plan=plan)
    if reason_code == "NO_MATCHING_METHOD_EVIDENCE":
        graph = scan_method_evidence_v2(
            skill=skill,
            target_logs=(_target_without_match(),),
        )
        plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
        state = start_method_state_v2(evaluation_id=EVALUATION_ID, plan=plan)
    elif reason_code == "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED":
        state = record_protocol_error_v2(
            state=state,
            role="SPECIALIST",
            reason="primary response has invalid structure",
        )
        state = record_protocol_error_v2(
            state=state,
            role="SPECIALIST",
            reason="repair response has invalid structure",
        )
    elif reason_code == "SPECIALIST_SEMANTIC_INVALID":
        state = record_semantic_invalid_v2(
            state=state,
            role="SPECIALIST",
            reason="semantic evaluation is invalid",
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    elif reason_code == "SPECIALIST_MODEL_EXECUTION_FAILED":
        state = record_model_execution_failure_v2(
            state=state,
            role="SPECIALIST",
            reason="model execution did not return a usable response",
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    else:
        state = fail_method_state_v2(
            state=state,
            reason_code=reason_code,
            reason=f"terminal infrastructure failure: {reason_code}",
        )
    projection = _terminal_projection(state, plan, graph)
    outcome = build_methods_specialist_terminal_outcome_v2(
        source,
        outcome_id=EARLY_OUTCOME_ID,
        terminal_projection=projection,
        produced_at="2026-07-31T00:03:20.000Z",
    )
    return source, state, projection, outcome


@pytest.mark.parametrize(
    "reason_code",
    [
        "NO_MATCHING_METHOD_EVIDENCE",
        "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
        "SPECIALIST_SEMANTIC_INVALID",
        "SPECIALIST_MODEL_EXECUTION_FAILED",
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    ],
)
def test_specialist_early_terminal_maps_status_job_and_failure(
    tmp_path,
    reason_code,
) -> None:
    source, state, projection, outcome = _specialist_terminal(
        tmp_path,
        reason_code,
    )
    assert validate_outcome_for_job(source, outcome) is outcome
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )
    transition = DomainCoordinator().plan(snapshot, request)
    assert not isinstance(transition, ApplicationError)
    assert transition.methods_terminal_projection == projection
    assert transition.candidate_mutation is None
    assert transition.next_job_spec is None
    assert transition.accepted_artifact_proposal_keys == []
    assert transition.accepted_evidence_proposal_keys == []
    expected_failed = state.status == "FAILED"
    assert transition.target_case_status is (
        CaseStatus.FAILED if expected_failed else CaseStatus.UNRESOLVED
    )
    assert transition.job_updates[0].target_status is (
        JobStatus.FAILED if expected_failed else JobStatus.SUCCEEDED
    )
    if expected_failed:
        expected_error = {
            "RESOURCE_SNAPSHOT_DRIFT": ErrorCode.ASSET_VERSION_UNAVAILABLE,
            "SERVER_INVARIANT_VIOLATION": ErrorCode.OUTCOME_INVALID,
            "AUDIT_ARCHIVE_FAILED": ErrorCode.EXECUTION_RECORD_FAILED,
        }[reason_code]
        assert outcome.result_type is OutcomeResultType.FAILED
        assert outcome.error is not None
        assert outcome.error.code is expected_error
        assert transition.case_failure_update is not None
        assert transition.case_failure_update.value is not None
        assert transition.case_failure_update.value.code is expected_error
    else:
        assert outcome.result_type is OutcomeResultType.INCONCLUSIVE
        assert transition.case_failure_update is None


@pytest.mark.parametrize(
    "reason_code",
    [
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED",
        "REVIEWER_SEMANTIC_INVALID",
        "REVIEWER_MODEL_EXECUTION_FAILED",
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    ],
)
def test_reviewer_early_terminal_requires_no_fabricated_reviewer_result(
    tmp_path,
    reason_code,
) -> None:
    inputs = _flow_inputs(tmp_path)
    _, _, _, diagnosis_state, job, graph, plan = _plan_and_review_job(inputs)
    specialist = _role(plan, "SPECIALIST", ("CONFIRMED", "REJECTED"))
    state = accept_specialist_evaluation_v2(
        state=start_method_state_v2(evaluation_id=EVALUATION_ID, plan=plan),
        evaluation=specialist,
    )
    if reason_code == "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED":
        state = record_protocol_error_v2(
            state=state,
            role="REVIEWER",
            reason="primary Reviewer response has invalid structure",
        )
        state = record_protocol_error_v2(
            state=state,
            role="REVIEWER",
            reason="repair Reviewer response has invalid structure",
        )
    elif reason_code == "REVIEWER_SEMANTIC_INVALID":
        state = record_semantic_invalid_v2(
            state=state,
            role="REVIEWER",
            reason="Reviewer semantic evaluation is invalid",
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    elif reason_code == "REVIEWER_MODEL_EXECUTION_FAILED":
        state = record_model_execution_failure_v2(
            state=state,
            role="REVIEWER",
            reason="Reviewer model execution failed",
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    else:
        state = fail_method_state_v2(
            state=state,
            reason_code=reason_code,
            reason=f"Reviewer terminal infrastructure failure: {reason_code}",
        )
    projection = _terminal_projection(state, plan, graph)
    outcome = build_methods_reviewer_outcome_v2(
        job,
        outcome_id=REVIEW_OUTCOME_ID,
        evaluation=None,
        terminal_projection=projection,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert outcome.methods_reviewer_result is None
    assert validate_outcome_for_job(job, outcome) is outcome
    snapshot = snapshot_with_active(
        job,
        status=CaseStatus.REVIEWING,
        state=diagnosis_state,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )
    transition = DomainCoordinator().plan(snapshot, request)
    assert not isinstance(transition, ApplicationError)
    assert transition.methods_terminal_projection == projection
    expected_failed = state.status == "FAILED"
    assert transition.target_case_status is (
        CaseStatus.FAILED if expected_failed else CaseStatus.UNRESOLVED
    )
    assert transition.job_updates[0].target_status is (
        JobStatus.FAILED if expected_failed else JobStatus.SUCCEEDED
    )
    target_state = apply_diagnosis_state_delta(
        diagnosis_state,
        transition.accepted_state_delta,
        evidence_ids_by_proposal_key={},
    )
    case = apply_transition_plan_to_case(
        snapshot.case,
        transition,
        target_state,
        created_job=None,
        processed_at=outcome.produced_at,
    )
    view = project_case_components(case, None, [])
    assert view.methods_result == projection
    assert view.final_result is None
    assert view.unresolved_result is None
    assert view.artifacts == []


def test_unresolved_reason_must_belong_to_source_job_stage(tmp_path) -> None:
    source, _, projection, _ = _specialist_terminal(
        tmp_path,
        "NO_MATCHING_METHOD_EVIDENCE",
    )
    value = projection.model_dump(mode="python")
    value["reason_code"] = "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED"
    wrong_stage = MethodsTerminalProjectionV2.model_validate(value)
    outcome = build_methods_specialist_terminal_outcome_v2(
        source,
        outcome_id=EARLY_OUTCOME_ID,
        terminal_projection=wrong_stage,
        produced_at="2026-07-31T00:03:20.000Z",
    )
    with pytest.raises(ValueError, match="source Job stage"):
        validate_outcome_for_job(source, outcome)


def test_specialist_early_terminal_cannot_resolve_without_reviewer(tmp_path) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    with pytest.raises(ValueError, match="early"):
        build_methods_specialist_terminal_outcome_v2(
            flow.source,
            outcome_id=EARLY_OUTCOME_ID,
            terminal_projection=flow.projection,
            produced_at="2026-07-31T00:03:20.000Z",
        )


def test_methods_review_missing_terminal_projection_never_falls_back_to_candidate(
    tmp_path,
) -> None:
    inputs = _flow_inputs(tmp_path)
    _, _, _, state, job, _, _ = _plan_and_review_job(inputs)
    legacy_baseline = review_outcome()
    assert legacy_baseline.decision_audit is not None
    legacy_audit = rebuild(
        legacy_baseline.decision_audit,
        job_id=job.job_id,
        case_id=job.case_id,
    )
    legacy = rebuild(
        legacy_baseline,
        job_id=job.job_id,
        case_id=job.case_id,
        base_state_revision=job.base_state_revision,
        decision_audit=legacy_audit,
    )
    snapshot = snapshot_with_active(
        job,
        status=CaseStatus.REVIEWING,
        state=state,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=legacy),
        continuation_resources=continuation(
            incoming_outcome_id=legacy.outcome_id,
            job=job,
        ),
        occurred_at=legacy.produced_at,
    )
    result = DomainCoordinator().plan(snapshot, request)
    assert isinstance(result, ApplicationError)
    assert "terminal projection" in result.message
