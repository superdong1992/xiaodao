from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import MethodEvaluationPlanV2, MethodRoleEvaluationV2
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
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
            activation_markers=(marker,),
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


def _same_method_target_and_noise_plan() -> MethodEvaluationPlanV2:
    """Mirror the historical unrelated-log-noise shape at contract level."""

    base = _skill()
    skill = ResolvedSpecializedSkillV1(
        registration_root=base.registration_root,
        package_root=base.package_root,
        registration=base.registration,
        methods=MethodsManifestV1(
            skill_name=base.methods.skill_name,
            source_wiki_sha256=base.methods.source_wiki_sha256,
            required_user_inputs=base.methods.required_user_inputs,
            required_artifacts=base.methods.required_artifacts,
            log_derived_fields=("request_id",),
            shared_references=base.methods.shared_references,
            methods=(
                MethodCardV1(
                    id="client-receive-blocked",
                    title="client-receive-blocked",
                    reference="references/client-receive-blocked.md",
                    priority=1,
                    evidence_markers=("LATE_RESPONSE",),
                    activation_markers=("LATE_RESPONSE",),
                ),
            ),
        ),
        registration_sha256=base.registration_sha256,
        package_tree_sha256=base.package_tree_sha256,
        combined_sha256=base.combined_sha256,
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            _target(
                "LATE_RESPONSE service=svc_noise api=NoiseApi request_id=999\n"
                "LATE_RESPONSE service=svc_profile api=Lookup request_id=601\n"
            ),
        ),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    assert len(plan.evaluations) == 1
    assert len(plan.evaluations[0].evidence_event_refs) == 2
    return plan


def _response(
    plan: MethodEvaluationPlanV2,
    verdicts: tuple[str, ...] = ("CONFIRMED", "REJECTED"),
    *,
    reason_prefix: str = "reason",
) -> list[dict[str, object]]:
    return [
        {
            "evaluation_ref": planned.evaluation_ref,
            "verdict": verdict,
            "supporting_event_refs": (
                list(planned.evidence_event_refs) if verdict == "CONFIRMED" else []
            ),
            "reason": f"{reason_prefix}-{index}",
        }
        for index, (planned, verdict) in enumerate(
            zip(plan.evaluations, verdicts, strict=True),
            start=1,
        )
    ]


def test_response_root_is_array_with_exact_item_fields() -> None:
    plan = _plan()
    response = _response(plan)
    assert all(
        tuple(item) == (
            "evaluation_ref",
            "verdict",
            "supporting_event_refs",
            "reason",
        )
        for item in response
    )
    payload = json.dumps(response).encode("utf-8")

    parsed = parse_method_evaluation_response_v2(plan=plan, response=payload)

    assert [item.evaluation_ref for item in parsed] == [
        item.evaluation_ref for item in plan.evaluations
    ]
    assert [item.verdict for item in parsed] == ["CONFIRMED", "REJECTED"]
    assert parsed[0].supporting_event_refs == plan.evaluations[0].evidence_event_refs
    assert parsed[1].supporting_event_refs == ()
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


@pytest.mark.parametrize(
    ("verdict", "supporting"),
    [
        ("CONFIRMED", ()),
        ("REJECTED", ("planned",)),
        ("UNKNOWN", ("planned",)),
    ],
    ids=["confirmed-empty", "rejected-nonempty", "unknown-nonempty"],
)
def test_response_binds_supporting_event_presence_to_verdict(
    verdict: str,
    supporting: tuple[str, ...],
) -> None:
    plan = _plan()
    response = _response(plan)
    response[0]["verdict"] = verdict
    response[0]["supporting_event_refs"] = [
        plan.evaluations[0].evidence_event_refs[0] if ref == "planned" else ref
        for ref in supporting
    ]

    with pytest.raises(MethodEvaluationResponseError):
        parse_method_evaluation_response_v2(plan=plan, response=response)


def test_response_rejects_duplicate_or_foreign_supporting_event_refs() -> None:
    plan = _plan()
    planned_ref = plan.evaluations[0].evidence_event_refs[0]
    foreign_ref = plan.evaluations[1].evidence_event_refs[0]

    duplicate = _response(plan)
    duplicate[0]["supporting_event_refs"] = [planned_ref, planned_ref]
    with pytest.raises(MethodEvaluationResponseError):
        parse_method_evaluation_response_v2(plan=plan, response=duplicate)

    foreign = _response(plan)
    foreign[0]["supporting_event_refs"] = [foreign_ref]
    with pytest.raises(MethodEvaluationResponseError, match="belong"):
        parse_method_evaluation_response_v2(plan=plan, response=foreign)


def test_response_requires_supporting_refs_in_current_evaluation_event_order() -> None:
    plan = _same_method_target_and_noise_plan()
    response = _response(plan, ("CONFIRMED",))
    response[0]["supporting_event_refs"] = list(
        reversed(plan.evaluations[0].evidence_event_refs)
    )

    with pytest.raises(MethodEvaluationResponseError, match="planned event order"):
        parse_method_evaluation_response_v2(plan=plan, response=response)


@pytest.mark.parametrize(
    ("attempt", "repair_used"),
    [("PRIMARY", False), ("REPAIR", True)],
)
def test_role_evaluation_parses_one_explicit_attempt(
    attempt: str,
    repair_used: bool,
) -> None:
    plan = _plan()

    evaluation = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=_response(plan),
        attempt=attempt,  # type: ignore[arg-type]
    )

    assert evaluation.repair_used is repair_used


def test_role_evaluation_rejects_each_invalid_attempt_independently() -> None:
    plan = _plan()

    for attempt in ("PRIMARY", "REPAIR"):
        with pytest.raises(MethodEvaluationResponseError):
            evaluate_method_role_v2(
                role="SPECIALIST",
                plan=plan,
                response={"not": "an array"},
                attempt=attempt,  # type: ignore[arg-type]
            )


def test_role_evaluation_has_no_primary_plus_repair_fallback_api() -> None:
    plan = _plan()

    with pytest.raises(TypeError, match="primary_response"):
        evaluate_method_role_v2(
            role="SPECIALIST",
            plan=plan,
            primary_response={"not": "an array"},
            repair_response=_response(plan),
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
            response=_response(
                plan,
                first_verdicts,
                reason_prefix="diagnose-private-reason",
            ),
            attempt="PRIMARY",
        ),
        evaluate_method_role_v2(
            role="REVIEWER",
            plan=plan,
            response=_response(
                plan,
                second_verdicts,
                reason_prefix="review-private-reason",
            ),
            attempt="PRIMARY",
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
    assert consensus.confirmed_event_refs == (
        plan.evaluations[0].evidence_event_refs[0],
    )


def test_consensus_keeps_only_target_event_from_same_method_noise() -> None:
    plan = _same_method_target_and_noise_plan()
    noise_ref, target_ref = plan.evaluations[0].evidence_event_refs
    specialist_response = _response(plan, ("CONFIRMED",), reason_prefix="specialist")
    reviewer_response = _response(plan, ("CONFIRMED",), reason_prefix="reviewer")
    specialist_response[0]["supporting_event_refs"] = [target_ref]
    reviewer_response[0]["supporting_event_refs"] = [target_ref]
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=specialist_response,
        attempt="PRIMARY",
    )
    reviewer = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=reviewer_response,
        attempt="PRIMARY",
    )

    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )

    assert consensus.status == "RESOLVED"
    assert consensus.confirmed_event_refs == (target_ref,)
    assert noise_ref not in consensus.confirmed_event_refs


def test_consensus_requires_exact_supporting_event_agreement() -> None:
    plan = _same_method_target_and_noise_plan()
    noise_ref, target_ref = plan.evaluations[0].evidence_event_refs
    specialist_response = _response(plan, ("CONFIRMED",))
    reviewer_response = _response(plan, ("CONFIRMED",))
    specialist_response[0]["supporting_event_refs"] = [target_ref]
    reviewer_response[0]["supporting_event_refs"] = [noise_ref]
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=specialist_response,
        attempt="PRIMARY",
    )
    reviewer = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=reviewer_response,
        attempt="PRIMARY",
    )

    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )

    assert consensus.status == "UNRESOLVED"
    assert consensus.confirmed_evaluation_refs == ()
    assert consensus.confirmed_method_ids == ()
    assert consensus.confirmed_event_refs == ()


def test_consensus_revalidates_supporting_refs_against_its_plan() -> None:
    plan = _plan()
    specialist, reviewer = _roles(
        plan,
        ("CONFIRMED", "REJECTED"),
        ("CONFIRMED", "REJECTED"),
    )
    payload = specialist.model_dump(mode="json")
    payload["evaluations"][0]["supporting_event_refs"] = [
        plan.evaluations[1].evidence_event_refs[0]
    ]
    foreign = MethodRoleEvaluationV2.model_validate(payload)

    with pytest.raises(ValueError, match="belong"):
        resolve_method_consensus_v2(
            plan=plan,
            first=foreign,
            second=reviewer,
        )


def test_consensus_flattens_confirmed_events_in_plan_order() -> None:
    plan = _plan()
    specialist, reviewer = _roles(
        plan,
        ("CONFIRMED", "CONFIRMED"),
        ("CONFIRMED", "CONFIRMED"),
    )

    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )

    assert consensus.status == "RESOLVED"
    assert consensus.confirmed_event_refs == tuple(
        event_ref
        for evaluation in plan.evaluations
        for event_ref in evaluation.evidence_event_refs
    )


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
    assert consensus.confirmed_event_refs == ()
