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
    ExecutionFailure,
    JobStatus,
    JobType,
    METHOD_PUBLIC_REASON_TEXT_V2,
    MethodsTerminalProjectionV2,
    MethodsValidationReasonCode,
    OutcomeResultType,
    ReviewOutcomeTriggerPayload,
    TriggerType,
    method_terminal_result_ref_v2,
    validate_outcome_for_job,
    validate_methods_reviewer_terminal_v2,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    fail_method_state_v2,
    finalize_reviewer_consensus_v2,
    finalize_specialist_evaluation_v2,
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
)

from ._builders import (
    continuation,
    rebuild,
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
OTHER_CASE_ID = "00000000-0000-0000-0000-000000000083"
OTHER_JOB_ID = "00000000-0000-0000-0000-000000000084"
PRIVATE_REASON_SENTINEL = "PRIVATE_ROLE_REASON_MUST_NOT_BE_PUBLIC_7f3c9d"


@dataclass(frozen=True)
class _ReviewTerminalFlow:
    source: object
    handoff_snapshot: object
    handoff_outcome: object
    handoff_transition: object
    diagnosis_state: object
    review_job: object
    graph: object
    plan: object
    terminal_state: object
    terminal_result: object
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
                "supporting_event_refs": (
                    list(item.evidence_event_refs)
                    if verdict == "CONFIRMED"
                    else []
                ),
                "reason": f"private {role.lower()} reason {index}",
            }
            for index, (item, verdict) in enumerate(
                zip(plan.evaluations, verdicts, strict=True),
                start=1,
            )
        ],
        attempt="PRIMARY",
    )


def _terminal_result(state, plan, graph, *, terminal_job_id: str):
    return build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=terminal_job_id,
        limitations=("server-observed limitation",),
        reasons=(),
    )


def _with_recomputed_result_ref(result, **changes: object):
    mutated = result.model_copy(update=changes)
    result_ref = method_terminal_result_ref_v2(
        case_id=mutated.case_id,
        source_job_id=mutated.source_job_id,
        terminal_job_id=mutated.terminal_job_id,
        evaluation_id=mutated.evaluation_id,
        status=mutated.status,
        plan_ref=mutated.plan_ref,
        evidence_graph_ref=mutated.evidence_graph_ref,
        reason_code=mutated.reason_code,
        diagnostic_id=mutated.diagnostic_id,
        diagnostic_evaluation_ref=mutated.diagnostic_evaluation_ref,
        evaluations=mutated.evaluations,
        confirmed_evaluation_refs=mutated.confirmed_evaluation_refs,
        confirmed_method_ids=mutated.confirmed_method_ids,
        confirmed_event_refs=mutated.confirmed_event_refs,
        confirmed_hit_refs=mutated.confirmed_hit_refs,
        limitations=mutated.limitations,
        reasons=mutated.reasons,
    )
    return mutated.model_copy(update={"result_ref": result_ref})


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
        state=start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
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
    terminal_result = _terminal_result(
        terminal,
        plan,
        graph,
        terminal_job_id=review_job.job_id,
    )
    outcome = build_methods_reviewer_outcome_v2(
        review_job,
        outcome_id=REVIEW_OUTCOME_ID,
        terminal_state=terminal,
        terminal_result=terminal_result,
        plan=plan,
        evidence=graph,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert outcome.methods_terminal_projection is not None
    projection = outcome.methods_terminal_projection
    return _ReviewTerminalFlow(
        source=source,
        handoff_snapshot=handoff_snapshot,
        handoff_outcome=handoff_outcome,
        handoff_transition=handoff_transition,
        diagnosis_state=diagnosis_state,
        review_job=review_job,
        graph=graph,
        plan=plan,
        terminal_state=terminal,
        terminal_result=terminal_result,
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
    assert terminal.source_job_id == flow.source.job_id
    assert projection.source_job_id == job.job_id
    assert projection.case_id == job.case_id
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
    state = start_method_state_v2(
        case_id=source.case_id,
        source_job_id=source.job_id,
        evaluation_id=EVALUATION_ID,
        plan=plan,
    )
    if reason_code == "NO_MATCHING_METHOD_EVIDENCE":
        graph = scan_method_evidence_v2(
            skill=skill,
            target_logs=(_target_without_match(),),
        )
        plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
        state = start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        )
    elif reason_code == "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED":
        state = record_protocol_error_v2(
            state=state,
            role="SPECIALIST",
            reason=PRIVATE_REASON_SENTINEL,
        )
        state = record_protocol_error_v2(
            state=state,
            role="SPECIALIST",
            reason=PRIVATE_REASON_SENTINEL,
        )
    elif reason_code == "SPECIALIST_SEMANTIC_INVALID":
        state = record_semantic_invalid_v2(
            state=state,
            role="SPECIALIST",
            reason=PRIVATE_REASON_SENTINEL,
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    elif reason_code == "SPECIALIST_MODEL_EXECUTION_FAILED":
        state = record_model_execution_failure_v2(
            state=state,
            role="SPECIALIST",
            reason=PRIVATE_REASON_SENTINEL,
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    else:
        state = fail_method_state_v2(
            state=state,
            reason_code=reason_code,
            reason=PRIVATE_REASON_SENTINEL,
        )
    terminal_result = _terminal_result(
        state,
        plan,
        graph,
        terminal_job_id=source.job_id,
    )
    outcome = build_methods_specialist_terminal_outcome_v2(
        source,
        outcome_id=EARLY_OUTCOME_ID,
        terminal_state=state,
        terminal_result=terminal_result,
        plan=plan,
        evidence=graph,
        produced_at="2026-07-31T00:03:20.000Z",
    )
    assert outcome.methods_terminal_projection is not None
    projection = outcome.methods_terminal_projection
    return source, state, graph, plan, terminal_result, projection, outcome


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
    source, state, _, _, terminal_result, projection, outcome = _specialist_terminal(
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
    assert PRIVATE_REASON_SENTINEL not in state.model_dump_json()
    assert PRIVATE_REASON_SENTINEL not in terminal_result.model_dump_json()
    assert PRIVATE_REASON_SENTINEL not in projection.model_dump_json()
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
        assert outcome.error.message == METHOD_PUBLIC_REASON_TEXT_V2[reason_code]
        assert transition.case_failure_update is not None
        assert transition.case_failure_update.value is not None
        assert transition.case_failure_update.value.code is expected_error
        assert (
            transition.case_failure_update.value.message
            == METHOD_PUBLIC_REASON_TEXT_V2[reason_code]
        )
    else:
        assert outcome.result_type is OutcomeResultType.INCONCLUSIVE
        assert transition.case_failure_update is None

    target_state = apply_diagnosis_state_delta(
        snapshot.case.diagnosis_state,
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
    assert PRIVATE_REASON_SENTINEL not in view.model_dump_json()


def test_specialist_only_resolved_result_reaches_candidate_free_case(
    tmp_path,
) -> None:
    source, _, graph, plan = _flow_inputs(tmp_path)
    specialist = _role(plan, "SPECIALIST", ("CONFIRMED", "REJECTED"))
    state = finalize_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    terminal_result = _terminal_result(
        state,
        plan,
        graph,
        terminal_job_id=source.job_id,
    )
    outcome = build_methods_specialist_terminal_outcome_v2(
        source,
        outcome_id=EARLY_OUTCOME_ID,
        terminal_state=state,
        terminal_result=terminal_result,
        plan=plan,
        evidence=graph,
        produced_at="2026-07-31T00:03:20.000Z",
    )

    assert validate_outcome_for_job(source, outcome) is outcome
    assert outcome.methods_review_target is None
    assert outcome.methods_reviewer_result is None
    projection = outcome.methods_terminal_projection
    assert projection is not None and projection.status == "RESOLVED"
    assert projection.confirmed_evaluation_refs == (
        plan.evaluations[0].evaluation_ref,
    )
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
    assert transition.target_case_status is CaseStatus.RESOLVED
    assert transition.next_job_spec is None
    target_state = apply_diagnosis_state_delta(
        snapshot.case.diagnosis_state,
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
    assert case.methods_result == projection
    assert case.final_result is None
    assert case.diagnosis_state.candidate_conclusion is None


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
    source = inputs[0]
    _, _, _, diagnosis_state, job, graph, plan = _plan_and_review_job(inputs)
    specialist = _role(plan, "SPECIALIST", ("CONFIRMED", "REJECTED"))
    state = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    if reason_code == "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED":
        state = record_protocol_error_v2(
            state=state,
            role="REVIEWER",
            reason=PRIVATE_REASON_SENTINEL,
        )
        state = record_protocol_error_v2(
            state=state,
            role="REVIEWER",
            reason=PRIVATE_REASON_SENTINEL,
        )
    elif reason_code == "REVIEWER_SEMANTIC_INVALID":
        state = record_semantic_invalid_v2(
            state=state,
            role="REVIEWER",
            reason=PRIVATE_REASON_SENTINEL,
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    elif reason_code == "REVIEWER_MODEL_EXECUTION_FAILED":
        state = record_model_execution_failure_v2(
            state=state,
            role="REVIEWER",
            reason=PRIVATE_REASON_SENTINEL,
            evaluation_ref=plan.evaluations[0].evaluation_ref,
        )
    else:
        state = fail_method_state_v2(
            state=state,
            reason_code=reason_code,
            reason=PRIVATE_REASON_SENTINEL,
        )
    terminal_result = _terminal_result(
        state,
        plan,
        graph,
        terminal_job_id=job.job_id,
    )
    outcome = build_methods_reviewer_outcome_v2(
        job,
        outcome_id=REVIEW_OUTCOME_ID,
        terminal_state=state,
        terminal_result=terminal_result,
        plan=plan,
        evidence=graph,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert outcome.methods_terminal_projection is not None
    projection = outcome.methods_terminal_projection
    assert outcome.methods_reviewer_result is None
    assert validate_outcome_for_job(job, outcome) is outcome
    assert PRIVATE_REASON_SENTINEL not in state.model_dump_json()
    assert PRIVATE_REASON_SENTINEL not in terminal_result.model_dump_json()
    assert PRIVATE_REASON_SENTINEL not in projection.model_dump_json()
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
    assert PRIVATE_REASON_SENTINEL not in view.model_dump_json()


def test_unresolved_reason_must_belong_to_source_job_stage(tmp_path) -> None:
    source, _, _, _, _, projection, legal_outcome = _specialist_terminal(
        tmp_path,
        "NO_MATCHING_METHOD_EVIDENCE",
    )
    wrong_stage = projection.model_copy(
        update={"reason_code": "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED"}
    )
    outcome = legal_outcome.model_copy(
        update={"methods_terminal_projection": wrong_stage}
    )
    with pytest.raises(ValueError, match="source Job stage"):
        validate_outcome_for_job(source, outcome)


def test_specialist_terminal_rejects_a_reviewed_consensus_state(tmp_path) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    with pytest.raises(ValueError, match="specialized DIAGNOSE"):
        build_methods_specialist_terminal_outcome_v2(
            flow.source,
            outcome_id=EARLY_OUTCOME_ID,
            terminal_state=flow.terminal_state,
            terminal_result=flow.terminal_result,
            plan=flow.plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:20.000Z",
        )


@pytest.mark.parametrize(
    (
        "specialist_verdicts",
        "reviewer_verdicts",
        "mutated_verdict",
        "message",
    ),
    [
        (
            ("CONFIRMED", "REJECTED"),
            ("CONFIRMED", "REJECTED"),
            "REJECTED",
            "confirmed refs",
        ),
        (
            ("CONFIRMED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            "UNKNOWN",
            "must not contain UNKNOWN",
        ),
        (
            ("UNKNOWN", "REJECTED"),
            ("UNKNOWN", "REJECTED"),
            "REJECTED",
            "requires an UNKNOWN",
        ),
        (
            ("REJECTED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            "CONFIRMED",
            "all Reviewer verdicts REJECTED",
        ),
    ],
)
def test_reviewer_verdict_relation_is_identical_at_both_validation_entries(
    tmp_path,
    specialist_verdicts,
    reviewer_verdicts,
    mutated_verdict,
    message,
) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=specialist_verdicts,
        reviewer_verdicts=reviewer_verdicts,
    )
    reviewer = flow.outcome.methods_reviewer_result
    assert reviewer is not None
    mutated_item = reviewer.evaluations[0].model_copy(
        update={"verdict": mutated_verdict}
    )
    mutated_reviewer = reviewer.model_copy(
        update={"evaluations": (mutated_item, *reviewer.evaluations[1:])}
    )
    with pytest.raises(ValueError, match=message):
        validate_methods_reviewer_terminal_v2(
            mutated_reviewer,
            flow.projection,
            review_job_id=flow.review_job.job_id,
            reviewed_state_revision=flow.review_job.base_state_revision,
            expected_target=flow.review_job.methods_review_target,
        )

    value = flow.outcome.model_dump(mode="python")
    value["methods_reviewer_result"] = mutated_reviewer
    with pytest.raises(ValueError, match=message):
        type(flow.outcome).model_validate(value)

    bypassed = flow.outcome.model_copy(
        update={"methods_reviewer_result": mutated_reviewer}
    )
    with pytest.raises(ValueError, match=message):
        validate_outcome_for_job(flow.review_job, bypassed)


def test_reviewer_factory_binds_terminal_result_to_exact_production_state(
    tmp_path,
) -> None:
    first = _review_terminal(
        tmp_path / "first",
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    second = _review_terminal(
        tmp_path / "second",
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("REJECTED", "REJECTED"),
    )
    with pytest.raises(ValueError, match="production state|result_ref"):
        build_methods_reviewer_outcome_v2(
            first.review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=second.terminal_state,
            terminal_result=first.terminal_result,
            plan=first.plan,
            evidence=first.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_reviewer_factory_rejects_production_result_from_another_plan(
    tmp_path,
) -> None:
    flow = _review_terminal(
        tmp_path / "original",
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    _, skill, _, _ = _flow_inputs(tmp_path / "other")
    content = (
        b"API_COMPLETE request_id=req-other\n"
        b"UNRELATED_POSITIVE request_id=req-2\n"
    )
    other_graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            FrozenTargetLogV1(
                source_id="server",
                relative_path="logs/server.log",
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            ),
        ),
    )
    other_plan = build_method_evaluation_plan_v2(
        skill=skill,
        evidence=other_graph,
    )
    other_specialist = _role(
        other_plan,
        "SPECIALIST",
        ("CONFIRMED", "REJECTED"),
    )
    other_reviewer = _role(
        other_plan,
        "REVIEWER",
        ("CONFIRMED", "REJECTED"),
    )
    other_pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=flow.source.case_id,
            source_job_id=flow.source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=other_plan,
        ),
        evaluation=other_specialist,
    )
    other_terminal = finalize_reviewer_consensus_v2(
        state=other_pending,
        plan=other_plan,
        reviewer_evaluation=other_reviewer,
        consensus=resolve_method_consensus_v2(
            plan=other_plan,
            first=other_specialist,
            second=other_reviewer,
        ),
    )
    other_result = build_method_terminal_result_v2(
        state=other_terminal,
        plan=other_plan,
        evidence=other_graph,
        terminal_job_id=flow.review_job.job_id,
    )
    assert other_result.plan_ref != flow.terminal_result.plan_ref

    with pytest.raises(ValueError, match="production state and Plan"):
        build_methods_reviewer_outcome_v2(
            flow.review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=other_terminal,
            terminal_result=other_result,
            plan=flow.plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_server_factory_rejects_extra_free_form_terminal_reason(tmp_path) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    mutated_result = flow.terminal_result.model_copy(
        update={"reasons": (PRIVATE_REASON_SENTINEL,)}
    )
    with pytest.raises(ValueError, match="production state|result_ref"):
        build_methods_reviewer_outcome_v2(
            flow.review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=flow.terminal_state,
            terminal_result=mutated_result,
            plan=flow.plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


@pytest.mark.parametrize(
    ("field_name", "mutated_ref"),
    [
        ("confirmed_event_refs", "event-" + "f" * 64),
        ("confirmed_hit_refs", "hit-" + "f" * 64),
    ],
)
def test_factory_revalidates_terminal_event_and_hit_mapping(
    tmp_path,
    field_name: str,
    mutated_ref: str,
) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    mutated_result = flow.terminal_result.model_copy(
        update={field_name: (mutated_ref,)}
    )
    with pytest.raises(ValueError, match="resolved|result_ref"):
        build_methods_reviewer_outcome_v2(
            flow.review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=flow.terminal_state,
            terminal_result=mutated_result,
            plan=flow.plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_reviewer_factory_rejects_hit_from_unselected_event(tmp_path) -> None:
    source, skill, _, _ = _flow_inputs(tmp_path)
    content = (
        b"API_COMPLETE request_id=req-target\n"
        b"API_COMPLETE request_id=req-noise\n"
        b"UNRELATED_POSITIVE request_id=req-other\n"
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            FrozenTargetLogV1(
                source_id="server",
                relative_path="logs/server.log",
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            ),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    _, _, _, _, review_job, _, _ = _plan_and_review_job(
        (source, skill, graph, plan)
    )
    selected_event_ref, noise_event_ref = plan.evaluations[0].evidence_event_refs

    def selected_role(role: str):
        return evaluate_method_role_v2(
            role=role,
            plan=plan,
            response=[
                {
                    "evaluation_ref": item.evaluation_ref,
                    "verdict": "CONFIRMED" if index == 0 else "REJECTED",
                    "supporting_event_refs": (
                        [selected_event_ref] if index == 0 else []
                    ),
                    "reason": f"private {role.lower()} reason {index}",
                }
                for index, item in enumerate(plan.evaluations)
            ],
            attempt="PRIMARY",
        )

    specialist = selected_role("SPECIALIST")
    reviewer = selected_role("REVIEWER")
    pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    terminal_state = finalize_reviewer_consensus_v2(
        state=pending,
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=resolve_method_consensus_v2(
            plan=plan,
            first=specialist,
            second=reviewer,
        ),
    )
    terminal_result = _terminal_result(
        terminal_state,
        plan,
        graph,
        terminal_job_id=review_job.job_id,
    )
    noise_event = next(
        item for item in graph.events if item.event_ref == noise_event_ref
    )
    forged_evaluation = terminal_result.evaluations[0].model_copy(
        update={"evidence_hit_refs": noise_event.evidence_hit_refs}
    )
    forged_result = _with_recomputed_result_ref(
        terminal_result,
        evaluations=(forged_evaluation,),
        confirmed_hit_refs=noise_event.evidence_hit_refs,
    )

    with pytest.raises(ValueError, match="exact consensus evidence"):
        build_methods_reviewer_outcome_v2(
            review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=terminal_state,
            terminal_result=forged_result,
            plan=plan,
            evidence=graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_factory_revalidates_plan_model_copy(tmp_path) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    mutated_plan = flow.plan.model_copy(
        update={"evidence_graph_ref": "graph-" + "f" * 64}
    )

    with pytest.raises(ValueError, match="plan_ref"):
        build_methods_reviewer_outcome_v2(
            flow.review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=flow.terminal_state,
            terminal_result=flow.terminal_result,
            plan=mutated_plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_terminal_workflow_identity_rejects_cross_case_reuse(tmp_path) -> None:
    (
        source,
        state,
        graph,
        plan,
        terminal_result,
        projection,
        legal_outcome,
    ) = _specialist_terminal(
        tmp_path,
        "NO_MATCHING_METHOD_EVIDENCE",
    )
    foreign_case_job = rebuild(
        source,
        case_id=OTHER_CASE_ID,
    )
    with pytest.raises(ValueError, match="specialized DIAGNOSE"):
        build_methods_specialist_terminal_outcome_v2(
            foreign_case_job,
            outcome_id=EARLY_OUTCOME_ID,
            terminal_state=state,
            terminal_result=terminal_result,
            plan=plan,
            evidence=graph,
            produced_at="2026-07-31T00:03:20.000Z",
        )

    foreign_source_job = rebuild(source, job_id=OTHER_JOB_ID)
    with pytest.raises(ValueError, match="specialized DIAGNOSE"):
        build_methods_specialist_terminal_outcome_v2(
            foreign_source_job,
            outcome_id=EARLY_OUTCOME_ID,
            terminal_state=state,
            terminal_result=terminal_result,
            plan=plan,
            evidence=graph,
            produced_at="2026-07-31T00:03:20.000Z",
        )

    value = legal_outcome.model_dump(mode="python")
    value["methods_terminal_projection"] = projection.model_copy(
        update={"case_id": OTHER_CASE_ID}
    )
    with pytest.raises(ValueError, match="match its DIAGNOSE/REVIEW Outcome"):
        type(legal_outcome).model_validate(value)

    value = legal_outcome.model_dump(mode="python")
    value["methods_terminal_projection"] = projection.model_copy(
        update={"source_job_id": OTHER_JOB_ID}
    )
    with pytest.raises(ValueError, match="match its DIAGNOSE/REVIEW Outcome"):
        type(legal_outcome).model_validate(value)


def test_reviewer_terminal_result_cannot_rebind_to_another_review_job(
    tmp_path,
) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    other_review_job = rebuild(flow.review_job, job_id=OTHER_JOB_ID)
    with pytest.raises(ValueError, match="Candidate-free REVIEW Job"):
        build_methods_reviewer_outcome_v2(
            other_review_job,
            outcome_id=REVIEW_OUTCOME_ID,
            terminal_state=flow.terminal_state,
            terminal_result=flow.terminal_result,
            plan=flow.plan,
            evidence=flow.graph,
            produced_at="2026-07-31T00:03:30.000Z",
        )


def test_failure_reason_and_diagnostic_types_cannot_cross_contracts(tmp_path) -> None:
    _, _, _, _, _, _, outcome = _specialist_terminal(
        tmp_path,
        "RESOURCE_SNAPSHOT_DRIFT",
    )
    assert outcome.error is not None
    value = outcome.error.model_dump(mode="python")
    value["reason_code"] = "SPECIALIST_SEMANTIC_INVALID"
    with pytest.raises(ValueError, match="FAILED reason"):
        ExecutionFailure.model_validate(value)

    value = outcome.error.model_dump(mode="python")
    value["diagnostic_id"] = "00000000-0000-0000-0000-000000000099"
    with pytest.raises(ValueError, match=r"diag-\*"):
        ExecutionFailure.model_validate(value)

    value = outcome.error.model_dump(mode="python")
    value["reason_code"] = MethodsValidationReasonCode.VALIDATION_FAILED
    with pytest.raises(ValueError, match="UUID diagnostic_id"):
        ExecutionFailure.model_validate(value)


def test_methods_review_missing_terminal_projection_never_falls_back_to_candidate(
    tmp_path,
) -> None:
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=("CONFIRMED", "REJECTED"),
        reviewer_verdicts=("CONFIRMED", "REJECTED"),
    )
    snapshot = snapshot_with_active(
        flow.review_job,
        status=CaseStatus.REVIEWING,
        state=flow.diagnosis_state,
    )
    legal_request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=flow.outcome),
        continuation_resources=continuation(
            incoming_outcome_id=flow.outcome.outcome_id,
            job=flow.review_job,
        ),
        occurred_at=flow.outcome.produced_at,
    )
    without_projection = flow.outcome.model_copy(
        update={"methods_terminal_projection": None}
    )
    mutated_payload = legal_request.payload.model_copy(
        update={"job_outcome": without_projection}
    )
    request = legal_request.model_copy(
        update={"payload": mutated_payload}
    )
    result = DomainCoordinator().plan(snapshot, request)
    assert isinstance(result, ApplicationError)
    assert "terminal projection" in result.message
