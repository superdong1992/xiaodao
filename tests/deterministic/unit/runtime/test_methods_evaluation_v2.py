from __future__ import annotations

import copy
import json

import pytest

from problem_locator.contracts import (
    MethodEvaluationPlanItemV2,
    MethodEvaluationPlanV2,
    method_evaluation_plan_ref_v2,
    method_evaluation_ref_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationRepairExhaustedError,
    MethodEvaluationResponseError,
    evaluate_method_role_v2,
    parse_method_evaluation_response_v2,
    resolve_method_consensus_v2,
)


def _plan() -> MethodEvaluationPlanV2:
    skill_sha256 = "1" * 64
    graph_ref = "graph-" + "2" * 64
    evaluations = tuple(
        MethodEvaluationPlanItemV2(
            evaluation_ref=method_evaluation_ref_v2(
                method_id=method_id,
                method_priority=method_priority,
                evidence_event_refs=(event_ref,),
                evidence_hit_refs=(hit_ref,),
            ),
            method_id=method_id,
            method_priority=method_priority,
            evidence_event_refs=(event_ref,),
            evidence_hit_refs=(hit_ref,),
        )
        for method_priority, method_id, event_ref, hit_ref in (
            (1, "first-method", "event-" + "5" * 64, "hit-" + "3" * 64),
            (2, "second-method", "event-" + "6" * 64, "hit-" + "4" * 64),
        )
    )
    return MethodEvaluationPlanV2(
        plan_ref=method_evaluation_plan_ref_v2(
            skill_sha256=skill_sha256,
            evidence_graph_ref=graph_ref,
            evaluations=evaluations,
        ),
        skill_sha256=skill_sha256,
        evidence_graph_ref=graph_ref,
        evaluations=evaluations,
    )


def _response(
    plan: MethodEvaluationPlanV2,
    verdicts: tuple[str, ...] = ("CONFIRMED", "REJECTED"),
    *,
    reason_prefix: str = "reason",
) -> list[dict[str, str]]:
    return [
        {
            "evaluation_ref": planned.evaluation_ref,
            "verdict": verdict,
            "reason": f"{reason_prefix}-{index}",
        }
        for index, (planned, verdict) in enumerate(
            zip(plan.evaluations, verdicts, strict=True),
            start=1,
        )
    ]


def test_response_root_is_array_with_exact_item_fields() -> None:
    plan = _plan()
    payload = json.dumps(_response(plan)).encode("utf-8")

    parsed = parse_method_evaluation_response_v2(plan=plan, response=payload)

    assert [item.evaluation_ref for item in parsed] == [
        item.evaluation_ref for item in plan.evaluations
    ]
    assert [item.verdict for item in parsed] == ["CONFIRMED", "REJECTED"]
    assert all(item.evaluation_ref.startswith("eval-") for item in parsed)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.reverse(),
        lambda value: value.pop(),
        lambda value: value.append(copy.deepcopy(value[0])),
        lambda value: value[0].update({"unexpected": True}),
    ],
    ids=["out-of-order", "missing", "extra-item", "extra-field"],
)
def test_response_rejects_order_coverage_and_extra_fields(mutation: object) -> None:
    plan = _plan()
    response = _response(plan)
    mutation(response)  # type: ignore[operator]

    with pytest.raises(MethodEvaluationResponseError):
        parse_method_evaluation_response_v2(plan=plan, response=response)


@pytest.mark.parametrize(
    "response",
    [
        {"not": "an array"},
        b'{"not":"an array"}',
        b'[{"evaluation_ref":"duplicate","evaluation_ref":"field"}]',
    ],
)
def test_response_rejects_non_array_or_ambiguous_json(response: object) -> None:
    with pytest.raises(MethodEvaluationResponseError):
        parse_method_evaluation_response_v2(plan=_plan(), response=response)


def test_each_role_may_consume_only_one_structural_repair() -> None:
    plan = _plan()
    valid = _response(plan)

    repaired = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        primary_response={"not": "an array"},
        repair_response=valid,
    )
    primary_wins = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        primary_response=valid,
        repair_response={"must": "remain unconsumed"},
    )

    assert repaired.repair_used is True
    assert primary_wins.repair_used is False
    with pytest.raises(MethodEvaluationRepairExhaustedError, match="no further repair"):
        evaluate_method_role_v2(
            role="SPECIALIST",
            plan=plan,
            primary_response={"not": "an array"},
            repair_response={"still": "not an array"},
        )


def _roles(
    plan: MethodEvaluationPlanV2,
    first_verdicts: tuple[str, str],
    second_verdicts: tuple[str, str],
):
    return (
        evaluate_method_role_v2(
            role="SPECIALIST",
            plan=plan,
            primary_response=_response(
                plan,
                first_verdicts,
                reason_prefix="diagnose-private-reason",
            ),
        ),
        evaluate_method_role_v2(
            role="REVIEWER",
            plan=plan,
            primary_response=_response(
                plan,
                second_verdicts,
                reason_prefix="review-private-reason",
            ),
        ),
    )


def test_blind_consensus_ignores_reason_and_resolves_complete_agreement() -> None:
    plan = _plan()
    diagnose, review = _roles(
        plan,
        ("CONFIRMED", "REJECTED"),
        ("CONFIRMED", "REJECTED"),
    )

    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=diagnose,
        second=review,
    )

    assert consensus.status == "RESOLVED"
    assert consensus.confirmed_evaluation_refs == (plan.evaluations[0].evaluation_ref,)
    assert consensus.confirmed_method_ids == ("first-method",)


@pytest.mark.parametrize(
    ("first_verdicts", "second_verdicts"),
    [
        (("CONFIRMED", "REJECTED"), ("REJECTED", "REJECTED")),
        (("UNKNOWN", "REJECTED"), ("UNKNOWN", "REJECTED")),
        (("REJECTED", "REJECTED"), ("REJECTED", "REJECTED")),
    ],
    ids=["disagreement", "unknown", "no-confirmed"],
)
def test_consensus_is_unresolved_for_disagreement_unknown_or_no_confirmation(
    first_verdicts: tuple[str, str],
    second_verdicts: tuple[str, str],
) -> None:
    plan = _plan()
    diagnose, review = _roles(plan, first_verdicts, second_verdicts)

    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=diagnose,
        second=review,
    )

    assert consensus.status == "UNRESOLVED"
    assert consensus.confirmed_evaluation_refs == ()
    assert consensus.confirmed_method_ids == ()
