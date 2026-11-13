"""Project model judgments onto a fixed plan before blind Methods consensus."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from problem_locator.contracts import (
    MethodConsensusV2,
    MethodEvaluationOutputItemV2,
    MethodEvaluationPlanV2,
    MethodEvaluationRoleV2,
    MethodRoleEvaluationV2,
)




class MethodEvaluationResponseError(ValueError):
    """Raised when a model response cannot be read as a JSON array."""

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
        response = response.strip().removeprefix("\ufeff").strip()
        fenced = re.fullmatch(r"```(?:json)?[^\S\r\n]*\r?\n([\s\S]*?)\r?\n```", response, re.IGNORECASE)
        if fenced is not None:
            response = fenced.group(1)
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
    """Read recognizable judgments and restore the server-owned plan order."""
    decoded = _decoded_response(response)
    if not isinstance(decoded, list):
        raise MethodEvaluationResponseError("model evaluation response root must be an array")
    reviews = [item.model_dump() if isinstance(item, MethodEvaluationOutputItemV2) else item
               for item in decoded if isinstance(item, (Mapping, MethodEvaluationOutputItemV2))]
    parsed: list[MethodEvaluationOutputItemV2] = []
    for planned in plan.evaluations:
        matches = [item for item in reviews if item.get("evaluation_ref") == planned.evaluation_ref]
        choices: list[MethodEvaluationOutputItemV2] = []
        for raw in matches:
            verdict = raw.get("verdict")
            verdict = verdict.strip().upper() if isinstance(verdict, str) else "UNKNOWN"
            if verdict not in {"CONFIRMED", "REJECTED", "UNKNOWN"}:
                verdict = "UNKNOWN"
            refs = raw.get("supporting_event_refs", ())
            refs = refs if isinstance(refs, (list, tuple)) else ()
            supporting = tuple(ref for ref in planned.evidence_event_refs if ref in refs)
            if verdict == "CONFIRMED" and not supporting:
                verdict = "UNKNOWN"
            reason = raw.get("reason")
            reason = reason if isinstance(reason, str) and reason.strip() else "模型未提供逐项说明。"
            choices.append(MethodEvaluationOutputItemV2(
                evaluation_ref=planned.evaluation_ref, verdict=verdict,
                supporting_event_refs=supporting if verdict == "CONFIRMED" else (), reason=reason))
        signatures = {(item.verdict, item.supporting_event_refs) for item in choices}
        if len(signatures) == 1:
            parsed.append(choices[0])
        else:
            parsed.append(MethodEvaluationOutputItemV2(
                evaluation_ref=planned.evaluation_ref, verdict="UNKNOWN",
                supporting_event_refs=(), reason="模型未对该项给出明确判断。"))
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
