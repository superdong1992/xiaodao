from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    Case,
    CaseStatus,
    ErrorCode,
    DiagnosisOutcomeTriggerPayload,
    ExecutionFailedTriggerPayload,
    ExecutionFailure,
    ExecutionStage,
    JobStatus,
    OutcomeDisposition,
    ReviewOutcomeTriggerPayload,
    RouteOutcomeTriggerPayload,
    TriggerType,
    validate_transition_plan_for_outcome,
)
from problem_locator.application.formalization import apply_case_failure_update
from problem_locator.application.projection import project_case_components
from problem_locator.contracts.enums import MethodsValidationReasonCode
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnose_job,
    failure_outcome,
    rebuild,
    review_job,
    route_job,
    snapshot_with_active,
    trigger,
)


FATAL_CODES = (
    ErrorCode.CONTEXT_LIMIT,
    ErrorCode.ASSET_VERSION_UNAVAILABLE,
    ErrorCode.BACKEND_OUTPUT_LIMIT,
    ErrorCode.OUTCOME_MISSING,
    ErrorCode.OUTCOME_INVALID,
    ErrorCode.WORKSPACE_LIMIT,
    ErrorCode.LOGPARSE_OUTPUT_INVALID,
    ErrorCode.CONFIG_INVALID,
    ErrorCode.RESOURCE_NOT_FOUND,
    ErrorCode.RESOURCE_HASH_MISMATCH,
    ErrorCode.RESOURCE_SIZE_MISMATCH,
    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
    ErrorCode.PATH_VIOLATION,
)

CONDITIONAL_CODES = (
    ErrorCode.BACKEND_START_FAILED,
    ErrorCode.BACKEND_CANCELLED,
    ErrorCode.BACKEND_TIMEOUT,
    ErrorCode.BACKEND_EXIT_FAILED,
    ErrorCode.WORKSPACE_PREPARE_FAILED,
    ErrorCode.RESOURCE_STAGE_FAILED,
    ErrorCode.EXECUTION_RECORD_FAILED,
    ErrorCode.LOGPARSE_FAILED,
)


def _plan(code: ErrorCode, retryable: bool) -> object:
    source = route_job()
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.EXECUTION_FAILED,
        payload=ExecutionFailedTriggerPayload(
            source_job_id=source.job_id,
            source_outcome_id=None,
            execution_failure=ExecutionFailure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=code,
                message=f"{code.value} during the fixed execution.",
                retryable=retryable,
                details=[],
            ),
        ),
    )
    return DomainCoordinator().plan(snapshot, request)


@pytest.mark.parametrize("code", FATAL_CODES)
def test_every_fatal_execution_code_fails_the_case(code: ErrorCode) -> None:
    plan = _plan(code, retryable=False)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "FAILED"
    assert plan.job_updates[0].target_status is JobStatus.FAILED
    assert plan.case_failure_update is not None
    assert plan.case_failure_update.value is not None
    assert plan.case_failure_update.value.code is code
    assert plan.case_failure_update.value.source_outcome_id is None


@pytest.mark.parametrize("code", CONDITIONAL_CODES)
def test_every_retryable_conditional_code_interrupts(code: ErrorCode) -> None:
    plan = _plan(code, retryable=True)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "INTERRUPTED"
    assert plan.job_updates[0].target_status is JobStatus.INTERRUPTED
    assert plan.case_failure_update is None


@pytest.mark.parametrize("code", CONDITIONAL_CODES)
def test_every_non_retryable_conditional_code_fails(code: ErrorCode) -> None:
    plan = _plan(code, retryable=False)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "FAILED"
    assert plan.job_updates[0].target_status is JobStatus.FAILED
    assert plan.case_failure_update is not None


def test_non_execution_error_code_is_rejected_by_the_domain_boundary() -> None:
    result = _plan(ErrorCode.IDEMPOTENCY_CONFLICT, retryable=False)

    assert isinstance(result, ApplicationError)
    assert result.code is ErrorCode.VALIDATION_ERROR
    assert result.retryable is False


def test_methods_failure_contract_reaches_the_public_case_projection() -> None:
    source = route_job()
    snapshot = snapshot_with_active(source)
    diagnostic_id = "00000000-0000-4000-8000-000000000777"
    failure = ExecutionFailure(
        stage=ExecutionStage.OUTCOME_VALIDATE,
        code=ErrorCode.OUTCOME_INVALID,
        message="Methods diagnosis draft is not grounded in the frozen inputs.",
        retryable=False,
        details=[],
        reason_code=MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING,
        diagnostic_id=diagnostic_id,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.EXECUTION_FAILED,
        payload=ExecutionFailedTriggerPayload(
            source_job_id=snapshot.active_job.job_id,
            source_outcome_id=None,
            execution_failure=failure,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.case_failure_update is not None
    case_failure = plan.case_failure_update.value
    assert case_failure is not None
    assert (
        case_failure.reason_code
        is MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING
    )
    assert case_failure.diagnostic_id == diagnostic_id
    applied_failure = apply_case_failure_update(
        snapshot.case.failure,
        plan.case_failure_update,
    )
    assert applied_failure == case_failure
    failed_case = Case.model_validate(
        {
            **snapshot.case.model_dump(mode="python"),
            "status": CaseStatus.FAILED,
            "case_revision": snapshot.case.case_revision + 1,
            "active_job_id": None,
            "failure": applied_failure,
            "updated_at": request.occurred_at,
        }
    )
    view = project_case_components(failed_case, None, [])
    assert view.failure is not None
    assert (
        view.failure.reason_code
        is MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING
    )
    assert view.failure.diagnostic_id == diagnostic_id


@pytest.mark.parametrize(
    ("source_factory", "trigger_type", "payload_type"),
    [
        (route_job, TriggerType.ROUTE_OUTCOME, RouteOutcomeTriggerPayload),
        (
            diagnose_job,
            TriggerType.DIAGNOSIS_OUTCOME,
            DiagnosisOutcomeTriggerPayload,
        ),
        (review_job, TriggerType.REVIEW_OUTCOME, ReviewOutcomeTriggerPayload),
    ],
)
@pytest.mark.parametrize(
    ("retryable", "expected_case", "expected_job"),
    [
        (True, "INTERRUPTED", JobStatus.INTERRUPTED),
        (False, "FAILED", JobStatus.FAILED),
    ],
)
def test_failed_outcome_uses_the_same_closed_failure_matrix_for_every_job_type(
    source_factory: object,
    trigger_type: TriggerType,
    payload_type: object,
    retryable: bool,
    expected_case: str,
    expected_job: JobStatus,
) -> None:
    source = source_factory()
    snapshot = snapshot_with_active(source)
    base = failure_outcome()
    failure = ExecutionFailure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT,
        message="The fixed execution timed out.",
        retryable=retryable,
        details=[],
    )
    outcome = rebuild(
        base,
        job_id=source.job_id,
        case_id=source.case_id,
        job_type=source.job_type,
        base_state_revision=source.base_state_revision,
        error=failure,
        produced_at="2026-07-31T00:03:00.000Z",
    )
    request = trigger(
        snapshot,
        trigger_type=trigger_type,
        payload=payload_type(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == expected_case
    assert plan.job_updates[0].target_status is expected_job
    assert plan.outcome_disposition is OutcomeDisposition.APPLIED
    if retryable:
        assert plan.case_failure_update is None
    else:
        assert plan.case_failure_update is not None
        assert plan.case_failure_update.value is not None
        assert plan.case_failure_update.value.source_outcome_id == outcome.outcome_id
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
