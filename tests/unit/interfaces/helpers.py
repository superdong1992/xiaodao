from __future__ import annotations

from problem_locator.contracts.commands import (
    ApplicationResponse,
    ArtifactSummary,
    BusinessReceipt,
    CaseView,
)
from problem_locator.contracts.enums import (
    ArtifactKind,
    CaseStatus,
    ErrorCode,
    ResourceKind,
)
from problem_locator.contracts.limits import CONTRACT_REVISION, SCHEMA_VERSION
from problem_locator.contracts.models import (
    ProblemSpec,
    ReadinessCheck,
    ReadinessReport,
    StateExportObjectCounts,
    ValidationIssue,
    ValidationReport,
)


CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000002"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000003"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000004"
FIXED_TIME = "2026-07-31T12:00:00.000Z"
SHA256_A = "a" * 64


def problem_spec_input() -> dict[str, object]:
    return {
        "statement": "RPC request times out.",
        "expected_behavior": "The RPC succeeds within the deadline.",
        "actual_behavior": "The caller reports a timeout.",
        "scope": "Payment to inventory RPC.",
        "goals": ["Locate the timeout cause."],
        "non_goals": [],
        "constraints": ["Use only supplied evidence."],
        "completion_criteria": ["Identify an evidenced cause."],
    }


def case_view(*, revision: int = 1, artifacts: list[ArtifactSummary] | None = None) -> CaseView:
    return CaseView(
        case_id=CASE_ID,
        status=CaseStatus.WAITING_INPUT,
        case_revision=revision,
        diagnosis_state_revision=1,
        problem_spec=ProblemSpec(revision=1, **problem_spec_input()),
        user_facts=[],
        confirmed_facts=[],
        open_questions=[],
        pending_requirements=[],
        active_job=None,
        selected_skill_ref=None,
        final_result=None,
        failure=None,
        artifacts=[] if artifacts is None else artifacts,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )


def application_response(
    *,
    operation: str = "CreateCase",
    primary_resource_id: str = CASE_ID,
    revision: int = 1,
    with_case_view: bool = True,
) -> ApplicationResponse:
    return ApplicationResponse(
        business_receipt=BusinessReceipt(
            operation=operation,
            primary_resource_id=primary_resource_id,
            case_id=CASE_ID,
            case_revision=revision,
            job_id=None,
            status="ACCEPTED",
        ),
        case_view=case_view(revision=revision) if with_case_view else None,
        wait_timed_out=False,
        dispatch_pending=False,
    )


def artifact_summary(*, size: int = 7, sha256: str = SHA256_A) -> ArtifactSummary:
    return ArtifactSummary(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="diagnostic.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=size,
        sha256=sha256,
        created_by_job_id=JOB_ID,
        created_at=FIXED_TIME,
        downloadable=True,
    )


def object_counts() -> StateExportObjectCounts:
    return StateExportObjectCounts(
        cases=0,
        jobs=0,
        outcomes=0,
        outcome_processing_records=0,
        recovery_processing_records=0,
        execution_failure_records=0,
        attachments=0,
        evidence=0,
        artifacts=0,
        idempotency_records=0,
        runtime_epochs=0,
    )


def valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        schema_version=SCHEMA_VERSION,
        contract_revision=CONTRACT_REVISION,
        generation=0,
        object_counts=object_counts(),
        errors=[],
    )


def invalid_report(
    error_code: ErrorCode = ErrorCode.STATE_CORRUPT,
) -> ValidationReport:
    return ValidationReport(
        valid=False,
        schema_version=None,
        contract_revision=None,
        generation=None,
        object_counts=object_counts(),
        errors=[
            ValidationIssue(
                code=error_code.value,
                object_type="StateFile",
                object_id=None,
                field_path=None,
                message="State validation failed.",
            )
        ],
    )


def readiness(
    *,
    ready: bool = True,
    error_code: ErrorCode = ErrorCode.STATE_CORRUPT,
) -> ReadinessReport:
    checks = [
        ReadinessCheck(name=name, passed=ready, message=None)
        for name in ("CONFIG", "INSTANCE_LOCK", "STATE", "DATA_DIRECTORIES", "RECOVERY")
    ]
    if ready:
        return ReadinessReport(ready=True, checks=checks, error=None)
    from problem_locator.contracts.models import ApplicationError

    return ReadinessReport(
        ready=False,
        checks=checks,
        error=ApplicationError(
            code=error_code,
            message="State validation failed.",
            details=[],
            retryable=False,
        ),
    )
