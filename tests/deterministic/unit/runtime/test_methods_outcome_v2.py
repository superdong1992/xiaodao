from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

import problem_locator.runtime.methods_evidence_v2 as methods_evidence_v2
from problem_locator.contracts import (
    method_terminal_result_ref_v2,
    project_method_terminal_result_v2,
    validate_method_terminal_result_v2,
)
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    fail_method_state_v2,
    finalize_reviewer_consensus_v2,
    interrupt_method_state_v2,
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
from problem_locator.runtime.methods_outcome_v2 import build_method_terminal_result_v2
from problem_locator.runtime.methods_skill import (
    MethodCardV1,
    MethodsManifestV1,
    PreprocessingBindingV1,
    RegistrationTemplateV1,
    ResolvedSpecializedSkillV1,
    RuntimeRoleBindingV1,
)


EVALUATION_ID = "00000000-0000-0000-0000-000000000091"
SECOND_EVALUATION_ID = "00000000-0000-0000-0000-000000000092"
CASE_ID = "00000000-0000-0000-0000-000000000001"
SOURCE_JOB_ID = "00000000-0000-0000-0000-000000000010"


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
            registration_id="outcome-test",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/outcome-test",
            skill_name="outcome-test",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="outcome-test",
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


def _target(*, repeated_first: bool = False) -> FrozenTargetLogV1:
    content = (
        b"FIRST_MARKER request_id=req-1\n"
        + (
            b"FIRST_MARKER request_id=req-noise\n"
            if repeated_first
            else b""
        )
        + b"SECOND_MARKER request_id=req-2\n"
    )
    return FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _context(
    specialist_verdicts: tuple[str, str] = ("CONFIRMED", "REJECTED"),
    reviewer_verdicts: tuple[str, str] = ("CONFIRMED", "REJECTED"),
    *,
    evaluation_id: str = EVALUATION_ID,
    repeated_first: bool = False,
    first_event_index: int | None = None,
):
    skill = _skill()
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(_target(repeated_first=repeated_first),),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    def response(verdicts: tuple[str, str], prefix: str):
        return [
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": verdict,
                "supporting_event_refs": (
                    [item.evidence_event_refs[first_event_index]]
                    if (
                        verdict == "CONFIRMED"
                        and index == 1
                        and first_event_index is not None
                    )
                    else list(item.evidence_event_refs)
                    if verdict == "CONFIRMED"
                    else []
                ),
                "reason": f"{prefix}-{index}",
            }
            for index, (item, verdict) in enumerate(
                zip(plan.evaluations, verdicts, strict=True),
                start=1,
            )
        ]

    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=response(specialist_verdicts, "specialist"),
        attempt="PRIMARY",
    )
    reviewer = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=response(reviewer_verdicts, "reviewer"),
        attempt="PRIMARY",
    )
    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )
    reviewer_pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=CASE_ID,
            source_job_id=SOURCE_JOB_ID,
            evaluation_id=evaluation_id,
            plan=plan,
        ),
        evaluation=specialist,
    )
    state = finalize_reviewer_consensus_v2(
        state=reviewer_pending,
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )
    return graph, plan, consensus, state


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


def test_resolved_result_maps_only_consensus_confirmed_refs_and_limitations() -> None:
    graph, plan, consensus, state = _context()

    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
        limitations=("Only the frozen target logs were evaluated.",),
    )
    repeated = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
        limitations=("Only the frozen target logs were evaluated.",),
    )

    assert repeated == result
    assert result.result_ref.startswith("result-")
    assert result.status == "RESOLVED"
    assert result.reason_code is None
    assert result.confirmed_evaluation_refs == consensus.confirmed_evaluation_refs
    assert result.confirmed_method_ids == consensus.confirmed_method_ids
    assert result.confirmed_event_refs == plan.evaluations[0].evidence_event_refs
    assert result.confirmed_hit_refs == plan.evaluations[0].evidence_hit_refs
    assert result.limitations == ("Only the frozen target logs were evaluated.",)
    assert result.reasons == ()
    assert len(result.evaluations) == 1
    assert result.evaluations[0].verdict == "CONFIRMED"
    assert result.diagnostic_id.startswith("diag-")
    assert all(item.evaluation_ref.startswith("eval-") for item in result.evaluations)


def test_resolved_result_uses_only_selected_events_and_their_graph_hits() -> None:
    graph, plan, consensus, state = _context(
        repeated_first=True,
        first_event_index=0,
    )
    selected_ref = plan.evaluations[0].evidence_event_refs[0]
    noise_ref = plan.evaluations[0].evidence_event_refs[1]
    event_by_ref = {item.event_ref: item for item in graph.events}

    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )

    assert consensus.confirmed_event_refs == (selected_ref,)
    assert result.confirmed_event_refs == (selected_ref,)
    assert result.confirmed_hit_refs == event_by_ref[selected_ref].evidence_hit_refs
    assert result.evaluations[0].evidence_event_refs == (selected_ref,)
    assert (
        result.evaluations[0].evidence_hit_refs
        == event_by_ref[selected_ref].evidence_hit_refs
    )
    assert noise_ref not in result.confirmed_event_refs
    assert all(
        hit_ref not in result.confirmed_hit_refs
        for hit_ref in event_by_ref[noise_ref].evidence_hit_refs
    )


def test_graph_validation_rejects_hits_from_an_unselected_event() -> None:
    graph, plan, _, state = _context(
        repeated_first=True,
        first_event_index=0,
    )
    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )
    noise_event_ref = plan.evaluations[0].evidence_event_refs[1]
    noise_event = next(
        item for item in graph.events if item.event_ref == noise_event_ref
    )
    forged_evaluation = result.evaluations[0].model_copy(
        update={"evidence_hit_refs": noise_event.evidence_hit_refs}
    )
    forged = _with_recomputed_result_ref(
        result,
        evaluations=(forged_evaluation,),
        confirmed_hit_refs=noise_event.evidence_hit_refs,
    )

    with pytest.raises(ValueError, match="exact consensus evidence"):
        validate_method_terminal_result_v2(
            state,
            forged,
            plan,
            evidence=graph,
        )


def test_terminal_result_rejects_extra_free_form_reason() -> None:
    graph, plan, _, state = _context()

    with pytest.raises(ValueError, match="production state"):
        build_method_terminal_result_v2(
            state=state,
            plan=plan,
            evidence=graph,
            terminal_job_id=SOURCE_JOB_ID,
            reasons=("private model summary must not escape",),
        )


def test_terminal_result_ref_includes_case_source_and_terminal_job_identity() -> None:
    graph, plan, _, _ = _context()
    first = fail_method_state_v2(
        state=start_method_state_v2(
            case_id=CASE_ID,
            source_job_id=SOURCE_JOB_ID,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        reason_code="SERVER_INVARIANT_VIOLATION",
        reason="private first workflow detail",
    )
    second = fail_method_state_v2(
        state=start_method_state_v2(
            case_id="00000000-0000-0000-0000-000000000002",
            source_job_id="00000000-0000-0000-0000-000000000020",
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        reason_code="SERVER_INVARIANT_VIOLATION",
        reason="private second workflow detail",
    )

    first_result = build_method_terminal_result_v2(
        state=first,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )
    second_result = build_method_terminal_result_v2(
        state=second,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )
    other_terminal_result = build_method_terminal_result_v2(
        state=first,
        plan=plan,
        evidence=graph,
        terminal_job_id="00000000-0000-0000-0000-000000000030",
    )

    assert first_result.result_ref != second_result.result_ref
    assert first_result.diagnostic_id != second_result.diagnostic_id
    assert first_result.result_ref != other_terminal_result.result_ref
    assert first_result.diagnostic_id == other_terminal_result.diagnostic_id


def test_projection_revalidates_single_field_hit_ref_mutation() -> None:
    graph, plan, _, state = _context()
    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )
    mutated = result.model_copy(
        update={"confirmed_hit_refs": ("hit-" + "f" * 64,)}
    )

    with pytest.raises(ValidationError, match="result_ref|confirmed"):
        project_method_terminal_result_v2(mutated)


def test_unresolved_result_clears_every_confirmed_ref() -> None:
    graph, plan, _, state = _context(
        specialist_verdicts=("REJECTED", "REJECTED"),
        reviewer_verdicts=("REJECTED", "REJECTED"),
    )

    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
        limitations=("No method was confirmed.",),
    )

    assert result.status == "UNRESOLVED"
    assert result.reason_code == "NO_CONFIRMED_METHOD"
    assert result.confirmed_evaluation_refs == ()
    assert result.confirmed_method_ids == ()
    assert result.confirmed_event_refs == ()
    assert result.confirmed_hit_refs == ()
    assert result.evaluations == ()
    assert result.limitations == ("No method was confirmed.",)
    assert all("specialist-" not in item and "reviewer-" not in item for item in result.reasons)


def test_no_matching_evidence_result_exposes_only_server_reason() -> None:
    skill = _skill()
    content = b"no marker is present\n"
    target = FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    graph = scan_method_evidence_v2(skill=skill, target_logs=(target,))
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    state = start_method_state_v2(
        case_id=CASE_ID,
        source_job_id=SOURCE_JOB_ID,
        evaluation_id=EVALUATION_ID,
        plan=plan,
    )

    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )

    assert result.status == "UNRESOLVED"
    assert result.reason_code == "NO_MATCHING_METHOD_EVIDENCE"
    assert result.evaluations == ()
    assert result.confirmed_evaluation_refs == ()
    assert result.reasons == ("当前日志中没有匹配任何已加载方法的证据。",)


def test_same_plan_has_distinct_result_identity_per_evaluation() -> None:
    first_graph, first_plan, _, first_state = _context(
        evaluation_id=EVALUATION_ID
    )
    second_graph, second_plan, _, second_state = _context(
        evaluation_id=SECOND_EVALUATION_ID
    )
    first = build_method_terminal_result_v2(
        state=first_state,
        plan=first_plan,
        evidence=first_graph,
        terminal_job_id=SOURCE_JOB_ID,
    )
    second = build_method_terminal_result_v2(
        state=second_state,
        plan=second_plan,
        evidence=second_graph,
        terminal_job_id=SOURCE_JOB_ID,
    )

    assert first.plan_ref == second.plan_ref
    assert first.evaluation_id != second.evaluation_id
    assert first.diagnostic_id != second.diagnostic_id
    assert first.result_ref != second.result_ref


def test_failed_result_clears_confirmed_refs_even_after_resolved_consensus() -> None:
    graph, plan, consensus, resolved = _context()
    assert consensus.confirmed_evaluation_refs
    failed = fail_method_state_v2(
        state=resolved,
        reason_code="AUDIT_ARCHIVE_FAILED",
        reason="The terminal archive could not be persisted.",
    )

    result = build_method_terminal_result_v2(
        state=failed,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )

    assert result.status == "FAILED"
    assert result.reason_code == "AUDIT_ARCHIVE_FAILED"
    assert result.confirmed_evaluation_refs == ()
    assert result.confirmed_method_ids == ()
    assert result.confirmed_event_refs == ()
    assert result.confirmed_hit_refs == ()
    assert result.evaluations == ()
    assert result.reasons == (
        "评估审计记录未能完整归档。",
    )


def test_outcome_mapping_does_not_rescan_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, plan, _, state = _context()

    def fail_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"outcome mapping rescanned evidence: {args!r} {kwargs!r}")

    monkeypatch.setattr(methods_evidence_v2, "scan_method_evidence_v2", fail_scan)
    monkeypatch.setattr(methods_evidence_v2, "_validated_logs", fail_scan)

    result = build_method_terminal_result_v2(
        state=state,
        plan=plan,
        evidence=graph,
        terminal_job_id=SOURCE_JOB_ID,
    )

    assert result.status == "RESOLVED"


def test_interrupted_state_does_not_generate_terminal_result() -> None:
    graph, plan, _, _ = _context()
    interrupted = interrupt_method_state_v2(
        state=start_method_state_v2(
            case_id=CASE_ID,
            source_job_id=SOURCE_JOB_ID,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        )
    )

    with pytest.raises(ValueError, match="terminal result requires"):
        build_method_terminal_result_v2(
            state=interrupted,
            plan=plan,
            evidence=graph,
            terminal_job_id=SOURCE_JOB_ID,
        )


def test_single_field_state_mutation_cannot_publish_unresolved_result() -> None:
    graph, plan, _, resolved = _context()
    mutated = resolved.model_copy(update={"status": "UNRESOLVED"})

    with pytest.raises((ValidationError, ValueError)):
        build_method_terminal_result_v2(
            state=mutated,
            plan=plan,
            evidence=graph,
            terminal_job_id=SOURCE_JOB_ID,
        )
