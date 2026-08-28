from __future__ import annotations

import hashlib
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from problem_locator.contracts import (
    MethodStateReasonCodeV2,
    MethodStateStatusV2,
    MethodStateV2,
)
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    fail_method_state_v2,
    finalize_reviewer_consensus_v2,
    interrupt_method_state_v2,
    record_model_execution_failure_v2,
    record_protocol_error_v2,
    record_semantic_invalid_v2,
    resume_method_state_v2,
    start_method_state_v2,
)
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationResponseError,
    evaluate_method_role_v2,
    resolve_method_consensus_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
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


EVALUATION_ID = "00000000-0000-0000-0000-000000000091"
SECOND_EVALUATION_ID = "00000000-0000-0000-0000-000000000092"


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
            registration_id="state-test",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/state-test",
            skill_name="state-test",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="state-test",
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


def _plan(*, matching: bool = True):
    skill = _skill()
    text = (
        "FIRST_MARKER request_id=req-1\nSECOND_MARKER request_id=req-2\n"
        if matching
        else "no matching evidence\n"
    )
    graph = scan_method_evidence_v2(skill=skill, target_logs=(_target(text),))
    return build_method_evaluation_plan_v2(skill=skill, evidence=graph)


def _response(plan, verdicts: tuple[str, str], prefix: str):
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": verdict,
            "reason": f"{prefix}-{index}",
        }
        for index, (item, verdict) in enumerate(
            zip(plan.evaluations, verdicts, strict=True),
            start=1,
        )
    ]


def _evaluations(
    plan,
    specialist_verdicts: tuple[str, str] = ("CONFIRMED", "REJECTED"),
    reviewer_verdicts: tuple[str, str] = ("CONFIRMED", "REJECTED"),
    *,
    specialist_attempt: str = "PRIMARY",
    reviewer_attempt: str = "PRIMARY",
):
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=_response(plan, specialist_verdicts, "specialist"),
        attempt=specialist_attempt,  # type: ignore[arg-type]
    )
    reviewer = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=_response(plan, reviewer_verdicts, "reviewer"),
        attempt=reviewer_attempt,  # type: ignore[arg-type]
    )
    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=reviewer,
    )
    return specialist, reviewer, consensus


def _start(plan, *, evaluation_id: str = EVALUATION_ID):
    return start_method_state_v2(evaluation_id=evaluation_id, plan=plan)


def _reviewer_pending(plan):
    specialist, _, _ = _evaluations(plan)
    return accept_specialist_evaluation_v2(
        state=_start(plan),
        evaluation=specialist,
    )


def test_state_status_contract_is_exact_and_has_no_partial_terminal() -> None:
    assert set(get_args(MethodStateStatusV2)) == {
        "SPECIALIST_PENDING",
        "REVIEWER_PENDING",
        "RESOLVED",
        "UNRESOLVED",
        "FAILED",
        "INTERRUPTED",
    }


def test_state_reason_code_contract_is_exact() -> None:
    assert set(get_args(MethodStateReasonCodeV2)) == {
        "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
        "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED",
        "SPECIALIST_SEMANTIC_INVALID",
        "REVIEWER_SEMANTIC_INVALID",
        "SPECIALIST_MODEL_EXECUTION_FAILED",
        "REVIEWER_MODEL_EXECUTION_FAILED",
        "SPECIALIST_REVIEWER_DISAGREEMENT",
        "INCOMPLETE_EVALUATION",
        "NO_CONFIRMED_METHOD",
        "NO_MATCHING_METHOD_EVIDENCE",
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    }


@pytest.mark.parametrize(
    ("specialist_verdicts", "reviewer_verdicts", "status", "reason_code"),
    [
        (
            ("CONFIRMED", "REJECTED"),
            ("CONFIRMED", "REJECTED"),
            "RESOLVED",
            None,
        ),
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
            ("CONFIRMED", "UNKNOWN"),
            ("CONFIRMED", "UNKNOWN"),
            "UNRESOLVED",
            "INCOMPLETE_EVALUATION",
        ),
        (
            ("UNKNOWN", "REJECTED"),
            ("REJECTED", "REJECTED"),
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
def test_consensus_truth_table_drives_terminal_state(
    specialist_verdicts: tuple[str, str],
    reviewer_verdicts: tuple[str, str],
    status: str,
    reason_code: str | None,
) -> None:
    plan = _plan()
    specialist, reviewer, consensus = _evaluations(
        plan,
        specialist_verdicts,
        reviewer_verdicts,
    )
    pending = accept_specialist_evaluation_v2(
        state=_start(plan),
        evaluation=specialist,
    )

    terminal = finalize_reviewer_consensus_v2(
        state=pending,
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )

    assert terminal.status == status
    assert terminal.reason_code == reason_code
    assert terminal.diagnostic_id is not None
    assert "PARTIALLY_RESOLVED" not in terminal.status


def test_no_matching_method_evidence_is_immediately_unresolved() -> None:
    state = _start(_plan(matching=False))

    assert state.status == "UNRESOLVED"
    assert state.reason_code == "NO_MATCHING_METHOD_EVIDENCE"
    assert state.evaluation_refs == ()


def test_terminal_state_and_diagnostic_identity_are_stable() -> None:
    plan = _plan()
    pending = _start(plan)

    first = record_semantic_invalid_v2(
        state=pending,
        role="SPECIALIST",
        reason="The semantic response is invalid.",
        evaluation_ref=plan.evaluations[0].evaluation_ref,
    )
    second = record_semantic_invalid_v2(
        state=pending,
        role="SPECIALIST",
        reason="The semantic response is invalid.",
        evaluation_ref=plan.evaluations[0].evaluation_ref,
    )

    assert first == second
    assert first.state_ref.startswith("state-")
    assert first.diagnostic_id is not None
    assert first.diagnostic_id.startswith("diag-")


def test_same_plan_has_distinct_diagnostics_for_distinct_evaluations() -> None:
    plan = _plan()
    first = record_semantic_invalid_v2(
        state=_start(plan, evaluation_id=EVALUATION_ID),
        role="SPECIALIST",
        reason="The semantic response is invalid.",
        evaluation_ref=plan.evaluations[0].evaluation_ref,
    )
    second = record_semantic_invalid_v2(
        state=_start(plan, evaluation_id=SECOND_EVALUATION_ID),
        role="SPECIALIST",
        reason="The semantic response is invalid.",
        evaluation_ref=plan.evaluations[0].evaluation_ref,
    )

    assert first.plan_ref == second.plan_ref
    assert first.evaluation_id != second.evaluation_id
    assert first.diagnostic_id != second.diagnostic_id
    assert first.state_ref != second.state_ref


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
def test_each_role_gets_one_protocol_repair_then_exhausts(role: str) -> None:
    plan = _plan()
    state = (
        _start(plan)
        if role == "SPECIALIST"
        else _reviewer_pending(plan)
    )
    evaluation_ref = plan.evaluations[0].evaluation_ref

    first = record_protocol_error_v2(
        state=state,
        role=role,
        reason="The response shape is invalid.",
        evaluation_ref=evaluation_ref,
    )
    specialist, reviewer, consensus = _evaluations(
        plan,
        specialist_attempt="REPAIR" if role == "SPECIALIST" else "PRIMARY",
        reviewer_attempt="REPAIR" if role == "REVIEWER" else "PRIMARY",
    )
    repaired_terminal = (
        accept_specialist_evaluation_v2(state=first, evaluation=specialist)
        if role == "SPECIALIST"
        else finalize_reviewer_consensus_v2(
            state=first,
            plan=plan,
            reviewer_evaluation=reviewer,
            consensus=consensus,
        )
    )
    second = record_protocol_error_v2(
        state=first,
        role=role,
        reason="The repaired response shape is still invalid.",
        evaluation_ref=evaluation_ref,
    )

    assert first.status == f"{role}_PENDING"
    assert getattr(first, f"{role.lower()}_protocol_failures") == 1
    assert repaired_terminal.status == (
        "REVIEWER_PENDING" if role == "SPECIALIST" else "RESOLVED"
    )
    assert second.status == "UNRESOLVED"
    assert second.reason_code == f"{role}_PROTOCOL_REPAIR_EXHAUSTED"
    assert second.diagnostic_evaluation_ref == evaluation_ref


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
@pytest.mark.parametrize(
    ("recorded_failure", "attempt"),
    [(False, "REPAIR"), (True, "PRIMARY")],
    ids=["repair-without-error", "primary-after-error"],
)
def test_role_acceptance_requires_attempt_to_match_state_repair_count(
    role: str,
    recorded_failure: bool,
    attempt: str,
) -> None:
    plan = _plan()
    state = _start(plan) if role == "SPECIALIST" else _reviewer_pending(plan)
    if recorded_failure:
        state = record_protocol_error_v2(
            state=state,
            role=role,  # type: ignore[arg-type]
            reason="The primary response shape is invalid.",
        )
    specialist, reviewer, consensus = _evaluations(
        plan,
        specialist_attempt=attempt if role == "SPECIALIST" else "PRIMARY",
        reviewer_attempt=attempt if role == "REVIEWER" else "PRIMARY",
    )

    with pytest.raises(ValueError, match=f"{role} state expects"):
        if role == "SPECIALIST":
            accept_specialist_evaluation_v2(state=state, evaluation=specialist)
        else:
            finalize_reviewer_consensus_v2(
                state=state,
                plan=plan,
                reviewer_evaluation=reviewer,
                consensus=consensus,
            )


def test_state_contract_rejects_persisted_repair_count_mismatch() -> None:
    plan = _plan()
    pending = _reviewer_pending(plan)
    mutated = pending.model_copy(update={"specialist_protocol_failures": 1})

    with pytest.raises(ValidationError, match="repair marker differs"):
        MethodStateV2.model_validate(mutated.model_dump(mode="python"))


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
def test_second_protocol_error_ends_role_before_a_third_response(role: str) -> None:
    plan = _plan()
    state = _start(plan) if role == "SPECIALIST" else _reviewer_pending(plan)

    with pytest.raises(MethodEvaluationResponseError):
        evaluate_method_role_v2(
            role=role,  # type: ignore[arg-type]
            plan=plan,
            response={"invalid": "primary"},
            attempt="PRIMARY",
        )
    first_failure = record_protocol_error_v2(
        state=state,
        role=role,  # type: ignore[arg-type]
        reason="The primary response shape is invalid.",
    )
    with pytest.raises(MethodEvaluationResponseError):
        evaluate_method_role_v2(
            role=role,  # type: ignore[arg-type]
            plan=plan,
            response={"invalid": "repair"},
            attempt="REPAIR",
        )
    exhausted = record_protocol_error_v2(
        state=first_failure,
        role=role,  # type: ignore[arg-type]
        reason="The repaired response shape is still invalid.",
    )
    specialist, reviewer, consensus = _evaluations(
        plan,
        specialist_attempt="REPAIR" if role == "SPECIALIST" else "PRIMARY",
        reviewer_attempt="REPAIR" if role == "REVIEWER" else "PRIMARY",
    )

    assert exhausted.status == "UNRESOLVED"
    assert getattr(exhausted, f"{role.lower()}_protocol_failures") == 2
    with pytest.raises(ValueError, match="state is not pending"):
        if role == "SPECIALIST":
            accept_specialist_evaluation_v2(
                state=exhausted,
                evaluation=specialist,
            )
        else:
            finalize_reviewer_consensus_v2(
                state=exhausted,
                plan=plan,
                reviewer_evaluation=reviewer,
                consensus=consensus,
            )


@pytest.mark.parametrize(
    ("operation", "role", "reason_code"),
    [
        (record_semantic_invalid_v2, "SPECIALIST", "SPECIALIST_SEMANTIC_INVALID"),
        (record_semantic_invalid_v2, "REVIEWER", "REVIEWER_SEMANTIC_INVALID"),
        (
            record_model_execution_failure_v2,
            "SPECIALIST",
            "SPECIALIST_MODEL_EXECUTION_FAILED",
        ),
        (
            record_model_execution_failure_v2,
            "REVIEWER",
            "REVIEWER_MODEL_EXECUTION_FAILED",
        ),
    ],
)
def test_semantic_and_model_failures_are_unresolved_not_failed(
    operation,
    role: str,
    reason_code: str,
) -> None:
    plan = _plan()
    state = (
        _start(plan)
        if role == "SPECIALIST"
        else _reviewer_pending(plan)
    )

    terminal = operation(
        state=state,
        role=role,
        reason="Role evaluation could not be accepted.",
        evaluation_ref=plan.evaluations[0].evaluation_ref,
    )

    assert terminal.status == "UNRESOLVED"
    assert terminal.reason_code == reason_code
    assert terminal.status != "FAILED"


@pytest.mark.parametrize(
    "reason_code",
    [
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    ],
)
def test_only_infrastructure_categories_enter_failed(reason_code: str) -> None:
    state = fail_method_state_v2(
        state=_start(_plan()),
        reason_code=reason_code,
        reason="The server-owned terminal step failed.",
    )

    assert state.status == "FAILED"
    assert state.reason_code == reason_code


def test_non_infrastructure_reason_cannot_enter_failed() -> None:
    with pytest.raises(ValueError, match="only resource drift"):
        fail_method_state_v2(
            state=_start(_plan()),
            reason_code="SPECIALIST_SEMANTIC_INVALID",
            reason="Semantic rejection is not infrastructure failure.",
        )


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
def test_interrupt_preserves_role_and_resume_returns_to_its_pending_state(role: str) -> None:
    plan = _plan()
    state = (
        _start(plan)
        if role == "SPECIALIST"
        else _reviewer_pending(plan)
    )

    interrupted = interrupt_method_state_v2(state=state)
    resumed = resume_method_state_v2(state=interrupted)

    assert interrupted.status == "INTERRUPTED"
    assert interrupted.current_role == role
    assert interrupted.reason_code is None
    assert interrupted.diagnostic_id is None
    assert resumed.status == f"{role}_PENDING"
    assert resumed.current_role == role


def test_state_contract_forbids_partially_resolved_single_field_mutation() -> None:
    plan = _plan()
    specialist, reviewer, consensus = _evaluations(plan)
    terminal = finalize_reviewer_consensus_v2(
        state=accept_specialist_evaluation_v2(
            state=_start(plan),
            evaluation=specialist,
        ),
        plan=plan,
        reviewer_evaluation=reviewer,
        consensus=consensus,
    )
    mutated = terminal.model_copy(update={"status": "PARTIALLY_RESOLVED"})

    with pytest.raises(ValidationError):
        MethodStateV2.model_validate(mutated.model_dump(mode="python"))


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
def test_pending_state_rejects_protocol_count_two_single_field_mutation(
    role: str,
) -> None:
    plan = _plan()
    pending = (
        _start(plan)
        if role == "SPECIALIST"
        else _reviewer_pending(plan)
    )
    field = f"{role.lower()}_protocol_failures"
    mutated = pending.model_copy(update={field: 2})

    with pytest.raises(ValidationError, match="at most one protocol repair"):
        MethodStateV2.model_validate(mutated.model_dump(mode="python"))
