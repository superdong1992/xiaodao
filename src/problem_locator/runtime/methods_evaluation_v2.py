"""Strict model-response validation, one repair, and blind Methods consensus."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ValidationError

from problem_locator.contracts import (
    MethodConsensusV2,
    MethodEvaluationOutputItemV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodRoleEvaluationV2,
)


_OUTPUT_FIELDS = frozenset(
    {"evaluation_ref", "verdict", "supporting_event_refs", "reason"}
)


class MethodEvaluationResponseError(ValueError):
    """Raised when a model response violates the exact evaluation plan shape."""

    def __init__(
        self,
        message: str,
        *,
        raw_response_bytes: bytes | None = None,
    ) -> None:
        self.raw_response_bytes = raw_response_bytes
        super().__init__(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field is forbidden: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _decoded_response(response: object) -> object:
    if isinstance(response, bytes):
        try:
            response = response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MethodEvaluationResponseError(
                "model evaluation response must be UTF-8 JSON"
            ) from exc
    if isinstance(response, str):
        try:
            return json.loads(
                response,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise MethodEvaluationResponseError(
                "model evaluation response must contain one JSON array"
            ) from exc
    return response


def parse_method_evaluation_response_v2(
    *,
    plan: MethodEvaluationPlanV2,
    response: object,
) -> tuple[MethodEvaluationOutputItemV2, ...]:
    """Parse the exact root-array response and bind it to plan order."""

    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    decoded = _decoded_response(response)
    if not isinstance(decoded, list):
        raise MethodEvaluationResponseError("model evaluation response root must be an array")
    if len(decoded) != len(plan.evaluations):
        raise MethodEvaluationResponseError(
            "model evaluation response must exactly cover every planned evaluation"
        )

    parsed: list[MethodEvaluationOutputItemV2] = []
    for index, (raw_item, planned) in enumerate(
        zip(decoded, plan.evaluations, strict=True)
    ):
        if isinstance(raw_item, MethodEvaluationOutputItemV2):
            item = raw_item
        else:
            if not isinstance(raw_item, Mapping) or set(raw_item) != _OUTPUT_FIELDS:
                raise MethodEvaluationResponseError(
                    f"model evaluation item {index} must contain only "
                    "evaluation_ref, verdict, supporting_event_refs, and reason"
                )
            try:
                item = MethodEvaluationOutputItemV2.model_validate(dict(raw_item))
            except ValidationError as exc:
                raise MethodEvaluationResponseError(
                    f"model evaluation item {index} is invalid"
                ) from exc
        if item.evaluation_ref != planned.evaluation_ref:
            raise MethodEvaluationResponseError(
                "model evaluation response must use exact plan order and coverage"
            )
        planned_event_refs = planned.evidence_event_refs
        if any(ref not in planned_event_refs for ref in item.supporting_event_refs):
            raise MethodEvaluationResponseError(
                f"model evaluation item {index} supporting_event_refs must belong "
                "to its planned evaluation"
            )
        supporting_ref_set = set(item.supporting_event_refs)
        expected_supporting_order = tuple(
            ref for ref in planned_event_refs if ref in supporting_ref_set
        )
        if item.supporting_event_refs != expected_supporting_order:
            raise MethodEvaluationResponseError(
                f"model evaluation item {index} supporting_event_refs must retain "
                "the planned event order"
            )
        parsed.append(item)
    return tuple(parsed)


def evaluate_method_role_v2(
    *,
    role: MethodEvaluationRoleV2,
    plan: MethodEvaluationPlanV2,
    response: object,
    attempt: Literal["PRIMARY", "REPAIR"],
) -> MethodRoleEvaluationV2:
    """Validate exactly one role response for one state-owned attempt."""

    if role not in {"SPECIALIST", "REVIEWER"}:
        raise ValueError("role must be SPECIALIST or REVIEWER")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    if attempt not in {"PRIMARY", "REPAIR"}:
        raise ValueError("attempt must be PRIMARY or REPAIR")
    evaluations = parse_method_evaluation_response_v2(
        plan=plan,
        response=response,
    )

    return MethodRoleEvaluationV2(
        role=role,
        plan_ref=plan.plan_ref,
        evaluations=evaluations,
        repair_used=attempt == "REPAIR",
    )


def _validate_role_coverage(
    *,
    plan: MethodEvaluationPlanV2,
    evaluation: MethodRoleEvaluationV2,
) -> None:
    if evaluation.plan_ref != plan.plan_ref:
        raise ValueError("role evaluation belongs to a different plan")
    expected_refs = tuple(item.evaluation_ref for item in plan.evaluations)
    actual_refs = tuple(item.evaluation_ref for item in evaluation.evaluations)
    if actual_refs != expected_refs:
        raise ValueError("role evaluation must retain exact plan order and coverage")
    for item, planned in zip(
        evaluation.evaluations,
        plan.evaluations,
        strict=True,
    ):
        if any(
            ref not in planned.evidence_event_refs
            for ref in item.supporting_event_refs
        ):
            raise ValueError(
                "role evaluation supporting_event_refs must belong to their "
                "planned evaluation"
            )
        supporting_ref_set = set(item.supporting_event_refs)
        expected_supporting_order = tuple(
            ref
            for ref in planned.evidence_event_refs
            if ref in supporting_ref_set
        )
        if item.supporting_event_refs != expected_supporting_order:
            raise ValueError(
                "role evaluation supporting_event_refs must retain planned event order"
            )


def resolve_method_consensus_v2(
    *,
    plan: MethodEvaluationPlanV2,
    first: MethodRoleEvaluationV2,
    second: MethodRoleEvaluationV2,
) -> MethodConsensusV2:
    """Resolve two blind roles using refs and verdicts, without comparing reasons."""

    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    if not isinstance(first, MethodRoleEvaluationV2) or not isinstance(
        second, MethodRoleEvaluationV2
    ):
        raise TypeError("consensus inputs must be MethodRoleEvaluationV2")
    if {first.role, second.role} != {"SPECIALIST", "REVIEWER"}:
        raise ValueError("consensus requires one SPECIALIST and one REVIEWER evaluation")
    _validate_role_coverage(plan=plan, evaluation=first)
    _validate_role_coverage(plan=plan, evaluation=second)

    first_blind = tuple(
        (item.evaluation_ref, item.verdict, item.supporting_event_refs)
        for item in first.evaluations
    )
    second_blind = tuple(
        (item.evaluation_ref, item.verdict, item.supporting_event_refs)
        for item in second.evaluations
    )
    verdicts = tuple(item.verdict for item in first.evaluations)
    resolved = (
        first_blind == second_blind
        and "UNKNOWN" not in verdicts
        and "CONFIRMED" in verdicts
    )
    if not resolved:
        return MethodConsensusV2(
            plan_ref=plan.plan_ref,
            status="UNRESOLVED",
            confirmed_evaluation_refs=(),
            confirmed_method_ids=(),
            confirmed_event_refs=(),
        )

    confirmed_pairs = tuple(
        (
            planned.evaluation_ref,
            planned.method_id,
            evaluated.supporting_event_refs,
        )
        for planned, evaluated in zip(
            plan.evaluations,
            first.evaluations,
            strict=True,
        )
        if evaluated.verdict == "CONFIRMED"
    )
    return MethodConsensusV2(
        plan_ref=plan.plan_ref,
        status="RESOLVED",
        confirmed_evaluation_refs=tuple(item[0] for item in confirmed_pairs),
        confirmed_method_ids=tuple(item[1] for item in confirmed_pairs),
        confirmed_event_refs=tuple(
            event_ref
            for _, _, supporting_event_refs in confirmed_pairs
            for event_ref in supporting_event_refs
        ),
    )


__all__ = [
    "MethodEvaluationResponseError",
    "evaluate_method_role_v2",
    "parse_method_evaluation_response_v2",
    "resolve_method_consensus_v2",
]
