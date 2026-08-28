"""Strict model-response validation, one repair, and blind Methods consensus."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from problem_locator.contracts import (
    MethodConsensusV2,
    MethodEvaluationOutputItemV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodRoleEvaluationV2,
)


_OUTPUT_FIELDS = frozenset({"evaluation_ref", "verdict", "reason"})
_NO_REPAIR = object()


class MethodEvaluationResponseError(ValueError):
    """Raised when a model response violates the exact evaluation plan shape."""


class MethodEvaluationRepairExhaustedError(MethodEvaluationResponseError):
    """Raised after both the primary response and sole repair are invalid."""


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
                    "evaluation_ref, verdict, and reason"
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
        parsed.append(item)
    return tuple(parsed)


def evaluate_method_role_v2(
    *,
    role: MethodEvaluationRoleV2,
    plan: MethodEvaluationPlanV2,
    primary_response: object,
    repair_response: object = _NO_REPAIR,
) -> MethodRoleEvaluationV2:
    """Validate primary output, consuming at most one supplied structural repair.

    Both responses are values supplied by the caller.  This pure function never
    invokes a model or Runtime callback.  A repair is ignored when the primary
    response is already valid.
    """

    if role not in {"SPECIALIST", "REVIEWER"}:
        raise ValueError("role must be SPECIALIST or REVIEWER")
    if not isinstance(plan, MethodEvaluationPlanV2):
        raise TypeError("plan must be MethodEvaluationPlanV2")
    try:
        evaluations = parse_method_evaluation_response_v2(
            plan=plan,
            response=primary_response,
        )
    except MethodEvaluationResponseError as primary_error:
        if repair_response is _NO_REPAIR:
            raise MethodEvaluationResponseError(
                "primary model evaluation response is invalid and no repair was supplied"
            ) from primary_error
        try:
            evaluations = parse_method_evaluation_response_v2(
                plan=plan,
                response=repair_response,
            )
        except MethodEvaluationResponseError as repair_error:
            raise MethodEvaluationRepairExhaustedError(
                "the sole structural repair is invalid; no further repair is allowed"
            ) from repair_error
        repair_used = True
    else:
        repair_used = False

    return MethodRoleEvaluationV2(
        role=role,
        plan_ref=plan.plan_ref,
        evaluations=evaluations,
        repair_used=repair_used,
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


def resolve_method_consensus_v2(
    *,
    plan: MethodEvaluationPlanV2,
    first: MethodRoleEvaluationV2,
    second: MethodRoleEvaluationV2,
) -> MethodConsensusV2:
    """Resolve two blind roles using only ``evaluation_ref`` and ``verdict``."""

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
        (item.evaluation_ref, item.verdict) for item in first.evaluations
    )
    second_blind = tuple(
        (item.evaluation_ref, item.verdict) for item in second.evaluations
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
        )

    confirmed_pairs = tuple(
        (planned.evaluation_ref, planned.method_id)
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
    )


__all__ = [
    "MethodEvaluationRepairExhaustedError",
    "MethodEvaluationResponseError",
    "evaluate_method_role_v2",
    "parse_method_evaluation_response_v2",
    "resolve_method_consensus_v2",
]
