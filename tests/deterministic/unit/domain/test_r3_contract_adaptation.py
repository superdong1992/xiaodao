from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    ArtifactKind,
    ArtifactProposal,
    COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER,
    DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisStateDelta,
    DiagnosticExportMetadata,
    ErrorCode,
    EvidenceProposal,
    EvidenceSourceType,
    ExecutionFailedTriggerPayload,
    ExecutionStage,
    JobStatus,
    OutcomeResultType,
    ReviewOutcomeTriggerPayload,
    ResourceKind,
    RouteOutcomeTriggerPayload,
    StagedResourceRef,
    TriggerType,
    ValidatedTrigger,
    canonical_json_bytes,
    coordinator_outcome_error_failure,
    deterministic_outcome_failure,
    validate_coordinator_plan_result,
    validate_outcome_for_job,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnose_job,
    diagnosis_outcome,
    rebuild,
    review_job,
    review_outcome,
    route_job,
    route_outcome,
    snapshot_with_active,
    trigger,
)


EXPECTED_ERRORS_BY_TRIGGER = {
    TriggerType.CREATE_CASE: frozenset(
        {
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.ACTIVE_JOB_EXISTS,
            ErrorCode.VALIDATION_ERROR,
        }
    ),
    TriggerType.ROUTE_OUTCOME: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.DIAGNOSIS_OUTCOME: frozenset(
        {
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.NEW_CASE_REQUIRED,
            ErrorCode.VALIDATION_ERROR,
        }
    ),
    TriggerType.REVIEW_OUTCOME: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.SUBMIT_SUPPLEMENT: frozenset(
        {
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.ACTIVE_JOB_EXISTS,
            ErrorCode.NEW_CASE_REQUIRED,
            ErrorCode.VALIDATION_ERROR,
        }
    ),
    TriggerType.CANCEL_CASE: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.RESUME_INTERRUPTED: frozenset(
        {
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.ACTIVE_JOB_EXISTS,
            ErrorCode.VALIDATION_ERROR,
        }
    ),
    TriggerType.EXECUTION_FAILED: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.ASSET_VERSION_UNAVAILABLE: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.MARK_OLD_EPOCH_INTERRUPTED: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
    TriggerType.STALE_ACTIVE_OUTCOME: frozenset(
        {ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR}
    ),
}


def _application_error(code: ErrorCode) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=f"Deterministic {code.value} domain decision.",
        details=[],
        retryable=False,
    )


def _empty_delta(**changes: object) -> DiagnosisStateDelta:
    values: dict[str, object] = {
        "problem_spec_patch": None,
        "add_user_facts": [],
        "proposed_facts": [],
        "add_active_hypotheses": [],
        "update_hypotheses": [],
        "reject_hypotheses": [],
        "add_open_questions": [],
        "resolve_questions": [],
        "add_pending_requirements": [],
        "fulfill_requirements": [],
        "add_evidence_bindings": [],
    }
    values.update(changes)
    return DiagnosisStateDelta.model_validate(values)


def _unexpected_outcome_evidence(outcome_id: str) -> EvidenceProposal:
    return EvidenceProposal(
        proposal_key="unexpected_evidence",
        source_type=EvidenceSourceType.PREVIOUS_OUTCOME,
        source_binding={
            "existing_source_ref": outcome_id,
            "artifact_proposal_key": None,
        },
        locator={
            "kind": "PREVIOUS_OUTCOME",
            "json_pointer": "/payload",
        },
        summary="This finalized Outcome must be rejected semantically.",
        content_hash=None,
        staged_resource_ref=None,
    )


def _semantic_rejection_case(case_name: str):
    if case_name == "route_validation":
        source = route_job()
        base = route_outcome()
        digest = "a" * 64
        artifact = ArtifactProposal(
            proposal_key="unexpected_artifact",
            artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
            name="unexpected.json",
            content_type="application/json",
            resource_kind=ResourceKind.FILE,
            size=7,
            sha256=digest,
            staged_resource_ref=StagedResourceRef(
                staging_id="00000000-0000-0000-0000-000000000099",
                owner_job_id=source.job_id,
                proposal_key="unexpected_artifact",
                resource_kind=ResourceKind.FILE,
                size=7,
                sha256=digest,
                tree_manifest=None,
            ),
            metadata=DiagnosticExportMetadata(
                schema_version=1,
                format_id="diagnostic-export-v1",
                description="Unexpected Route artifact.",
            ),
        )
        outcome = rebuild(base, proposed_artifacts=[artifact])
        return (
            source,
            TriggerType.ROUTE_OUTCOME,
            RouteOutcomeTriggerPayload(job_outcome=outcome),
            outcome,
            ErrorCode.VALIDATION_ERROR,
        )
    if case_name == "review_validation":
        source = review_job()
        base = review_outcome()
        outcome = rebuild(
            base,
            proposed_evidence=[
                _unexpected_outcome_evidence(source.previous_outcome_refs[0])
            ],
        )
        return (
            source,
            TriggerType.REVIEW_OUTCOME,
            ReviewOutcomeTriggerPayload(job_outcome=outcome),
            outcome,
            ErrorCode.VALIDATION_ERROR,
        )

    source = diagnose_job()
    base = diagnosis_outcome()
    outcome = rebuild(
        base,
        result_type=OutcomeResultType.COMPLETED,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Do not create a zero-progress Job.",
        ),
        proposed_evidence=[],
        proposed_artifacts=[],
    )
    return (
        source,
        TriggerType.DIAGNOSIS_OUTCOME,
        DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        outcome,
        ErrorCode.VALIDATION_ERROR,
    )


def test_r3_trigger_specific_coordinator_error_sets_are_consumed_exactly() -> None:
    assert COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER == EXPECTED_ERRORS_BY_TRIGGER
    coordinator_codes = (
        ErrorCode.INVALID_CASE_STATE,
        ErrorCode.ACTIVE_JOB_EXISTS,
        ErrorCode.NEW_CASE_REQUIRED,
        ErrorCode.VALIDATION_ERROR,
    )
    for trigger_type, allowed_codes in EXPECTED_ERRORS_BY_TRIGGER.items():
        request = ValidatedTrigger.model_construct(
            trigger_type=trigger_type,
            payload=None,
        )
        for code in coordinator_codes:
            error = _application_error(code)
            if code in allowed_codes:
                assert validate_coordinator_plan_result(request, error) is error
            else:
                with pytest.raises(ValueError, match="allowed for this Trigger"):
                    validate_coordinator_plan_result(request, error)


@pytest.mark.parametrize(
    "case_name",
    [
        "route_validation",
        "diagnosis_validation",
        "review_validation",
    ],
)
def test_finalized_semantic_rejection_has_one_outcome_invalid_termination_plan(
    case_name: str,
) -> None:
    source, trigger_type, payload, outcome, expected_code = (
        _semantic_rejection_case(case_name)
    )
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=trigger_type,
        payload=payload,
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )
    before_snapshot = canonical_json_bytes(snapshot)
    before_request = canonical_json_bytes(request)

    assert validate_outcome_for_job(source, outcome) is outcome
    decision = DomainCoordinator().plan(snapshot, request)

    assert isinstance(decision, ApplicationError)
    assert decision.code is expected_code
    assert decision.retryable is False
    failure = coordinator_outcome_error_failure(request, decision)
    assert failure == deterministic_outcome_failure(
        ErrorCode.OUTCOME_INVALID,
        decision.details,
    )
    assert failure.stage is ExecutionStage.OUTCOME_VALIDATE
    assert failure.code is ErrorCode.OUTCOME_INVALID
    assert failure.message == "Job outcome validation failed."
    assert failure.retryable is False

    termination_trigger = trigger(
        snapshot,
        trigger_type=TriggerType.EXECUTION_FAILED,
        payload=ExecutionFailedTriggerPayload(
            source_job_id=source.job_id,
            source_outcome_id=outcome.outcome_id,
            execution_failure=failure,
        ),
        occurred_at=outcome.produced_at,
    )
    before_termination_trigger = canonical_json_bytes(termination_trigger)
    first_plan = DomainCoordinator().plan(snapshot, termination_trigger)
    second_plan = DomainCoordinator().plan(snapshot, termination_trigger)

    assert not isinstance(first_plan, ApplicationError)
    assert not isinstance(second_plan, ApplicationError)
    assert canonical_json_bytes(first_plan) == canonical_json_bytes(second_plan)
    assert first_plan.target_case_status.value == "FAILED"
    assert len(first_plan.job_updates) == 1
    assert first_plan.job_updates[0].job_id == source.job_id
    assert first_plan.job_updates[0].expected_status is JobStatus.RUNNING
    assert first_plan.job_updates[0].target_status is JobStatus.FAILED
    assert first_plan.outcome_disposition is None
    assert first_plan.accepted_state_delta == _empty_delta()
    assert first_plan.accepted_evidence_proposal_keys == []
    assert first_plan.accepted_artifact_proposal_keys == []
    assert first_plan.accepted_candidate_proposal_key is None
    assert first_plan.selected_skill_update is None
    assert first_plan.candidate_mutation is None
    assert first_plan.next_job_spec is None
    assert first_plan.final_result_target is None
    assert first_plan.clear_active_job is True
    assert first_plan.case_failure_update is not None
    case_failure = first_plan.case_failure_update.value
    assert case_failure is not None
    assert case_failure.code is ErrorCode.OUTCOME_INVALID
    assert case_failure.source_job_id == source.job_id
    assert case_failure.source_outcome_id == outcome.outcome_id
    assert case_failure.occurred_at == outcome.produced_at
    assert validate_transition_plan_for_outcome(first_plan, outcome) is first_plan
    assert canonical_json_bytes(snapshot) == before_snapshot
    assert canonical_json_bytes(request) == before_request
    assert canonical_json_bytes(termination_trigger) == before_termination_trigger


@pytest.mark.parametrize(
    "trigger_type",
    [
        TriggerType.ROUTE_OUTCOME,
        TriggerType.DIAGNOSIS_OUTCOME,
        TriggerType.REVIEW_OUTCOME,
    ],
)
def test_invalid_case_state_is_reserved_for_stale_not_terminal_normalization(
    trigger_type: TriggerType,
) -> None:
    request = ValidatedTrigger.model_construct(
        trigger_type=trigger_type,
        payload=None,
    )
    error = _application_error(ErrorCode.INVALID_CASE_STATE)

    assert validate_coordinator_plan_result(request, error) is error
    with pytest.raises(ValueError, match="STALE Outcome path"):
        coordinator_outcome_error_failure(request, error)


def test_non_outcome_trigger_cannot_enter_finalized_semantic_rejection_path() -> None:
    request = ValidatedTrigger.model_construct(
        trigger_type=TriggerType.CANCEL_CASE,
        payload=None,
    )
    error = _application_error(ErrorCode.VALIDATION_ERROR)

    with pytest.raises(ValueError, match="finalized Outcome Trigger"):
        coordinator_outcome_error_failure(request, error)
