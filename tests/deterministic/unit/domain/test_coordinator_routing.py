from __future__ import annotations

import hashlib

import pytest

from problem_locator.contracts import (
    ApplicationError,
    CancelCaseTriggerPayload,
    Case,
    CaseSnapshot,
    CaseStatus,
    CreateCaseTriggerPayload,
    DiagnosisMode,
    DiagnosisOutcomeTriggerPayload,
    EvidenceProposal,
    EvidenceSourceType,
    ErrorCode,
    FieldUpdateAction,
    GenericDiagnosisOutcome,
    GenericDiagnosisOutcomeV2,
    GenericResultStatus,
    Job,
    JobStatus,
    JobType,
    OutcomeResultType,
    RouteDecision,
    RouteKind,
    RouteOutcomeTriggerPayload,
    ResumeInterruptedTriggerPayload,
    TriggerType,
    canonical_json_bytes,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnosis_outcome,
    diagnose_job,
    rebuild,
    route_job,
    route_outcome,
    running,
    runtime_bindings,
    snapshot_with_active,
    state_from_job,
    trigger,
)


def test_create_case_builds_one_empty_route_job() -> None:
    job = route_job()
    state = state_from_job(job)
    case = Case(
        case_id=job.case_id,
        status=CaseStatus.NEW,
        case_revision=1,
        raw_problem_text=state.problem_spec.statement,
        diagnosis_state=state,
        active_job_id=None,
        selected_skill_ref=None,
        final_result=None,
        failure=None,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at="2026-07-31T00:00:00.000Z",
    )
    snapshot = CaseSnapshot(
        case=case,
        active_job=None,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.CREATE_CASE,
        payload=CreateCaseTriggerPayload(
            raw_problem_text=state.problem_spec.statement,
            problem_spec=state.problem_spec,
            initial_user_facts=state.user_facts,
        ),
        bindings={JobType.ROUTE: runtime_bindings(job)},
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.RUNNING
    assert plan.job_updates == []
    assert plan.clear_active_job is False
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.ROUTE
    assert plan.next_job_spec.target_state_revision == 1
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.attachment_refs == []
    assert plan.next_job_spec.artifact_bindings == []
    assert plan.next_job_spec.previous_outcome_refs == []


def test_route_match_sets_the_fixed_skill_and_starts_diagnosis() -> None:
    source = route_job()
    outcome = route_outcome()
    snapshot = snapshot_with_active(source)
    target = diagnose_job()
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(target)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    first = DomainCoordinator().plan(snapshot, request)
    second = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(first, ApplicationError)
    assert not isinstance(second, ApplicationError)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first.target_case_status is CaseStatus.RUNNING
    assert first.job_updates[0].target_status is JobStatus.SUCCEEDED
    assert first.selected_skill_update is not None
    assert first.selected_skill_update.action is FieldUpdateAction.SET
    assert first.selected_skill_update.value == outcome.payload.skill_ref
    assert first.next_job_spec is not None
    assert first.next_job_spec.job_type is JobType.DIAGNOSE
    assert first.next_job_spec.skill_ref == outcome.payload.skill_ref
    assert first.next_job_spec.target_state_revision == snapshot.case.diagnosis_state.revision
    assert validate_transition_plan_for_outcome(first, outcome) is first


def test_route_no_capability_starts_isolated_generic_diagnosis() -> None:
    source = route_job()
    snapshot = snapshot_with_active(source)
    base = route_outcome()
    reason = "No frozen Diagnosis Skill covers the target system."
    outcome = rebuild(
        base,
        result_type=OutcomeResultType.NO_CAPABILITY,
        payload=RouteDecision(
            kind=RouteKind.NO_CAPABILITY,
            skill_ref=None,
            reason=reason,
            confidence=0.95,
        ),
    )
    generic_bindings = rebuild(
        runtime_bindings(diagnose_job()),
        diagnosis_mode="GENERIC",
        review_policy=None,
        generic_skill_name="generic-problem-locator-smoke",
        skill_ref=None,
        logparse_tool_ref=None,
        logparse_product=None,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: generic_bindings},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.RUNNING
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.DIAGNOSE
    assert plan.next_job_spec.diagnosis_mode.value == "GENERIC"
    assert plan.next_job_spec.generic_skill_name == "generic-problem-locator-smoke"
    assert plan.next_job_spec.generic_problem_text == snapshot.case.raw_problem_text
    assert plan.next_job_spec.skill_ref is None
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.attachment_refs == []
    assert plan.next_job_spec.previous_outcome_refs == []
    assert plan.next_job_spec.artifact_bindings == []
    assert plan.selected_skill_update is not None
    assert plan.selected_skill_update.action is FieldUpdateAction.CLEAR
    assert plan.case_failure_update is None
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_no_match_after_reroute_discards_specialized_history_for_generic_job() -> None:
    reroute_source_outcome = diagnosis_outcome()
    source = rebuild(
        route_job(),
        previous_outcome_refs=[reroute_source_outcome.outcome_id],
    )
    snapshot = snapshot_with_active(source)
    outcome = rebuild(
        route_outcome(),
        result_type=OutcomeResultType.NO_CAPABILITY,
        payload=RouteDecision(
            kind=RouteKind.NO_CAPABILITY,
            skill_ref=None,
            reason="Rerouting found no semantically matching specialized Skill.",
            confidence=0.88,
        ),
    )
    generic_bindings = rebuild(
        runtime_bindings(diagnose_job()),
        diagnosis_mode=DiagnosisMode.GENERIC,
        review_policy=None,
        generic_skill_name="generic-problem-locator-smoke",
        skill_ref=None,
        logparse_tool_ref=None,
        logparse_product=None,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: generic_bindings},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert source.previous_outcome_refs == [reroute_source_outcome.outcome_id]
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.diagnosis_mode is DiagnosisMode.GENERIC
    assert plan.next_job_spec.previous_outcome_refs == []
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.attachment_refs == []
    assert plan.next_job_spec.artifact_bindings == []


def _generic_diagnose_job() -> Job:
    specialized = diagnose_job()
    payload = specialized.model_dump(mode="python")
    payload.update(
        diagnosis_mode=DiagnosisMode.GENERIC,
        review_policy=None,
        generic_skill_name="generic-problem-locator-smoke",
        generic_problem_text="原始多行问题\n第二行保持不变",
        context_snapshot=None,
        evidence_refs=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_refs=[],
        skill_ref=None,
        logparse_tool_ref=None,
        logparse_product=None,
    )
    return Job.model_validate(payload)


def _generic_snapshot(job: Job) -> CaseSnapshot:
    state = state_from_job(diagnose_job())
    active = running(job)
    return CaseSnapshot(
        case=Case(
            case_id=job.case_id,
            status=CaseStatus.RUNNING,
            case_revision=7,
            raw_problem_text=job.generic_problem_text,
            diagnosis_state=state,
            active_job_id=job.job_id,
            selected_skill_ref=None,
            final_result=None,
            unresolved_result=None,
            generic_result=None,
            failure=None,
            created_at="2026-07-31T00:00:00.000Z",
            updated_at="2026-07-31T00:03:00.000Z",
        ),
        active_job=active,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )


@pytest.mark.parametrize(
    ("generic_status", "case_status"),
    [
        (GenericResultStatus.RESOLVED, CaseStatus.RESOLVED),
        (GenericResultStatus.UNRESOLVED, CaseStatus.UNRESOLVED),
    ],
)
def test_generic_diagnosis_result_becomes_terminal_without_review(
    generic_status: GenericResultStatus,
    case_status: CaseStatus,
) -> None:
    job = _generic_diagnose_job()
    snapshot = _generic_snapshot(job)
    outcome = rebuild(
        diagnosis_outcome(),
        result_type=OutcomeResultType.COMPLETED,
        payload=GenericDiagnosisOutcome(
            status=generic_status,
            conclusion="通用定位结论",
            root_cause_analysis="通用定位文字版根因分析",
            skill_name="generic-problem-locator-smoke",
        ),
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        decision_audit=None,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is case_status
    assert plan.generic_result is not None
    assert plan.generic_result.status is generic_status
    assert plan.generic_result.source_job_id == job.job_id
    assert plan.generic_result.source_outcome_id == outcome.outcome_id
    assert plan.next_job_spec is None
    assert plan.candidate_mutation is None
    assert plan.final_result_target is None
    assert plan.unresolved_result_draft is None
    assert plan.accepted_evidence_proposal_keys == []
    assert plan.accepted_artifact_proposal_keys == []
    assert plan.selected_skill_update is not None
    assert plan.selected_skill_update.action is FieldUpdateAction.CLEAR
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


@pytest.mark.parametrize(
    ("generic_status", "case_status"),
    [
        (GenericResultStatus.RESOLVED, CaseStatus.RESOLVED),
        (GenericResultStatus.UNRESOLVED, CaseStatus.UNRESOLVED),
    ],
)
def test_generic_v2_markdown_result_becomes_a_terminal_artifact_draft(
    generic_status: GenericResultStatus,
    case_status: CaseStatus,
) -> None:
    job = _generic_diagnose_job()
    snapshot = _generic_snapshot(job)
    report = "# 定位报告\n\n| 项目 | 结论 |\n| --- | --- |\n| 状态 | 已确认 |\n\n```json\n{\"ok\":true}\n```\n"
    report_bytes = report.encode("utf-8")
    outcome = rebuild(
        diagnosis_outcome(),
        result_type=OutcomeResultType.COMPLETED,
        payload=GenericDiagnosisOutcomeV2(
            format_version=2,
            status=generic_status,
            report_markdown=report,
            report_utf8_size=len(report_bytes),
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            skill_name="generic-problem-locator-smoke",
        ),
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        decision_audit=None,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is case_status
    assert plan.generic_result is None
    assert plan.generic_result_v2_draft is not None
    assert plan.generic_result_v2_draft.status is generic_status
    assert plan.generic_result_v2_draft.report_markdown == report
    assert plan.generic_result_v2_draft.report_utf8_size == len(report_bytes)
    assert plan.generic_result_v2_draft.report_sha256 == hashlib.sha256(
        report_bytes
    ).hexdigest()
    assert plan.generic_result_v2_draft.source_job_id == job.job_id
    assert plan.generic_result_v2_draft.source_outcome_id == outcome.outcome_id
    assert plan.accepted_artifact_proposal_keys == []
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_generic_diagnosis_can_be_cancelled_without_specialized_side_effects() -> None:
    job = _generic_diagnose_job()
    snapshot = _generic_snapshot(job)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.CANCEL_CASE,
        payload=CancelCaseTriggerPayload(
            reason="USER_CANCEL",
            active_job_id=job.job_id,
        ),
        bindings={},
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.CANCELLED
    assert plan.next_job_spec is None
    assert plan.generic_result is None
    assert len(plan.job_updates) == 1
    assert plan.job_updates[0].target_status is JobStatus.CANCELLED


def test_interrupted_generic_diagnosis_resumes_with_exact_frozen_input() -> None:
    job = _generic_diagnose_job()
    source = rebuild(
        running(job),
        status=JobStatus.INTERRUPTED,
        finished_at="2026-07-31T00:00:20.000Z",
    )
    state = state_from_job(diagnose_job())
    snapshot = CaseSnapshot(
        case=Case(
            case_id=job.case_id,
            status=CaseStatus.INTERRUPTED,
            case_revision=7,
            raw_problem_text=job.generic_problem_text,
            diagnosis_state=state,
            active_job_id=None,
            selected_skill_ref=None,
            final_result=None,
            unresolved_result=None,
            generic_result=None,
            failure=None,
            created_at="2026-07-31T00:00:00.000Z",
            updated_at="2026-07-31T00:00:20.000Z",
        ),
        active_job=None,
        resume_source_job=source,
        replacement_job_ids_by_source={},
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.RESUME_INTERRUPTED,
        payload=ResumeInterruptedTriggerPayload(source_job_id=source.job_id),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=continuation(job=source),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.RUNNING
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.diagnosis_mode is DiagnosisMode.GENERIC
    assert plan.next_job_spec.generic_skill_name == source.generic_skill_name
    assert plan.next_job_spec.generic_problem_text == source.generic_problem_text
    assert plan.next_job_spec.replacement_for_job_id == source.job_id
    assert plan.next_job_spec.skill_ref is None
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.attachment_refs == []
    assert plan.next_job_spec.previous_outcome_refs == []
    assert plan.next_job_spec.artifact_bindings == []


def test_router_cannot_propose_domain_evidence() -> None:
    source = route_job()
    snapshot = snapshot_with_active(source)
    base = route_outcome()
    evidence = EvidenceProposal(
        proposal_key="router_evidence",
        source_type=EvidenceSourceType.PREVIOUS_OUTCOME,
        source_binding={
            "existing_source_ref": base.outcome_id,
            "artifact_proposal_key": None,
        },
        locator={
            "kind": "PREVIOUS_OUTCOME",
            "json_pointer": "/payload/reason",
        },
        summary="Router output is not domain Evidence.",
        content_hash=None,
        staged_resource_ref=None,
    )
    outcome = rebuild(base, proposed_evidence=[evidence])
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(diagnose_job())},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code is ErrorCode.VALIDATION_ERROR
