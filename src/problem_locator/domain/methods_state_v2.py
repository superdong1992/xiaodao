"""Deterministic business state machine for Evidence V2 role evaluation."""

from __future__ import annotations

from problem_locator.contracts import (
    FAILED_METHOD_REASON_CODES_V2,
    METHOD_PUBLIC_REASON_TEXT_V2,
    MethodConsensusV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRefV2,
    MethodEvaluationRoleV2,
    MethodRoleEvaluationV2,
    MethodStateReasonCodeV2,
    MethodStateV2,
    method_diagnostic_id_v2,
    method_state_ref_v2,
)


def _nonblank(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value.isspace():
        raise ValueError(f"{label} must be non-blank text")
    return value


def _make_state(
    *,
    case_id: str,
    source_job_id: str,
    evaluation_id: str,
    plan_ref: str,
    evaluation_refs: tuple[str, ...],
    status: str,
    current_role: str | None,
    specialist_protocol_failures: int,
    reviewer_protocol_failures: int,
    specialist_evaluation: MethodRoleEvaluationV2 | None,
    reviewer_evaluation: MethodRoleEvaluationV2 | None,
    consensus: MethodConsensusV2 | None,
    reason_code: str | None,
    diagnostic_id: str | None,
    diagnostic_evaluation_ref: str | None,
    reasons: tuple[str, ...],
) -> MethodStateV2:
    state_ref = method_state_ref_v2(
        case_id=case_id,
        source_job_id=source_job_id,
        evaluation_id=evaluation_id,
        plan_ref=plan_ref,
        evaluation_refs=evaluation_refs,
        status=status,
        current_role=current_role,
        specialist_protocol_failures=specialist_protocol_failures,
        reviewer_protocol_failures=reviewer_protocol_failures,
        specialist_evaluation=specialist_evaluation,
        reviewer_evaluation=reviewer_evaluation,
        consensus=consensus,
        reason_code=reason_code,
        diagnostic_id=diagnostic_id,
        diagnostic_evaluation_ref=diagnostic_evaluation_ref,
        reasons=reasons,
    )
    return MethodStateV2(
        state_ref=state_ref,
        case_id=case_id,
        source_job_id=source_job_id,
        evaluation_id=evaluation_id,
        plan_ref=plan_ref,
        evaluation_refs=evaluation_refs,
        status=status,
        current_role=current_role,
        specialist_protocol_failures=specialist_protocol_failures,
        reviewer_protocol_failures=reviewer_protocol_failures,
        specialist_evaluation=specialist_evaluation,
        reviewer_evaluation=reviewer_evaluation,
        consensus=consensus,
        reason_code=reason_code,
        diagnostic_id=diagnostic_id,
        diagnostic_evaluation_ref=diagnostic_evaluation_ref,
        reasons=reasons,
    )


def _replace(state: MethodStateV2, **changes: object) -> MethodStateV2:
    if not isinstance(state, MethodStateV2):
        raise TypeError("state must be MethodStateV2")
    values = {
        "case_id": state.case_id,
        "source_job_id": state.source_job_id,
        "evaluation_id": state.evaluation_id,
        "plan_ref": state.plan_ref,
        "evaluation_refs": state.evaluation_refs,
        "status": state.status,
        "current_role": state.current_role,
        "specialist_protocol_failures": state.specialist_protocol_failures,
        "reviewer_protocol_failures": state.reviewer_protocol_failures,
        "specialist_evaluation": state.specialist_evaluation,
        "reviewer_evaluation": state.reviewer_evaluation,
        "consensus": state.consensus,
        "reason_code": state.reason_code,
        "diagnostic_id": state.diagnostic_id,
        "diagnostic_evaluation_ref": state.diagnostic_evaluation_ref,
        "reasons": state.reasons,
    }
    values.update(changes)
    return _make_state(**values)  # type: ignore[arg-type]


def _diagnostic(
    state: MethodStateV2,
    *,
    status: str,
    reason_code: str | None,
    evaluation_ref: str | None,
) -> str:
    return method_diagnostic_id_v2(
        case_id=state.case_id,
        source_job_id=state.source_job_id,
        evaluation_id=state.evaluation_id,
        plan_ref=state.plan_ref,
        status=status,
        reason_code=reason_code,
        evaluation_ref=evaluation_ref,
    )


def _pending_role(state: MethodStateV2, role: MethodEvaluationRoleV2) -> None:
    expected_status = f"{role}_PENDING"
    if state.status != expected_status or state.current_role != role:
        raise ValueError("state is not pending for the requested role")


def _evaluation_ref(
    state: MethodStateV2,
    evaluation_ref: MethodEvaluationRefV2 | None,
) -> str | None:
    if evaluation_ref is not None and evaluation_ref not in state.evaluation_refs:
        raise ValueError("evaluation_ref does not belong to the state plan")
    return evaluation_ref


def _validate_role_evaluation(
    state: MethodStateV2,
    evaluation: MethodRoleEvaluationV2,
    *,
    role: MethodEvaluationRoleV2,
) -> None:
    if not isinstance(evaluation, MethodRoleEvaluationV2):
        raise TypeError("evaluation must be MethodRoleEvaluationV2")
    if evaluation.role != role or evaluation.plan_ref != state.plan_ref:
        raise ValueError("role evaluation differs from the pending state")
    if tuple(item.evaluation_ref for item in evaluation.evaluations) != state.evaluation_refs:
        raise ValueError("role evaluation does not exactly cover the state plan")


def _validate_repair_attempt(
    state: MethodStateV2,
    evaluation: MethodRoleEvaluationV2,
    *,
    role: MethodEvaluationRoleV2,
) -> None:
    failures = getattr(state, f"{role.lower()}_protocol_failures")
    expected_repair = failures == 1
    if evaluation.repair_used != expected_repair:
        expected = "repair" if expected_repair else "primary"
        raise ValueError(f"{role} state expects a {expected} evaluation attempt")


def start_method_state_v2(
    *,
    case_id: str,
    source_job_id: str,
    evaluation_id: str,
    plan: MethodEvaluationPlanV2,
) -> MethodStateV2:
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    evaluation_refs = tuple(item.evaluation_ref for item in plan.evaluations)
    if evaluation_refs:
        return _make_state(
            case_id=case_id,
            source_job_id=source_job_id,
            evaluation_id=evaluation_id,
            plan_ref=plan.plan_ref,
            evaluation_refs=evaluation_refs,
            status="SPECIALIST_PENDING",
            current_role="SPECIALIST",
            specialist_protocol_failures=0,
            reviewer_protocol_failures=0,
            specialist_evaluation=None,
            reviewer_evaluation=None,
            consensus=None,
            reason_code=None,
            diagnostic_id=None,
            diagnostic_evaluation_ref=None,
            reasons=(),
        )
    reason_code = "NO_MATCHING_METHOD_EVIDENCE"
    return _make_state(
        case_id=case_id,
        source_job_id=source_job_id,
        evaluation_id=evaluation_id,
        plan_ref=plan.plan_ref,
        evaluation_refs=(),
        status="UNRESOLVED",
        current_role=None,
        specialist_protocol_failures=0,
        reviewer_protocol_failures=0,
        specialist_evaluation=None,
        reviewer_evaluation=None,
        consensus=None,
        reason_code=reason_code,
        diagnostic_id=method_diagnostic_id_v2(
            case_id=case_id,
            source_job_id=source_job_id,
            evaluation_id=evaluation_id,
            plan_ref=plan.plan_ref,
            status="UNRESOLVED",
            reason_code=reason_code,
            evaluation_ref=None,
        ),
        diagnostic_evaluation_ref=None,
        reasons=(METHOD_PUBLIC_REASON_TEXT_V2[reason_code],),
    )


def accept_specialist_evaluation_v2(
    *,
    state: MethodStateV2,
    evaluation: MethodRoleEvaluationV2,
) -> MethodStateV2:
    _pending_role(state, "SPECIALIST")
    _validate_role_evaluation(state, evaluation, role="SPECIALIST")
    _validate_repair_attempt(state, evaluation, role="SPECIALIST")
    return _replace(
        state,
        status="REVIEWER_PENDING",
        current_role="REVIEWER",
        specialist_evaluation=evaluation,
    )


def record_protocol_error_v2(
    *,
    state: MethodStateV2,
    role: MethodEvaluationRoleV2,
    reason: str,
    evaluation_ref: MethodEvaluationRefV2 | None = None,
) -> MethodStateV2:
    _pending_role(state, role)
    _nonblank(reason, label="protocol error reason")
    bound_ref = _evaluation_ref(state, evaluation_ref)
    field = (
        "specialist_protocol_failures"
        if role == "SPECIALIST"
        else "reviewer_protocol_failures"
    )
    failures = getattr(state, field)
    if failures == 0:
        return _replace(state, **{field: 1})
    reason_code = f"{role}_PROTOCOL_REPAIR_EXHAUSTED"
    return _replace(
        state,
        **{
            field: 2,
            "status": "UNRESOLVED",
            "current_role": None,
            "reason_code": reason_code,
            "diagnostic_id": _diagnostic(
                state,
                status="UNRESOLVED",
                reason_code=reason_code,
                evaluation_ref=bound_ref,
            ),
            "diagnostic_evaluation_ref": bound_ref,
            "reasons": (METHOD_PUBLIC_REASON_TEXT_V2[reason_code],),
        },
    )


def _unresolve_role_failure(
    *,
    state: MethodStateV2,
    role: MethodEvaluationRoleV2,
    reason_code: MethodStateReasonCodeV2,
    reason: str,
    evaluation_ref: MethodEvaluationRefV2 | None,
) -> MethodStateV2:
    _pending_role(state, role)
    _nonblank(reason, label="role failure reason")
    bound_ref = _evaluation_ref(state, evaluation_ref)
    return _replace(
        state,
        status="UNRESOLVED",
        current_role=None,
        reason_code=reason_code,
        diagnostic_id=_diagnostic(
            state,
            status="UNRESOLVED",
            reason_code=reason_code,
            evaluation_ref=bound_ref,
        ),
        diagnostic_evaluation_ref=bound_ref,
        reasons=(METHOD_PUBLIC_REASON_TEXT_V2[reason_code],),
    )


def record_semantic_invalid_v2(
    *,
    state: MethodStateV2,
    role: MethodEvaluationRoleV2,
    reason: str,
    evaluation_ref: MethodEvaluationRefV2 | None = None,
) -> MethodStateV2:
    return _unresolve_role_failure(
        state=state,
        role=role,
        reason_code=f"{role}_SEMANTIC_INVALID",  # type: ignore[arg-type]
        reason=reason,
        evaluation_ref=evaluation_ref,
    )


def record_model_execution_failure_v2(
    *,
    state: MethodStateV2,
    role: MethodEvaluationRoleV2,
    reason: str,
    evaluation_ref: MethodEvaluationRefV2 | None = None,
) -> MethodStateV2:
    return _unresolve_role_failure(
        state=state,
        role=role,
        reason_code=f"{role}_MODEL_EXECUTION_FAILED",  # type: ignore[arg-type]
        reason=reason,
        evaluation_ref=evaluation_ref,
    )


def _consensus_reason(
    specialist: MethodRoleEvaluationV2,
    reviewer: MethodRoleEvaluationV2,
) -> tuple[str, str | None]:
    pairs = tuple(
        zip(specialist.evaluations, reviewer.evaluations, strict=True)
    )
    unknown = next(
        (
            second.evaluation_ref
            for first, second in pairs
            if second.verdict == "UNKNOWN"
        ),
        None,
    )
    if unknown is not None:
        return "INCOMPLETE_EVALUATION", unknown
    disagreement = next(
        (
            first.evaluation_ref
            for first, second in pairs
            if (
                first.verdict != second.verdict
                or first.supporting_event_refs != second.supporting_event_refs
            )
        ),
        None,
    )
    if disagreement is not None:
        return "SPECIALIST_REVIEWER_DISAGREEMENT", disagreement
    return "NO_CONFIRMED_METHOD", (
        specialist.evaluations[0].evaluation_ref
        if specialist.evaluations
        else None
    )


def _validate_consensus(
    *,
    plan: MethodEvaluationPlanV2,
    specialist: MethodRoleEvaluationV2,
    reviewer: MethodRoleEvaluationV2,
    consensus: MethodConsensusV2,
) -> None:
    if not isinstance(consensus, MethodConsensusV2) or consensus.plan_ref != plan.plan_ref:
        raise ValueError("consensus differs from the pending plan")
    specialist_blind = tuple(
        (item.evaluation_ref, item.verdict, item.supporting_event_refs)
        for item in specialist.evaluations
    )
    reviewer_blind = tuple(
        (item.evaluation_ref, item.verdict, item.supporting_event_refs)
        for item in reviewer.evaluations
    )
    verdicts = tuple(item.verdict for item in specialist.evaluations)
    resolved = (
        specialist_blind == reviewer_blind
        and "UNKNOWN" not in verdicts
        and "CONFIRMED" in verdicts
    )
    expected_status = "RESOLVED" if resolved else "UNRESOLVED"
    expected_refs = (
        tuple(
            item.evaluation_ref
            for item in specialist.evaluations
            if item.verdict == "CONFIRMED"
        )
        if resolved
        else ()
    )
    method_by_ref = {
        item.evaluation_ref: item.method_id for item in plan.evaluations
    }
    expected_methods = tuple(method_by_ref[item] for item in expected_refs)
    expected_event_refs = (
        tuple(
            event_ref
            for item in specialist.evaluations
            if item.verdict == "CONFIRMED"
            for event_ref in item.supporting_event_refs
        )
        if resolved
        else ()
    )
    if (
        consensus.status != expected_status
        or consensus.confirmed_evaluation_refs != expected_refs
        or consensus.confirmed_method_ids != expected_methods
        or consensus.confirmed_event_refs != expected_event_refs
    ):
        raise ValueError("consensus differs from the two role evaluations")


def finalize_reviewer_consensus_v2(
    *,
    state: MethodStateV2,
    plan: MethodEvaluationPlanV2,
    reviewer_evaluation: MethodRoleEvaluationV2,
    consensus: MethodConsensusV2,
) -> MethodStateV2:
    _pending_role(state, "REVIEWER")
    if not isinstance(plan, MethodEvaluationPlanV2) or plan.plan_ref != state.plan_ref:
        raise ValueError("plan differs from the reviewer-pending state")
    if tuple(item.evaluation_ref for item in plan.evaluations) != state.evaluation_refs:
        raise ValueError("plan evaluation identities differ from the pending state")
    _validate_role_evaluation(state, reviewer_evaluation, role="REVIEWER")
    _validate_repair_attempt(state, reviewer_evaluation, role="REVIEWER")
    if state.specialist_evaluation is None:
        raise ValueError("reviewer consensus requires specialist evaluation")
    _validate_consensus(
        plan=plan,
        specialist=state.specialist_evaluation,
        reviewer=reviewer_evaluation,
        consensus=consensus,
    )
    if consensus.status == "RESOLVED":
        return _replace(
            state,
            status="RESOLVED",
            current_role=None,
            reviewer_evaluation=reviewer_evaluation,
            consensus=consensus,
            reason_code=None,
            diagnostic_id=_diagnostic(
                state,
                status="RESOLVED",
                reason_code=None,
                evaluation_ref=None,
            ),
            diagnostic_evaluation_ref=None,
            reasons=(),
        )
    reason_code, evaluation_ref = _consensus_reason(
        state.specialist_evaluation,
        reviewer_evaluation,
    )
    return _replace(
        state,
        status="UNRESOLVED",
        current_role=None,
        reviewer_evaluation=reviewer_evaluation,
        consensus=consensus,
        reason_code=reason_code,
        diagnostic_id=_diagnostic(
            state,
            status="UNRESOLVED",
            reason_code=reason_code,
            evaluation_ref=evaluation_ref,
        ),
        diagnostic_evaluation_ref=evaluation_ref,
        reasons=(METHOD_PUBLIC_REASON_TEXT_V2[reason_code],),
    )


def fail_method_state_v2(
    *,
    state: MethodStateV2,
    reason_code: MethodStateReasonCodeV2,
    reason: str,
    evaluation_ref: MethodEvaluationRefV2 | None = None,
) -> MethodStateV2:
    if reason_code not in FAILED_METHOD_REASON_CODES_V2:
        raise ValueError("only resource drift, server invariant, or archive failure may fail")
    _nonblank(reason, label="failure reason")
    bound_ref = _evaluation_ref(state, evaluation_ref)
    return _replace(
        state,
        status="FAILED",
        current_role=None,
        reason_code=reason_code,
        diagnostic_id=_diagnostic(
            state,
            status="FAILED",
            reason_code=reason_code,
            evaluation_ref=bound_ref,
        ),
        diagnostic_evaluation_ref=bound_ref,
        reasons=(METHOD_PUBLIC_REASON_TEXT_V2[reason_code],),
    )


def interrupt_method_state_v2(*, state: MethodStateV2) -> MethodStateV2:
    if state.status not in {"SPECIALIST_PENDING", "REVIEWER_PENDING"}:
        raise ValueError("only a pending role may be interrupted")
    return _replace(state, status="INTERRUPTED")


def resume_method_state_v2(
    *,
    state: MethodStateV2,
    source_job_id: str | None = None,
) -> MethodStateV2:
    if state.status != "INTERRUPTED" or state.current_role is None:
        raise ValueError("only an interrupted role may resume")
    return _replace(
        state,
        source_job_id=(
            state.source_job_id if source_job_id is None else source_job_id
        ),
        status=f"{state.current_role}_PENDING",
    )


__all__ = [
    "accept_specialist_evaluation_v2",
    "fail_method_state_v2",
    "finalize_reviewer_consensus_v2",
    "interrupt_method_state_v2",
    "record_model_execution_failure_v2",
    "record_protocol_error_v2",
    "record_semantic_invalid_v2",
    "resume_method_state_v2",
    "start_method_state_v2",
]
