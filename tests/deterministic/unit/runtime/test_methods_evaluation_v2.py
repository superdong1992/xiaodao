from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import MethodEvaluationPlanV2
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationRepairExhaustedError,
    MethodEvaluationResponseError,
    evaluate_method_role_v2,
    parse_method_evaluation_response_v2,
    resolve_method_consensus_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_skill import (
    MethodCardV1,
    MethodsManifestV1,
    PreprocessingBindingV1,
    RegistrationTemplateV1,
    ResolvedSpecializedSkillV1,
    RuntimeRoleBindingV1,
)


def _skill() -> ResolvedSpecializedSkillV1:
    role = RuntimeRoleBindingV1("profile", "tools", "policy", "output")
    methods = tuple(
        MethodCardV1(
            id=method_id,
            title=method_id,
            reference=f"references/{method_id}.md",
            priority=priority,
            evidence_markers=(marker,),
        )
        for priority, method_id, marker in (
            (1, "first-method", "FIRST_MARKER"),
            (2, "second-method", "SECOND_MARKER"),
        )
    )
    return ResolvedSpecializedSkillV1(
        registration_root=Path("registration"),
        package_root=Path("package"),
        registration=RegistrationTemplateV1(
            registration_id="evaluation-test",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/evaluation-test",
            skill_name="evaluation-test",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="evaluation-test",
            source_wiki_sha256="1" * 64,
            required_user_inputs=(),
            required_artifacts=(),
            log_derived_fields=("request_id",),
            shared_references=(),
            methods=methods,
        ),
        registration_sha256="2" * 64,
        package_tree_sha256="3" * 64,
        combined_sha256="4" * 64,
    )


def _target(text: str) -> FrozenTargetLogV1:
    content = text.encode("utf-8")
    return FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _plan() -> MethodEvaluationPlanV2:
    skill = _skill()
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target(
                "FIRST_MARKER request_id=req-1\n"
                "SECOND_MARKER request_id=req-2\n"
            ),
        ),
    )
    return build_method_evaluation_plan_v2(skill=skill, evidence=graph)


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
