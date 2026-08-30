"""Reference-only mapping from Evidence V2 terminal state to public result."""

from __future__ import annotations

from collections.abc import Sequence

from problem_locator.contracts import (
    MethodConfirmedEvaluationV2,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodStateV2,
    MethodTerminalResultV2,
    method_terminal_result_ref_v2,
    project_method_terminal_result_v2,
    validate_method_terminal_result_v2,
)

from .methods_evidence_v2 import validate_method_evaluation_plan_v2


def _text(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of strings")
    frozen = tuple(values)
    if any(not isinstance(item, str) or not item or item.isspace() for item in frozen):
        raise ValueError(f"{label} must contain non-blank strings")
    return tuple(dict.fromkeys(frozen))


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def build_method_terminal_result_v2(
    *,
    state: MethodStateV2,
    plan: MethodEvaluationPlanV2,
    evidence: MethodEvidenceGraphV2,
    terminal_job_id: str,
    limitations: Sequence[str] = (),
    reasons: Sequence[str] = (),
) -> MethodTerminalResultV2:
    """Map terminal business state using only evaluation, event, and hit refs."""

    if not isinstance(state, MethodStateV2):
        raise TypeError("state must be MethodStateV2")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    if not isinstance(evidence, MethodEvidenceGraphV2):
        raise TypeError("evidence must be MethodEvidenceGraphV2")
    if state.status not in {"RESOLVED", "UNRESOLVED", "FAILED"}:
        raise ValueError("terminal result requires RESOLVED, UNRESOLVED, or FAILED state")
    if state.plan_ref != plan.plan_ref:
        raise ValueError("state differs from the terminal evaluation plan")
    planned_refs = tuple(item.evaluation_ref for item in plan.evaluations)
    if state.evaluation_refs != planned_refs:
        raise ValueError("state evaluation identities differ from the plan")
    validate_method_evaluation_plan_v2(evidence=evidence, plan=plan)

    known_event_refs = {item.event_ref for item in evidence.events}
    known_hit_refs = {item.hit_ref for item in evidence.hits}
    event_by_ref = {item.event_ref: item for item in evidence.events}
    plan_by_ref = {item.evaluation_ref: item for item in plan.evaluations}
    for evaluation_ref in planned_refs:
        planned = plan_by_ref[evaluation_ref]
        if any(item not in known_event_refs for item in planned.evidence_event_refs):
            raise ValueError("plan evaluation references an unknown evidence event")
        if any(item not in known_hit_refs for item in planned.evidence_hit_refs):
            raise ValueError("plan evaluation references an unknown evidence hit")

    evaluations: tuple[MethodConfirmedEvaluationV2, ...] = ()
    confirmed_evaluation_refs: tuple[str, ...] = ()
    confirmed_method_ids: tuple[str, ...] = ()
    confirmed_event_refs: tuple[str, ...] = ()
    confirmed_hit_refs: tuple[str, ...] = ()
    if state.status == "RESOLVED":
        if state.consensus is None or state.consensus.status != "RESOLVED":
            raise ValueError("resolved result requires resolved consensus")
        confirmed_evaluation_refs = state.consensus.confirmed_evaluation_refs
        confirmed_method_ids = state.consensus.confirmed_method_ids
        confirmed_items = tuple(
            plan_by_ref.get(evaluation_ref)
            for evaluation_ref in confirmed_evaluation_refs
        )
        if any(item is None for item in confirmed_items):
            raise ValueError("consensus confirms an unknown evaluation")
        actual_method_ids = tuple(
            item.method_id for item in confirmed_items if item is not None
        )
        if actual_method_ids != confirmed_method_ids:
            raise ValueError("consensus method identities differ from the plan")
        specialist = state.specialist_evaluation
        reviewer = state.reviewer_evaluation
        if specialist is None or reviewer is None:
            raise ValueError("resolved result requires both role evaluations")
        specialist_by_ref = {
            item.evaluation_ref: item for item in specialist.evaluations
        }
        reviewer_by_ref = {
            item.evaluation_ref: item for item in reviewer.evaluations
        }
        selected_by_ref = {
            evaluation_ref: specialist_by_ref[evaluation_ref].supporting_event_refs
            for evaluation_ref in confirmed_evaluation_refs
        }
        if any(
            reviewer_by_ref[evaluation_ref].supporting_event_refs
            != selected_by_ref[evaluation_ref]
            for evaluation_ref in confirmed_evaluation_refs
        ):
            raise ValueError("resolved role evaluations select different evidence events")
        flattened_selected_event_refs = _unique(
            tuple(
                event_ref
                for evaluation_ref in confirmed_evaluation_refs
                for event_ref in selected_by_ref[evaluation_ref]
            )
        )
        if flattened_selected_event_refs != state.consensus.confirmed_event_refs:
            raise ValueError("consensus event refs differ from selected evidence events")
        for item in confirmed_items:
            if item is None:
                continue
            selected = selected_by_ref[item.evaluation_ref]
            if any(event_ref not in item.evidence_event_refs for event_ref in selected):
                raise ValueError(
                    "selected evidence event lies outside its planned evaluation"
                )
            if any(
                event_by_ref[event_ref].method_id != item.method_id
                for event_ref in selected
            ):
                raise ValueError("selected evidence event belongs to another method")
        evaluations = tuple(
            MethodConfirmedEvaluationV2(
                evaluation_ref=item.evaluation_ref,
                method_id=item.method_id,
                evidence_event_refs=selected_by_ref[item.evaluation_ref],
                evidence_hit_refs=_unique(
                    tuple(
                        hit_ref
                        for event_ref in selected_by_ref[item.evaluation_ref]
                        for hit_ref in event_by_ref[event_ref].evidence_hit_refs
                    )
                ),
                verdict="CONFIRMED",
            )
            for item in confirmed_items
            if item is not None
        )
        confirmed_event_refs = flattened_selected_event_refs
        confirmed_hit_refs = _unique(
            tuple(
                ref
                for item in evaluations
                for ref in item.evidence_hit_refs
            )
        )

    if state.diagnostic_id is None:
        raise ValueError("terminal state must expose diagnostic_id")
    frozen_limitations = _text(limitations, label="limitations")
    frozen_reasons = _unique(
        (*state.reasons, *_text(reasons, label="reasons"))
    )
    result_ref = method_terminal_result_ref_v2(
        case_id=state.case_id,
        source_job_id=state.source_job_id,
        terminal_job_id=terminal_job_id,
        evaluation_id=state.evaluation_id,
        status=state.status,
        plan_ref=plan.plan_ref,
        evidence_graph_ref=evidence.graph_ref,
        reason_code=state.reason_code,
        diagnostic_id=state.diagnostic_id,
        diagnostic_evaluation_ref=state.diagnostic_evaluation_ref,
        evaluations=evaluations,
        confirmed_evaluation_refs=confirmed_evaluation_refs,
        confirmed_method_ids=confirmed_method_ids,
        confirmed_event_refs=confirmed_event_refs,
        confirmed_hit_refs=confirmed_hit_refs,
        limitations=frozen_limitations,
        reasons=frozen_reasons,
    )
    result = MethodTerminalResultV2(
        result_ref=result_ref,
        case_id=state.case_id,
        source_job_id=state.source_job_id,
        terminal_job_id=terminal_job_id,
        evaluation_id=state.evaluation_id,
        status=state.status,
        plan_ref=plan.plan_ref,
        evidence_graph_ref=evidence.graph_ref,
        reason_code=state.reason_code,
        diagnostic_id=state.diagnostic_id,
        diagnostic_evaluation_ref=state.diagnostic_evaluation_ref,
        evaluations=evaluations,
        confirmed_evaluation_refs=confirmed_evaluation_refs,
        confirmed_method_ids=confirmed_method_ids,
        confirmed_event_refs=confirmed_event_refs,
        confirmed_hit_refs=confirmed_hit_refs,
        limitations=frozen_limitations,
        reasons=frozen_reasons,
    )
    return validate_method_terminal_result_v2(
        state,
        result,
        plan,
        evidence=evidence,
    )


__all__ = [
    "build_method_terminal_result_v2",
    "project_method_terminal_result_v2",
]
