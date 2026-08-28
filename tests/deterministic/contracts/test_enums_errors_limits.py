from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from problem_locator.contracts import enums, limits
from problem_locator.contracts.errors import (
    APPLICATION_ERROR_RETRYABLE_CODES,
    CLI_EXIT_BY_ERROR_CODE,
    ERROR_CODES,
    ERROR_SPECS,
    EXECUTION_FAILURE_RETRYABLE_CODES,
)

from tests.deterministic.contracts._support import enum_values, public_value


ENUM_EXPECTATIONS = {
    "ArtifactKind": (
        "USER_RESULT",
        "USER_RESULT_ARCHIVE",
        "GENERIC_REPORT",
        "DIAGNOSTIC_EXPORT",
        "LOGPARSE_RUN",
        "AUDIT_BUNDLE",
    ),
    "AssetKind": (
        "AGENT_PROFILE",
        "DIAGNOSIS_SKILL",
        "TOOL_BUNDLE",
        "CONTEXT_POLICY",
        "OUTPUT_CONTRACT",
        "LOGPARSE_TOOL",
    ),
    "AttachmentStatus": ("UPLOADING", "READY", "FAILED"),
    "CancellationReason": ("USER_CANCEL", "SERVICE_SHUTDOWN"),
    "CandidateMutationAction": ("INSTALL", "SET_STATUS"),
    "CandidateStatus": ("PROPOSED", "REVIEWING", "REJECTED", "ACCEPTED"),
    "CausalFactorRole": ("CAUSE", "CONTRIBUTOR", "CONDITION"),
    "CaseStatus": (
        "NEW",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_ATTACHMENT",
        "REVIEWING",
        "RESOLVED",
        "PARTIALLY_RESOLVED",
        "UNRESOLVED",
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    ),
    "ContextSectionKind": (
        "PROFILE",
        "SKILL",
        "SKILL_INDEX",
        "TOOL_BUNDLE",
        "JOB_INSTRUCTION",
        "CONTEXT_SNAPSHOT",
        "OPEN_REQUIREMENTS",
        "REVIEW_TARGET",
        "OUTPUT_CONTRACT",
        "PREVIOUS_OUTCOME",
        "EVIDENCE",
        "RESOURCE_MANIFEST",
        "REVIEW_SUBJECT",
        "RESOLVED_LOGPARSE_PLAN",
    ),
    "DiagnosisItemStatus": ("ACTIVE", "RESOLVED", "REJECTED", "SUPERSEDED"),
    "DiagnosisMode": ("SPECIALIZED", "GENERIC"),
    "DiagnosisResolutionStatus": ("COMPLETE", "PARTIAL"),
    "DiagnosisProvenanceType": ("USER_INPUT", "AGENT_OUTCOME"),
    "CompletionCriterionStatus": (
        "SATISFIED",
        "PARTIALLY_SATISFIED",
        "UNSATISFIED",
        "UNKNOWN",
    ),
    "EvidenceSourceType": (
        "USER_FACT",
        "ATTACHMENT",
        "LOGPARSE",
        "TOOL_OUTPUT",
        "PREVIOUS_OUTCOME",
    ),
    "ExecutionStage": (
        "ASSET_RESOLUTION",
        "CONTEXT_BUILD",
        "WORKSPACE_PREPARE",
        "BACKEND_START",
        "BACKEND_EXECUTE",
        "TOOL_EXECUTE",
        "OUTCOME_VALIDATE",
        "RESOURCE_STAGE",
        "EXECUTION_RECORD",
    ),
    "FailureReportDisposition": ("APPLIED", "DUPLICATE", "STALE"),
    "FieldUpdateAction": ("SET", "CLEAR"),
    "GenericResultStatus": ("RESOLVED", "UNRESOLVED"),
    "JobStatus": (
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    ),
    "JobType": ("ROUTE", "DIAGNOSE", "REVIEW"),
    "MethodsValidationReasonCode": (
        "METHOD_EVIDENCE_MARKER_NOT_INDEXED",
        "METHOD_CONFIRMED_EVIDENCE_MISSING",
        "METHOD_CONFIRMED_MARKER_SCAN_MISS",
        "METHOD_EVIDENCE_SOURCE_CHANGED",
        "METHOD_VALIDATION_FAILED",
    ),
    "OutcomeDisposition": ("APPLIED", "DUPLICATE", "STALE", "REJECTED"),
    "OutcomeResultType": (
        "COMPLETED",
        "NEED_INPUT",
        "NEED_ATTACHMENT",
        "REROUTE",
        "NO_CAPABILITY",
        "FAILED",
        "INCONCLUSIVE",
    ),
    "RequirementKind": ("INPUT", "ATTACHMENT"),
    "RequirementStatus": ("OPEN", "FULFILLED"),
    "RuleClaimResult": ("PASS", "FAIL", "UNKNOWN"),
    "ResourceKind": ("FILE", "DIRECTORY"),
    "ReviewVerdict": ("PASS", "NEED_MORE_EVIDENCE", "REJECT"),
    "RouteKind": ("MATCHED", "NO_CAPABILITY"),
    "ServerRuleStatus": (
        "VERIFIED_PASS",
        "VERIFIED_FAIL",
        "UNVERIFIABLE",
        "SEMANTIC_ONLY",
        "NOT_APPLICABLE",
    ),
    "SupplementPolicy": ("NONE", "MISSING_ONLY"),
    "TriggerType": (
        "CREATE_CASE",
        "ROUTE_OUTCOME",
        "DIAGNOSIS_OUTCOME",
        "REVIEW_OUTCOME",
        "SUBMIT_SUPPLEMENT",
        "CANCEL_CASE",
        "RESUME_INTERRUPTED",
        "EXECUTION_FAILED",
        "ASSET_VERSION_UNAVAILABLE",
        "MARK_OLD_EPOCH_INTERRUPTED",
        "STALE_ACTIVE_OUTCOME",
    ),
    "WorkspaceInputKind": (
        "ATTACHMENT",
        "EVIDENCE",
        "ARTIFACT",
        "PREVIOUS_OUTCOME",
    ),
    "UnresolvedReasonCode": (
        "MECHANICAL_VERIFICATION_FAILED",
        "INSUFFICIENT_EVIDENCE",
        "SEMANTIC_REVIEW_REJECTED",
        "INVALID_NEED_MORE_REQUEST",
    ),
}


ERROR_HTTP_STATUS = {
    "ACTIVE_JOB_EXISTS": 409,
    "ARTIFACT_NOT_FOUND": 404,
    "ASSET_VERSION_UNAVAILABLE": 422,
    "ATTACHMENT_NOT_FOUND": 404,
    "ATTACHMENT_NOT_READY": 409,
    "BACKEND_CANCELLED": 409,
    "BACKEND_EXIT_FAILED": 502,
    "BACKEND_OUTPUT_LIMIT": 422,
    "BACKEND_START_FAILED": 500,
    "BACKEND_TIMEOUT": 504,
    "CASE_NOT_FOUND": 404,
    "CLAIM_REJECTED": 409,
    "CONFIG_INVALID": 500,
    "CONTEXT_LIMIT": 422,
    "DISPATCH_REJECTED": 503,
    "EXECUTION_RECORD_FAILED": 500,
    "IDEMPOTENCY_CONFLICT": 409,
    "INSTANCE_LOCKED": 503,
    "INVALID_CASE_STATE": 409,
    "JOB_CASE_MISMATCH": 409,
    "JOB_NOT_FOUND": 404,
    "LOGPARSE_FAILED": 422,
    "LOGPARSE_OUTPUT_INVALID": 422,
    "NEW_CASE_REQUIRED": 409,
    "NO_CAPABILITY": 422,
    "OUTCOME_INVALID": 422,
    "OUTCOME_MISSING": 422,
    "PATH_VIOLATION": 400,
    "RESOURCE_CASE_MISMATCH": 409,
    "RESOURCE_HASH_MISMATCH": 422,
    "RESOURCE_LIMIT_EXCEEDED": 413,
    "RESOURCE_NOT_FOUND": 500,
    "RESOURCE_PUBLISH_FAILED": 500,
    "RESOURCE_SIZE_MISMATCH": 422,
    "RESOURCE_STAGE_FAILED": 500,
    "REVISION_CONFLICT": 409,
    "STATE_CORRUPT": 503,
    "STATE_SCHEMA_UNSUPPORTED": 503,
    "STATE_WRITE_FAILED": 500,
    "UPLOAD_INCOMPLETE": 409,
    "VALIDATION_ERROR": 400,
    "WORKSPACE_LIMIT": 422,
    "WORKSPACE_PREPARE_FAILED": 500,
}

ERROR_CLI_EXIT = {
    **{
        code: 2
        for code in (
            "ACTIVE_JOB_EXISTS",
            "ARTIFACT_NOT_FOUND",
            "ATTACHMENT_NOT_FOUND",
            "ATTACHMENT_NOT_READY",
            "CASE_NOT_FOUND",
            "CLAIM_REJECTED",
            "IDEMPOTENCY_CONFLICT",
            "INVALID_CASE_STATE",
            "JOB_CASE_MISMATCH",
            "JOB_NOT_FOUND",
            "NEW_CASE_REQUIRED",
            "NO_CAPABILITY",
            "PATH_VIOLATION",
            "RESOURCE_CASE_MISMATCH",
            "RESOURCE_HASH_MISMATCH",
            "RESOURCE_LIMIT_EXCEEDED",
            "RESOURCE_SIZE_MISMATCH",
            "REVISION_CONFLICT",
            "UPLOAD_INCOMPLETE",
            "VALIDATION_ERROR",
        )
    },
    **{
        code: 3
        for code in (
            "CONFIG_INVALID",
            "INSTANCE_LOCKED",
            "RESOURCE_NOT_FOUND",
            "STATE_CORRUPT",
            "STATE_SCHEMA_UNSUPPORTED",
        )
    },
    **{
        code: 4
        for code in (
            "ASSET_VERSION_UNAVAILABLE",
            "BACKEND_CANCELLED",
            "BACKEND_EXIT_FAILED",
            "BACKEND_OUTPUT_LIMIT",
            "BACKEND_START_FAILED",
            "BACKEND_TIMEOUT",
            "CONTEXT_LIMIT",
            "DISPATCH_REJECTED",
            "EXECUTION_RECORD_FAILED",
            "LOGPARSE_FAILED",
            "LOGPARSE_OUTPUT_INVALID",
            "OUTCOME_INVALID",
            "OUTCOME_MISSING",
            "RESOURCE_PUBLISH_FAILED",
            "RESOURCE_STAGE_FAILED",
            "STATE_WRITE_FAILED",
            "WORKSPACE_LIMIT",
            "WORKSPACE_PREPARE_FAILED",
        )
    },
}

EXPECTED_EXECUTION_RETRYABLE = {
    "BACKEND_START_FAILED",
    "BACKEND_CANCELLED",
    "BACKEND_TIMEOUT",
    "BACKEND_EXIT_FAILED",
    "WORKSPACE_PREPARE_FAILED",
    "RESOURCE_STAGE_FAILED",
    "EXECUTION_RECORD_FAILED",
    "LOGPARSE_FAILED",
}

EXPECTED_APPLICATION_RETRYABLE = {
    "REVISION_CONFLICT",
    "ATTACHMENT_NOT_READY",
    "UPLOAD_INCOMPLETE",
    "DISPATCH_REJECTED",
    "INSTANCE_LOCKED",
    "STATE_WRITE_FAILED",
    "RESOURCE_PUBLISH_FAILED",
}


def _code(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _spec_http_status(spec: Any) -> int:
    if isinstance(spec, Mapping):
        for key in ("http_status", "http_status_code"):
            if key in spec:
                return int(spec[key])
    for name in ("http_status", "http_status_code"):
        if hasattr(spec, name):
            return int(getattr(spec, name))
    raise AssertionError(f"error spec has no HTTP status: {spec!r}")


def test_all_enum_values_are_frozen_in_declaration_order() -> None:
    for enum_name, expected in ENUM_EXPECTATIONS.items():
        assert enum_values(getattr(enums, enum_name)) == expected


def test_stale_is_an_outcome_disposition_never_a_job_status() -> None:
    assert "STALE" in enum_values(enums.OutcomeDisposition)
    assert "STALE" not in enum_values(enums.JobStatus)


def test_error_code_universe_and_http_mapping_are_exact() -> None:
    codes = {_code(code) for code in ERROR_CODES}
    assert codes == set(ERROR_HTTP_STATUS)
    normalized_specs = {_code(code): spec for code, spec in ERROR_SPECS.items()}
    assert set(normalized_specs) == set(ERROR_HTTP_STATUS)
    assert {
        code: _spec_http_status(spec) for code, spec in normalized_specs.items()
    } == ERROR_HTTP_STATUS


def test_every_error_code_has_one_frozen_cli_exit_mapping() -> None:
    assert set(ERROR_CLI_EXIT) == set(ERROR_HTTP_STATUS)
    assert {_code(code): value for code, value in CLI_EXIT_BY_ERROR_CODE.items()} == (
        ERROR_CLI_EXIT
    )
    assert {
        _code(code): spec.cli_exit_code for code, spec in ERROR_SPECS.items()
    } == ERROR_CLI_EXIT


def test_retryability_sets_do_not_grow_silently() -> None:
    assert {_code(code) for code in EXECUTION_FAILURE_RETRYABLE_CODES} == (
        EXPECTED_EXECUTION_RETRYABLE
    )
    assert {_code(code) for code in APPLICATION_ERROR_RETRYABLE_CODES} == (
        EXPECTED_APPLICATION_RETRYABLE
    )


@pytest.mark.parametrize("model_name", ["ExecutionFailure", "CaseFailure"])
@pytest.mark.parametrize(
    ("reason_code", "diagnostic_id"),
    [
        (enums.MethodsValidationReasonCode.VALIDATION_FAILED, None),
        (None, "00000000-0000-4000-8000-000000000777"),
    ],
)
def test_methods_failure_reason_and_diagnostic_are_an_atomic_pair(
    model_name: str,
    reason_code: enums.MethodsValidationReasonCode | None,
    diagnostic_id: str | None,
) -> None:
    from problem_locator.contracts.models import CaseFailure, ExecutionFailure

    if model_name == "ExecutionFailure":
        payload = {
            "stage": enums.ExecutionStage.OUTCOME_VALIDATE,
            "code": enums.ErrorCode.OUTCOME_INVALID,
            "message": "Methods validation failed.",
            "retryable": False,
            "details": [],
        }
        model = ExecutionFailure
    else:
        payload = {
            "code": enums.ErrorCode.OUTCOME_INVALID,
            "message": "Methods validation failed.",
            "source_job_id": None,
            "source_outcome_id": None,
            "occurred_at": "2026-07-31T00:00:00.000Z",
        }
        model = CaseFailure

    with pytest.raises(ValidationError):
        model.model_validate(
            {
                **payload,
                "reason_code": reason_code,
                "diagnostic_id": diagnostic_id,
            }
        )


def test_resource_limits_are_the_frozen_v1_values() -> None:
    assert limits.ROUTER_CONTEXT_BYTES == 131_072
    assert limits.SPECIALIST_CONTEXT_BYTES == 262_144
    assert limits.REVIEWER_CONTEXT_BYTES == 204_800
    assert limits.MAX_ATTACHMENT_BYTES == 2_684_354_560
    assert limits.MAX_CASE_RESOURCE_BYTES == 5_368_709_120
    assert limits.JOB_WALL_TIME_SECONDS == 1_800
    assert limits.JOB_STDOUT_STDERR_BYTES == 67_108_864
    assert limits.JOB_WORKSPACE_BYTES == 1_073_741_824
    assert limits.ACTIVE_WORKERS == 1


def test_retention_limits_are_explicit_and_not_derived_at_runtime() -> None:
    expected = {
        "ORPHAN_RESOURCE_RETENTION_SECONDS": 604_800,
        "PROPOSAL_STAGING_RETENTION_SECONDS": 86_400,
        "UPLOAD_TEMP_RETENTION_SECONDS": 86_400,
        "WORKSPACE_RETENTION_SECONDS": 86_400,
    }
    assert {name: getattr(limits, name) for name in expected} == expected


def test_default_resource_limits_are_role_specific_only_for_context() -> None:
    route = limits.default_resource_limits(enums.JobType.ROUTE)
    diagnose = limits.default_resource_limits(enums.JobType.DIAGNOSE)
    review = limits.default_resource_limits(enums.JobType.REVIEW)
    assert public_value(route, "context_bytes") == 131_072
    assert public_value(diagnose, "context_bytes") == 262_144
    assert public_value(review, "context_bytes") == 204_800
    for value in (route, diagnose, review):
        assert public_value(value, "wall_time_seconds") == 1_800
        assert public_value(value, "stdout_stderr_bytes") == 67_108_864
        assert public_value(value, "workspace_bytes") == 1_073_741_824


def test_revision_matrix_is_complete_and_preserves_the_combined_stale_rule() -> None:
    expected = {
        "ATTACHMENT_LIFECYCLE": (1, 0),
        "CANCEL_FAIL_INTERRUPT_OR_RESUME": (1, 0),
        "CREATE_CASE": ("set_1", "set_1"),
        "DUPLICATE_OUTCOME": (0, 0),
        "FIRST_STALE_OUTCOME": (1, 0),
        "IDEMPOTENT_REPLAY": (0, 0),
        "JOB_LIFECYCLE": (1, 0),
        "OUTCOME_WITH_EMPTY_STATE_DELTA": (1, 0),
        "PENDING_JOB_RESUME_WAKEUP": (0, 0),
        "READ_ONLY_OR_WAIT_TIMEOUT": (0, 0),
        "ROUTE_OUTCOME": (1, 0),
        "RUNTIME_EPOCH_RECORD": ("n/a", "n/a"),
        "SEMANTIC_OUTCOME_OR_SUPPLEMENT": (1, 1),
        "STALE_ACTIVE_OUTCOME": (1, 0),
    }
    actual = {
        event: (
            public_value(rule, "case_revision"),
            public_value(rule, "diagnosis_state_revision"),
        )
        for event, rule in limits.REVISION_MATRIX.items()
    }
    assert actual == expected
    # Base drift saves the STALE audit and interrupts in one commit: never +2.
    assert actual["STALE_ACTIVE_OUTCOME"] == (1, 0)
