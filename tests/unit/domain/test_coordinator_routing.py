from __future__ import annotations

from problem_locator.contracts import (
    ApplicationError,
    Case,
    CaseSnapshot,
    CaseStatus,
    CreateCaseTriggerPayload,
    EvidenceProposal,
    EvidenceSourceType,
    ErrorCode,
    FieldUpdateAction,
    JobStatus,
    JobType,
    OutcomeResultType,
    RouteDecision,
    RouteKind,
    RouteOutcomeTriggerPayload,
    TriggerType,
    canonical_json_bytes,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnose_job,
    rebuild,
    route_job,
    route_outcome,
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


def test_route_no_capability_fails_without_a_fallback_agent() -> None:
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
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.FAILED
    assert plan.next_job_spec is None
    assert plan.selected_skill_update is not None
    assert plan.selected_skill_update.action is FieldUpdateAction.CLEAR
    assert plan.case_failure_update is not None
    assert plan.case_failure_update.value is not None
    assert plan.case_failure_update.value.code is ErrorCode.NO_CAPABILITY
    assert plan.case_failure_update.value.message == reason
    assert plan.case_failure_update.value.source_job_id == source.job_id
    assert plan.case_failure_update.value.source_outcome_id == outcome.outcome_id
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


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
