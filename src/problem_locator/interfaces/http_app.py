"""FastAPI control/file routes sharing the S06 MCP application."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import TypeAdapter, ValidationError

from problem_locator import __version__
from problem_locator.contracts.commands import (
    ApplicationResponse,
    CreateCase,
    PrepareAttachment,
    SubmitSupplement,
    UploadAttachmentContent,
)
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_DESCRIPTION_UTF8_BYTES,
    MAX_USER_TEXT_UTF8_BYTES,
)
from problem_locator.contracts.models import (
    ContentType,
    OpaqueId,
    ProblemSpecInput,
    Sha256,
    UserFactInput,
    WaitSeconds,
)
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.contracts.ports import (
    ApplicationCommandPort,
    ApplicationQueryPort,
    StateAdminPort,
)
from problem_locator.diagnostics import HttpDiagnosticsMiddleware, log_event

from .error_mapping import (
    error_envelope,
    http_status_for,
    success_envelope,
    validation_diagnostics,
    validation_error_from,
)
from .http_streaming import AsyncRequestBinaryStream, iterate_binary_stream
from .mcp_server import create_mcp_transport
from .projections import artifact_view, web_upload_descriptor
from .rest_models import (
    ApplicationSuccessEnvelope,
    ArtifactListSuccessEnvelope,
    CaseQuerySuccessEnvelope,
    CreateCaseBody,
    ErrorEnvelope,
    LiveSuccessEnvelope,
    PrepareAttachmentBody,
    PrepareAttachmentSuccessEnvelope,
    ReadinessSuccessEnvelope,
    SubmitSupplementBody,
    UploadReadySuccessEnvelope,
)


_OPAQUE_ID = TypeAdapter(OpaqueId)
_CONTENT_TYPE = TypeAdapter(ContentType)
_SHA256 = TypeAdapter(Sha256)
_WAIT_SECONDS = TypeAdapter(WaitSeconds)
_DECIMAL_BYTES = re.compile(r"(?:0|[1-9][0-9]*)")
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CASE_ID_EXAMPLE = "00000000-0000-0000-0000-000000000001"
_JOB_ID_EXAMPLE = "00000000-0000-0000-0000-000000000002"
_ATTACHMENT_ID_EXAMPLE = "00000000-0000-0000-0000-000000000003"
_ARTIFACT_ID_EXAMPLE = "00000000-0000-0000-0000-000000000004"
_TIME_EXAMPLE = "2026-08-18T10:30:00.000Z"
_SHA256_EXAMPLE = "a" * 64
_T = TypeVar("_T")


_ERROR_RESPONSE_DESCRIPTIONS = {
    400: "The request is malformed or violates a validation rule.",
    404: "The requested Case, attachment, Job, or artifact does not exist.",
    409: "The request conflicts with current state, revision, or idempotency history.",
    413: "The uploaded or declared resource exceeds the supported size limit.",
    422: "The request entity cannot be processed.",
    500: "The service failed to read or persist valid state.",
    502: "A diagnosis backend or generated outcome failed.",
    503: "The service or a required capability is unavailable.",
    504: "A diagnosis backend exceeded its execution deadline.",
}
_ERROR_RESPONSES = {
    status: {"model": ErrorEnvelope, "description": description}
    for status, description in _ERROR_RESPONSE_DESCRIPTIONS.items()
}
_UPLOAD_REQUEST_CONTENT = {
    content_type: {"schema": {"type": "string", "format": "binary"}}
    for content_type in (
        "application/gzip",
        "application/zip",
        "application/x-tar",
    )
}

_UUID_PATH_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "pattern": _UUID_PATTERN,
}
_UUID_PARAMETER_NAMES = {
    "case_id",
    "wait_for_job_id",
    "attachment_id",
    "artifact_id",
}
_CORRELATION_HEADER = {
    "description": (
        "Server-generated UUID for correlating this HTTP response with diagnostic logs."
    ),
    "schema": {
        **_UUID_PATH_SCHEMA,
        "example": "20000000-0000-4000-8000-000000000001",
    },
}

_PROBLEM_SPEC_EXAMPLE = {
    "statement": "RPC request times out.",
    "expected_behavior": "The RPC succeeds within the deadline.",
    "actual_behavior": "The caller reports a timeout.",
    "scope": "Payment to inventory RPC.",
    "goals": ["Locate the timeout cause."],
    "non_goals": [],
    "constraints": ["Use only supplied evidence."],
    "completion_criteria": ["Identify an evidenced cause."],
    "revision": 1,
}
_ACTIVE_JOB_EXAMPLE = {
    "job_id": _JOB_ID_EXAMPLE,
    "job_type": "ROUTE",
    "diagnosis_mode": None,
    "status": "PENDING",
    "goal": "Select the diagnosis path for this Case.",
    "base_state_revision": 1,
    "created_at": _TIME_EXAMPLE,
    "started_at": None,
    "finished_at": None,
}
_CASE_VIEW_EXAMPLE = {
    "case_id": _CASE_ID_EXAMPLE,
    "status": "RUNNING",
    "case_revision": 1,
    "raw_problem_text": "RPC request times out.",
    "diagnosis_state_revision": 1,
    "problem_spec": _PROBLEM_SPEC_EXAMPLE,
    "user_facts": [],
    "confirmed_facts": [],
    "open_questions": [],
    "pending_requirements": [],
    "active_job": _ACTIVE_JOB_EXAMPLE,
    "selected_skill_ref": None,
    "final_result": None,
    "unresolved_result": None,
    "generic_result": None,
    "generic_result_v2": None,
    "failure": None,
    "artifacts": [],
    "created_at": _TIME_EXAMPLE,
    "updated_at": _TIME_EXAMPLE,
}
_BUSINESS_RECEIPT_EXAMPLE = {
    "operation": "CreateCase",
    "primary_resource_id": _CASE_ID_EXAMPLE,
    "case_id": _CASE_ID_EXAMPLE,
    "case_revision": 1,
    "job_id": _JOB_ID_EXAMPLE,
    "status": "RUNNING",
}
_APPLICATION_RESPONSE_EXAMPLE = {
    "business_receipt": _BUSINESS_RECEIPT_EXAMPLE,
    "case_view": _CASE_VIEW_EXAMPLE,
    "wait_timed_out": False,
    "dispatch_pending": False,
}
_REVISION_CONFLICT_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "REVISION_CONFLICT",
        "message": "Case revision does not match the expected revision.",
        "details": [
            {
                "field": "expected_case_revision",
                "resource_type": "Case",
                "resource_id": _CASE_ID_EXAMPLE,
                "resource_ref": None,
                "expected": 2,
                "actual": 3,
                "limit": None,
                "observed": None,
            }
        ],
        "retryable": True,
    },
}
_VALIDATION_ERROR_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed.",
        "details": [
            {
                "field": "body.expected_case_revision",
                "resource_type": None,
                "resource_id": None,
                "resource_ref": None,
                "expected": "Input should be a valid integer",
                "actual": "latest",
                "limit": None,
                "observed": None,
            }
        ],
        "retryable": False,
    },
}
_IDEMPOTENCY_CONFLICT_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "IDEMPOTENCY_CONFLICT",
        "message": "The request_id was already used with different business content.",
        "details": [],
        "retryable": False,
    },
}
_CASE_NOT_FOUND_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "CASE_NOT_FOUND",
        "message": "The requested Case does not exist.",
        "details": [],
        "retryable": False,
    },
}
_RESOURCE_CASE_MISMATCH_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "RESOURCE_CASE_MISMATCH",
        "message": "The requested artifact does not belong to the Case.",
        "details": [],
        "retryable": False,
    },
}
_RESOURCE_HASH_MISMATCH_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "RESOURCE_HASH_MISMATCH",
        "message": "The uploaded bytes do not match the declared SHA-256.",
        "details": [],
        "retryable": False,
    },
}
_INSTANCE_LOCKED_EXAMPLE = {
    "ok": False,
    "data": None,
    "error": {
        "code": "INSTANCE_LOCKED",
        "message": "Another service instance owns the configured state root.",
        "details": [],
        "retryable": True,
    },
}
_ERROR_EXAMPLES_BY_OPERATION = {
    "get_readiness": {"503": _INSTANCE_LOCKED_EXAMPLE},
    "create_case": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "409": _IDEMPOTENCY_CONFLICT_EXAMPLE,
    },
    "get_case": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "404": _CASE_NOT_FOUND_EXAMPLE,
    },
    "submit_supplement": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "409": _REVISION_CONFLICT_EXAMPLE,
    },
    "prepare_attachment": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "409": _REVISION_CONFLICT_EXAMPLE,
    },
    "upload_attachment": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "422": _RESOURCE_HASH_MISMATCH_EXAMPLE,
    },
    "list_artifacts": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "404": _CASE_NOT_FOUND_EXAMPLE,
    },
    "download_artifact": {
        "400": _VALIDATION_ERROR_EXAMPLE,
        "409": _RESOURCE_CASE_MISMATCH_EXAMPLE,
    },
}

_SUCCESS_EXAMPLES: dict[str, dict[str, Any]] = {
    "get_liveness": {
        "ok": True,
        "data": {"status": "live"},
        "error": None,
    },
    "get_readiness": {
        "ok": True,
        "data": {
            "ready": True,
            "checks": [
                {"name": name, "passed": True, "message": None}
                for name in (
                    "CONFIG",
                    "INSTANCE_LOCK",
                    "STATE",
                    "DATA_DIRECTORIES",
                    "RECOVERY",
                )
            ],
            "error": None,
        },
        "error": None,
    },
    "create_case": {
        "ok": True,
        "data": _APPLICATION_RESPONSE_EXAMPLE,
        "error": None,
    },
    "get_case": {
        "ok": True,
        "data": {"case_view": _CASE_VIEW_EXAMPLE, "wait_timed_out": False},
        "error": None,
    },
    "submit_supplement": {
        "ok": True,
        "data": {
            **_APPLICATION_RESPONSE_EXAMPLE,
            "business_receipt": {
                **_BUSINESS_RECEIPT_EXAMPLE,
                "operation": "SubmitSupplement",
                "case_revision": 2,
            },
            "case_view": {**_CASE_VIEW_EXAMPLE, "case_revision": 2},
        },
        "error": None,
    },
    "prepare_attachment": {
        "ok": True,
        "data": {
            "application_response": {
                **_APPLICATION_RESPONSE_EXAMPLE,
                "business_receipt": {
                    **_BUSINESS_RECEIPT_EXAMPLE,
                    "operation": "PrepareAttachment",
                    "primary_resource_id": _ATTACHMENT_ID_EXAMPLE,
                    "case_revision": 2,
                    "job_id": None,
                    "status": "UPLOADING",
                },
                "case_view": {**_CASE_VIEW_EXAMPLE, "case_revision": 2},
            },
            "upload": {
                "attachment_id": _ATTACHMENT_ID_EXAMPLE,
                "method": "PUT",
                "url": (
                    "http://127.0.0.1:8000/api/v1/attachments/"
                    f"{_ATTACHMENT_ID_EXAMPLE}/content"
                ),
                "required_headers": {
                    "Idempotency-Key": _ATTACHMENT_ID_EXAMPLE,
                    "Content-Type": "application/zip",
                    "X-Content-SHA256": _SHA256_EXAMPLE,
                },
                "expected_content_length": 1024,
                "max_bytes": MAX_ATTACHMENT_BYTES,
                "expires_at": None,
            },
        },
        "error": None,
    },
    "upload_attachment": {
        "ok": True,
        "data": {
            "attachment_id": _ATTACHMENT_ID_EXAMPLE,
            "case_id": _CASE_ID_EXAMPLE,
            "status": "READY",
            "case_revision": 3,
        },
        "error": None,
    },
    "list_artifacts": {
        "ok": True,
        "data": {
            "artifacts": [
                {
                    "artifact_id": _ARTIFACT_ID_EXAMPLE,
                    "kind": "DIAGNOSTIC_EXPORT",
                    "name": "diagnostic.json",
                    "content_type": "application/json",
                    "size": 1024,
                    "sha256": _SHA256_EXAMPLE,
                    "created_at": _TIME_EXAMPLE,
                    "download_url": (
                        "http://127.0.0.1:8000/api/v1/artifacts/"
                        f"{_ARTIFACT_ID_EXAMPLE}/content?case_id={_CASE_ID_EXAMPLE}"
                    ),
                }
            ]
        },
        "error": None,
    },
}

# Shared contract models deliberately remain transport-neutral.  These labels
# enrich only the REST OpenAPI projection and therefore cannot change the
# persisted or command/query schemas.
_REST_FIELD_DESCRIPTIONS = {
    "Content-Type": "Canonical media type that must match the prepared attachment.",
    "Idempotency-Key": "Attachment UUID reused as the idempotency key for raw upload.",
    "X-Content-SHA256": "Lowercase SHA-256 digest of the complete byte stream.",
    "active_job": "Current PENDING or RUNNING Job; null outside executing states.",
    "actual": "Actual value observed when evaluating the error detail.",
    "actual_behavior": "Behavior observed by the user.",
    "allowed_content_types": "Canonical media types accepted for an attachment requirement.",
    "allowed_values": "Closed set of text values accepted for this input; an empty array means unrestricted.",
    "application_response": "Mutation receipt and current Case view, when immediately available.",
    "artifact_id": "Canonical lowercase UUID of an immutable artifact.",
    "artifacts": "Artifacts currently visible to the browser client.",
    "attachment_id": "Canonical lowercase UUID of an attachment.",
    "attachment_ids": "Unique READY attachment UUIDs submitted as evidence.",
    "audit_artifact_id": "UUID of the audit bundle associated with an unresolved result.",
    "base_state_revision": "Diagnosis-state revision from which the Job was created.",
    "blocking_rule_ids": "Rule identifiers that prevented a resolved result.",
    "business_receipt": "Durable receipt proving the logical write was accepted.",
    "candidate_factors": "Possible causal factors not established as causes.",
    "case_id": "Canonical lowercase UUID of the Case.",
    "case_revision": "Concurrency revision for Case writes; always use the latest observed value.",
    "case_view": "Current Case projection, or null when the committed view could not be reread.",
    "causal_factors": "Evidence-backed causal factors in the proposed conclusion.",
    "checks": "Sanitized readiness checks for required service subsystems.",
    "code": "Stable machine-readable error code.",
    "completion_criteria": "Observable criteria used to decide whether diagnosis is complete.",
    "completion_criteria_mapping": "Result assessment for every completion criterion.",
    "conclusion": "Human-readable terminal conclusion.",
    "conclusion_id": "Canonical lowercase UUID of a candidate conclusion.",
    "confirmed_facts": "Active system-derived facts backed by evidence.",
    "constraints": "Operational or evidence constraints on the diagnosis.",
    "content_hash": "Lowercase SHA-256 digest identifying immutable versioned content.",
    "content_type": "Canonical media type of the resource bytes.",
    "created_at": "UTC creation timestamp with exactly millisecond precision.",
    "created_by_job_id": "UUID of the Job that created this artifact.",
    "created_revision": "Diagnosis-state revision in which this item was first recorded.",
    "criterion": "Exact completion-criterion text being assessed.",
    "criterion_index": "Zero-based position of the criterion in problem_spec.completion_criteria.",
    "data": "Operation-specific payload; null for errors.",
    "declared_sha256": "Lowercase SHA-256 digest declared before upload.",
    "declared_size": "Exact byte length declared before upload.",
    "details": "Structured, safe diagnostic details; may be empty.",
    "diagnostic_id": "Diagnostic UUID that correlates a classified failure across public results and execution records.",
    "diagnosis_mode": "Whether the Job uses a specialized or generic diagnosis path.",
    "diagnosis_state_revision": "Internal diagnosis-content revision; never use for REST write concurrency.",
    "dispatch_pending": "True when durable work exists but dispatch has not yet been confirmed.",
    "download_url": "Absolute URL for downloading this artifact; includes its owning case_id.",
    "downloadable": "Whether the artifact has externally downloadable immutable bytes.",
    "error": "Structured error for failures; null for successful envelopes.",
    "evidence_refs": "UUIDs of evidence records supporting this statement.",
    "excluded_factors": "Investigated factors excluded by the available evidence.",
    "expected": "Expected value or state associated with the error detail.",
    "expected_behavior": "Behavior that should occur when the system works correctly.",
    "expected_case_revision": "Latest case_revision the client expects before a write.",
    "expected_content_length": "Exact byte length expected for the raw upload.",
    "expires_at": "Upload expiration time; always null for V1 reserved uploads.",
    "explanation": "Human-readable rationale for a criterion assessment.",
    "factor_id": "Stable identifier of a causal factor within the conclusion.",
    "failure": "Terminal failure detail, present exactly when Case status is FAILED.",
    "field": "Request field or logical field associated with this error detail.",
    "final_result": "Accepted specialized conclusion for resolved states, otherwise null.",
    "finished_at": "UTC Job finish time, or null until the Job reaches a terminal state.",
    "format_version": "Version discriminator for the generic result representation.",
    "fulfilled_by_refs": "Resource or input references that fulfilled this requirement.",
    "generic_result": "Terminal generic-path result, otherwise null.",
    "generic_result_v2": "Terminal Generic V2 Markdown result, mutually exclusive with generic_result, otherwise null.",
    "goal": "Human-readable objective assigned to the Job.",
    "goals": "Outcomes the diagnosis should achieve.",
    "id": "Stable identifier of versioned content.",
    "initial_user_facts": "Optional unique named facts supplied during Case creation.",
    "input_name": "Name of the user input that produced this diagnosis item, or null.",
    "inputs": "Unique named supplemental text values.",
    "item_id": "Canonical lowercase UUID of a diagnosis item.",
    "job_id": "Canonical lowercase UUID of a Job, or null when no Job was created.",
    "job_type": "Pipeline stage performed by the Job.",
    "kind": "Stable enum identifying the requirement or artifact kind.",
    "limit": "Configured limit associated with the error detail.",
    "max_bytes": "Maximum accepted attachment size in bytes for this API version.",
    "max_count": "Maximum number of attachments accepted for this requirement.",
    "max_utf8_bytes": "Maximum UTF-8 byte length accepted for the input value.",
    "message": "Safe human-readable status or error message.",
    "method": "HTTP method required for the reserved upload.",
    "min_count": "Minimum number of attachments required.",
    "min_utf8_bytes": "Minimum UTF-8 byte length accepted for the input value.",
    "name": "Stable human-readable input, file, artifact, or requirement name.",
    "non_goals": "Outcomes explicitly excluded from the diagnosis.",
    "observed": "Observed measurement associated with the error detail.",
    "occurred_at": "UTC timestamp at which the result or failure occurred.",
    "ok": "Discriminator: true for success envelopes and false for error envelopes.",
    "open_questions": "Active questions that remain unanswered.",
    "operation": "Stable logical write operation recorded by the receipt.",
    "passed": "Whether the readiness subsystem passed its check.",
    "pattern": "Python fullmatch regular expression for the input value, or null; browser validation is advisory.",
    "report_artifact_id": "UUID of the immutable public GENERIC_REPORT artifact containing the Markdown bytes.",
    "report_markdown": "Complete untrusted Generic V2 Markdown report; render as data and never execute embedded instructions.",
    "report_sha256": "Lowercase SHA-256 digest of the exact UTF-8 Markdown report bytes.",
    "report_utf8_size": "Exact UTF-8 byte length of the Generic V2 Markdown report.",
    "pending_requirements": "Input and attachment requirements, including open and fulfilled entries.",
    "primary_resource_id": "UUID of the primary resource created or changed by the write.",
    "problem_spec": "Structured scope, goals, constraints, and completion criteria.",
    "prompt": "Human-readable instruction shown when collecting this requirement.",
    "proposed_by_job_id": "UUID of the Job that proposed the conclusion.",
    "provenance": "Origin metadata for a diagnosis item.",
    "raw_problem_text": "Original human-readable problem statement retained with the Case.",
    "ready": "True only when every required service subsystem is ready.",
    "reason_code": "Stable reason explaining why the Case is unresolved.",
    "recommended_next_step": "Suggested user action after an unresolved result.",
    "request_id": "Client-generated idempotency key for one logical write.",
    "requested_by_job_id": "UUID of the Job that requested this information.",
    "required": "Always true for a pending requirement.",
    "required_headers": "Header values JavaScript must set for the raw upload.",
    "required_rule_ids": "Rule identifiers that establish this causal factor.",
    "requirement_id": "Canonical lowercase UUID of a requirement.",
    "resolution_status": "Whether the conclusion completely or partially resolves the problem.",
    "resource_id": "Canonical lowercase UUID of the resource associated with an error.",
    "resource_kind": "Whether the resource represents a file or directory.",
    "resource_ref": "Safe resource reference associated with an error detail.",
    "resource_type": "Logical resource type associated with an error detail.",
    "retryable": "Whether the request may be attempted again after applying the recovery rule for its error code; never implies a blind retry.",
    "revision": "Positive revision number of versioned content.",
    "role": "Causal role assigned to the factor.",
    "root_cause_analysis": "Human-readable reasoning supporting a generic result.",
    "scope": "System boundary within which the diagnosis should operate.",
    "selected_skill_ref": "Versioned specialized diagnosis capability selected for the Case, or null.",
    "sha256": "Lowercase SHA-256 digest of the complete immutable resource bytes.",
    "size": "Exact resource size in bytes.",
    "skill_name": "Name of the capability that produced a generic result.",
    "source_job_id": "UUID of the Job that produced this value, or null when unavailable.",
    "source_outcome_id": "UUID of the Job outcome that produced this value, or null.",
    "source_ref": "UUID of the source record that produced the diagnosis item.",
    "source_type": "Stable provenance enum identifying the source category.",
    "started_at": "UTC Job start time, or null before execution begins.",
    "statement": "Concise human-readable fact, question, factor, or problem statement.",
    "status": "Current wire status; interpret using the containing model's enum.",
    "summary": "Human-readable summary of an unresolved result.",
    "supersedes": "UUIDs of earlier diagnosis items replaced by this item.",
    "supplement_policy": "Whether the client may supply only missing values for this requirement.",
    "supporting_evidence_refs": "UUIDs of evidence records supporting the conclusion.",
    "terminal_path_id": "Stable identifier of the terminal diagnosis path.",
    "unresolved_result": "Structured unresolved result, present only for UNRESOLVED Cases.",
    "updated_at": "UTC timestamp of the latest Case update, with millisecond precision.",
    "upload": "Reserved raw-byte upload instructions.",
    "url": "Absolute URL for the next HTTP request.",
    "user_facts": "Active facts supplied directly by the user.",
    "user_result_artifact_id": "UUID of the user-facing result artifact.",
    "value": "Non-empty text value associated with an input name.",
    "value_type": "Wire type accepted for the requirement; currently STRING.",
    "version": "Non-empty immutable content version string.",
    "wait_seconds": "Long-poll allowance in seconds from 0 through 30.",
    "wait_timed_out": "True when long polling reached wait_seconds before the watched Job changed.",
}

_SUCCESS_RESPONSE_DESCRIPTIONS = {
    "get_liveness": "The process is live.",
    "get_readiness": "Every required subsystem is ready.",
    "create_case": "The Case creation write was durably accepted.",
    "get_case": "The latest Case projection is returned.",
    "submit_supplement": "The supplement write was durably accepted.",
    "prepare_attachment": "The attachment reservation and upload instructions are returned.",
    "upload_attachment": "The bytes were verified and the attachment is READY.",
    "list_artifacts": "Public downloadable artifact metadata is returned.",
    "download_artifact": "Immutable artifact bytes and integrity headers are returned.",
}

_REQUEST_BODY_DESCRIPTIONS = {
    "create_case": "Strict JSON body defining one new Case.",
    "submit_supplement": "Strict JSON body containing requested inputs and READY attachments.",
    "prepare_attachment": "Strict JSON body declaring immutable attachment metadata.",
}


def _component_schema_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix):
            refs.add(ref.removeprefix(prefix))
        for nested in value.values():
            refs.update(_component_schema_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            refs.update(_component_schema_refs(nested))
    return refs


def _require_serialized_response_fields(schema: dict[str, Any]) -> None:
    """Reflect model_json's complete-field response serialization in OpenAPI."""

    component_schemas = schema["components"]["schemas"]
    pending: set[str] = set()
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for response in operation.get("responses", {}).values():
                for media in response.get("content", {}).values():
                    pending.update(_component_schema_refs(media.get("schema", {})))

    visited: set[str] = set()
    while pending:
        schema_name = pending.pop()
        if schema_name in visited:
            continue
        visited.add(schema_name)
        component = component_schemas[schema_name]
        properties = component.get("properties", {})
        if component.get("type") == "object" and properties:
            component["required"] = list(properties)
        pending.update(_component_schema_refs(component) - visited)


def _annotate_utf8_byte_limits(value: Any) -> set[int]:
    limits: set[int] = set()
    if isinstance(value, dict):
        max_length = value.get("maxLength")
        if max_length in {MAX_USER_TEXT_UTF8_BYTES, MAX_DESCRIPTION_UTF8_BYTES}:
            value["x-max-utf8-bytes"] = max_length
            limits.add(max_length)
        for nested in value.values():
            limits.update(_annotate_utf8_byte_limits(nested))
    elif isinstance(value, list):
        for nested in value:
            limits.update(_annotate_utf8_byte_limits(nested))
    return limits


def _apply_rest_openapi_overlay(schema: dict[str, Any]) -> None:
    """Add browser-facing semantics without mutating shared contract models."""

    schema["info"]["description"] = (
        "Authoritative browser REST contract. JSON bodies are strict and reject "
        "unknown fields. All timestamps are UTC with millisecond precision."
    )
    schema["tags"] = [
        {"name": "service", "description": "Liveness and readiness probes."},
        {"name": "cases", "description": "Create, observe, and supplement diagnosis Cases."},
        {"name": "attachments", "description": "Reserve and upload immutable evidence files."},
        {"name": "artifacts", "description": "List and download immutable result artifacts."},
    ]

    component_schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for schema_name, component in component_schemas.items():
        component.setdefault(
            "description",
            f"Browser REST representation of {schema_name}.",
        )
        for field_name, field_schema in component.get("properties", {}).items():
            field_schema.setdefault(
                "description",
                _REST_FIELD_DESCRIPTIONS[field_name],
            )
            byte_limits = _annotate_utf8_byte_limits(field_schema)
            if byte_limits and "UTF-8 byte" not in field_schema["description"]:
                joined_limits = " or ".join(f"{limit:,}" for limit in sorted(byte_limits))
                field_schema["description"] += (
                    f" Text values are non-empty, not all Unicode whitespace, and each "
                    f"is limited to {joined_limits} UTF-8 bytes."
                )

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = operation["operationId"]
            operation["responses"]["200"]["description"] = (
                _SUCCESS_RESPONSE_DESCRIPTIONS[operation_id]
            )
            request_body = operation.get("requestBody")
            if operation_id in _REQUEST_BODY_DESCRIPTIONS and request_body is not None:
                request_body["description"] = _REQUEST_BODY_DESCRIPTIONS[operation_id]
            # UUID patterns are documentation metadata only. Runtime values must
            # continue through the existing application validators so structured
            # validation errors remain wire-compatible.
            for parameter in operation.get("parameters", []):
                if (
                    parameter.get("in") not in {"path", "query"}
                    or parameter.get("name") not in _UUID_PARAMETER_NAMES
                ):
                    continue
                parameter_schema = parameter["schema"]
                string_schema = next(
                    (
                        candidate
                        for candidate in parameter_schema.get("anyOf", [])
                        if candidate.get("type") == "string"
                    ),
                    parameter_schema,
                )
                string_schema["pattern"] = _UUID_PATTERN
                parameter_schema.setdefault("format", "uuid")
            for response in operation.get("responses", {}).values():
                response.setdefault("headers", {})[
                    "X-Problem-Locator-Correlation-ID"
                ] = _CORRELATION_HEADER
            success_example = _SUCCESS_EXAMPLES.get(operation_id)
            if success_example is not None:
                content = operation["responses"]["200"]["content"]["application/json"]
                content["examples"] = {
                    "success": {
                        "summary": "Successful response",
                        "value": success_example,
                    }
                }
            for status, example in _ERROR_EXAMPLES_BY_OPERATION.get(
                operation_id,
                {},
            ).items():
                error_response = operation.get("responses", {}).get(status)
                if error_response is None:
                    continue
                error_response["content"]["application/json"]["examples"] = {
                    "error": {
                        "summary": "Representative structured error",
                        "value": example,
                    }
                }

    download = schema["paths"]["/api/v1/artifacts/{artifact_id}/content"]["get"]
    download["responses"]["200"]["content"].pop("application/json", None)
    for header_name, example in {
        "Content-Length": 1024,
        "Content-Type": "application/json",
        "X-Content-SHA256": _SHA256_EXAMPLE,
    }.items():
        download["responses"]["200"]["headers"][header_name]["example"] = example

    _require_serialized_response_fields(schema)
    schema["x-rest-interface-overlay-version"] = 2


@dataclass(frozen=True, slots=True)
class UploadHeaders:
    idempotency_key: str
    content_type: str
    content_length: int
    content_sha256: str


def _json(data: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status_code)


def _log_http_validation_failure(
    operation: str,
    arguments: dict[str, Any],
    error: ValidationError | RequestValidationError | ValueError | TypeError,
) -> None:
    log_event(
        "http.operation.validation_failed",
        level=logging.WARNING,
        operation=operation,
        arguments=arguments,
        validation_errors=validation_diagnostics(error),
        error=error,
    )


def _port_error_response(
    exc: ApplicationPortError,
    *,
    operation: str,
    arguments: dict[str, Any],
) -> JSONResponse:
    log_event(
        "http.operation.application_error",
        level=logging.WARNING,
        operation=operation,
        arguments=arguments,
        error_code=exc.error.code,
        application_error=exc.error,
        error=exc,
    )
    return _json(
        error_envelope(exc.error),
        status_code=http_status_for(exc.error),
    )


def _response_case_identity(response: ApplicationResponse) -> tuple[str, int]:
    """Use the durable receipt when r3 cannot reread the post-commit CaseView."""

    if response.case_view is not None:
        return response.case_view.case_id, response.case_view.case_revision
    receipt = response.business_receipt
    if receipt.case_id is None or receipt.case_revision is None:
        raise ValueError("application response contains no persisted case identity")
    return receipt.case_id, receipt.case_revision


async def _settle_worker(task: asyncio.Task[_T]) -> _T:
    """Wait for an uncancellable worker, tolerating repeated ASGI cancellation."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _port_call(
    function: Callable[..., _T],
    *args: Any,
    on_cancel: Callable[[], Awaitable[None]] | None = None,
    dispose_cancelled_result: Callable[[_T], None] | None = None,
) -> _T:
    """Run a synchronous Port without losing results or resources on cancel."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            await on_cancel()
        try:
            result = await _settle_worker(worker)
        except BaseException as exc:
            # Retrieve a worker failure so it is never logged as an unhandled
            # task exception; the cancelled HTTP request has no response sink.
            log_event(
                "http.cancelled_worker.failed",
                level=logging.ERROR,
                error=exc,
            )
        else:
            if dispose_cancelled_result is not None:
                dispose_cancelled_result(result)
        raise


class _ClosingStreamingResponse(StreamingResponse):
    """Close the frozen stream even if ASGI fails before iteration begins."""

    def __init__(self, stream: Any, *args: Any, **kwargs: Any) -> None:
        self._source_stream = stream
        super().__init__(*args, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BinaryStream.close is deliberately synchronous and idempotent.
            # Calling it here covers response-start/send failures where the
            # async body iterator's own finally block is never entered.
            self._source_stream.close()


def parse_upload_headers(request: Request, attachment_id: str) -> UploadHeaders:
    """Validate all four upload headers before exposing the request body."""

    typed_attachment_id = _OPAQUE_ID.validate_python(attachment_id)
    values: dict[bytes, list[bytes]] = {}
    for name, value in request.scope.get("headers", []):
        values.setdefault(name.lower(), []).append(value)

    required = {
        b"idempotency-key": "Idempotency-Key",
        b"content-type": "Content-Type",
        b"content-length": "Content-Length",
        b"x-content-sha256": "X-Content-SHA256",
    }
    decoded: dict[str, str] = {}
    for raw_name, public_name in required.items():
        matches = values.get(raw_name, [])
        if len(matches) != 1:
            raise ValueError(f"{public_name} must appear exactly once")
        try:
            decoded[public_name] = matches[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{public_name} must be ASCII") from exc

    if decoded["Idempotency-Key"] != typed_attachment_id:
        raise ValueError("Idempotency-Key must equal attachment_id")
    content_type = _CONTENT_TYPE.validate_python(decoded["Content-Type"])
    raw_length = decoded["Content-Length"]
    if _DECIMAL_BYTES.fullmatch(raw_length) is None:
        raise ValueError("Content-Length must be a canonical decimal integer")
    content_length = int(raw_length)
    if content_length > MAX_ATTACHMENT_BYTES:
        raise ValueError("Content-Length exceeds the V1 Attachment limit")
    content_sha256 = _SHA256.validate_python(decoded["X-Content-SHA256"])
    return UploadHeaders(
        idempotency_key=typed_attachment_id,
        content_type=content_type,
        content_length=content_length,
        content_sha256=content_sha256,
    )


def create_http_app(
    *,
    command_port: ApplicationCommandPort,
    query_port: ApplicationQueryPort,
    state_admin: StateAdminPort,
    public_base_url: str,
) -> FastAPI:
    """Create one ASGI application containing HTTP and stateless MCP routes."""

    mcp = create_mcp_transport(
        command_port,
        query_port,
        public_base_url=public_base_url,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="Problem Locator V1",
        version=__version__,
        description="Authoritative browser REST API for Problem Locator.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # Serve the versioned OpenAPI document in the repository's canonical JSON
    # profile so downloaded bytes can be checked directly against the snapshot.
    framework_openapi = app.openapi

    def rest_openapi() -> dict[str, Any]:
        schema = framework_openapi()
        if schema.get("x-rest-interface-overlay-version") != 2:
            _apply_rest_openapi_overlay(schema)
        return schema

    app.openapi = rest_openapi  # type: ignore[method-assign]
    app.openapi_url = "/openapi.json"
    app.docs_url = "/docs"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Idempotency-Key",
            "X-Content-SHA256",
        ],
        expose_headers=[
            "Content-Length",
            "Content-Type",
            "X-Content-SHA256",
            "X-Problem-Locator-Correlation-ID",
        ],
    )
    # Add diagnostics last so it wraps CORS-generated OPTIONS responses too.
    app.add_middleware(HttpDiagnosticsMiddleware)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_document() -> Response:
        return Response(
            content=canonical_json_bytes(app.openapi()),
            media_type="application/json",
        )

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> Response:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url="/docs/oauth2-redirect",
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect() -> Response:
        return get_swagger_ui_oauth2_redirect_html()

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        route = request.scope.get("route")
        operation = getattr(route, "name", None) or request.url.path
        arguments: dict[str, Any] = {
            "path": dict(request.path_params),
            "query": list(request.query_params.multi_items()),
        }
        if exc.body is not None:
            arguments["body"] = exc.body
        _log_http_validation_failure(operation, arguments, exc)
        error = validation_error_from(exc)
        return _json(error_envelope(error), status_code=http_status_for(error))

    @app.get(
        "/live",
        response_model=LiveSuccessEnvelope,
        operation_id="get_liveness",
        summary="Check process liveness",
        description=(
            "Returns HTTP 200 while the server process can accept HTTP requests. "
            "This endpoint does not assert that persistent state is ready."
        ),
        tags=["service"],
    )
    async def live() -> JSONResponse:
        return _json(success_envelope({"status": "live"}))

    @app.get(
        "/ready",
        response_model=ReadinessSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        operation_id="get_readiness",
        summary="Check service readiness",
        description=(
            "Returns a sanitized check list when configuration, state, data directories, "
            "recovery, and the instance lock are ready."
        ),
        tags=["service"],
    )
    async def ready() -> JSONResponse:
        report = await _port_call(state_admin.readiness)
        if report.ready:
            # Readiness check messages are not constrained by S00's safe
            # ApplicationError detail grammar.  Expose the frozen check names
            # and booleans, but never forward free-form infrastructure text.
            return _json(
                success_envelope(
                    {
                        "ready": True,
                        "checks": [
                            {
                                "name": check.name,
                                "passed": check.passed,
                                "message": None,
                            }
                            for check in report.checks
                        ],
                        "error": None,
                    }
                )
            )
        assert report.error is not None
        log_event(
            "service.readiness.failed",
            level=logging.ERROR,
            error_code=report.error.code,
            application_error=report.error,
            checks=report.checks,
        )
        return _json(
            error_envelope(report.error),
            status_code=http_status_for(report.error),
        )

    @app.post(
        "/api/v1/cases",
        response_model=ApplicationSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="create_case",
        operation_id="create_case",
        summary="Create a diagnosis Case",
        description=(
            "Creates one Case from a strict structured problem definition. Retrying the "
            "same logical write must reuse request_id and unchanged business content."
        ),
        tags=["cases"],
    )
    async def create_case(body: CreateCaseBody) -> JSONResponse:
        operation = "create_case"
        arguments: dict[str, Any] = {
            "body": body.model_dump(mode="json", by_alias=True)
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            problem_spec = ProblemSpecInput.model_validate(
                body.problem_spec.model_dump(mode="python"),
                strict=True,
            )
            initial_user_facts = [
                UserFactInput(name=fact.name, value=fact.value)
                for fact in body.initial_user_facts
            ]
            command = CreateCase(
                idempotency_key=body.request_id,
                raw_problem_text=body.raw_problem_text,
                problem_spec=problem_spec,
                initial_user_facts=initial_user_facts,
                wait_seconds=body.wait_seconds,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.get(
        "/api/v1/cases/{case_id}",
        response_model=CaseQuerySuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="get_case",
        operation_id="get_case",
        summary="Get the current Case view",
        description=(
            "Reads the latest Case projection and optionally long-polls until the watched "
            "Job changes state or wait_seconds elapses."
        ),
        tags=["cases"],
    )
    async def get_case(
        case_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID of the Case to read.",
                examples=[_CASE_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        request: Request,
        wait_for_job_id: Annotated[
            str | None,
            Query(
                description=(
                    "Job UUID whose state change should end the long poll. When omitted, "
                    "the active Job in the initial snapshot is watched if wait_seconds "
                    "is greater than 0."
                ),
                examples=[_JOB_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ] = None,
        wait_seconds: Annotated[
            int,
            Query(
                ge=0,
                le=30,
                description="Long-poll allowance in seconds; 0 returns immediately.",
                examples=[0, 30],
            ),
        ] = 0,
    ) -> JSONResponse:
        operation = "get_case"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            query_items = list(request.query_params.multi_items())
            allowed = {"wait_for_job_id", "wait_seconds"}
            names = [name for name, _value in query_items]
            if any(name not in allowed for name in names):
                raise ValueError("query contains an unknown parameter")
            if len(names) != len(set(names)):
                raise ValueError("query parameters must appear at most once")
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            typed_wait_job_id = (
                None
                if wait_for_job_id is None
                else _OPAQUE_ID.validate_python(wait_for_job_id)
            )
            typed_wait_seconds = _WAIT_SECONDS.validate_python(wait_seconds)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(
                query_port.get_case,
                typed_case_id,
                typed_wait_job_id,
                typed_wait_seconds,
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.post(
        "/api/v1/cases/{case_id}/supplements",
        response_model=ApplicationSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="submit_supplement",
        operation_id="submit_supplement",
        summary="Submit required inputs and attachments",
        description=(
            "Submits named text inputs and READY attachment UUIDs against the latest "
            "case_revision. At least one input or attachment is required."
        ),
        tags=["cases"],
    )
    async def submit_supplement(
        case_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID of the Case to supplement.",
                examples=[_CASE_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        body: SubmitSupplementBody,
    ) -> JSONResponse:
        operation = "submit_supplement"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "body": body.model_dump(mode="json", by_alias=True),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            command = SubmitSupplement(
                idempotency_key=body.request_id,
                case_id=typed_case_id,
                expected_case_revision=body.expected_case_revision,
                inputs={item.name: item.value for item in body.inputs},
                attachment_ids=body.attachment_ids,
                wait_seconds=body.wait_seconds,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.post(
        "/api/v1/cases/{case_id}/attachments",
        response_model=PrepareAttachmentSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="prepare_attachment",
        operation_id="prepare_attachment",
        summary="Reserve an attachment upload",
        description=(
            "Declares immutable file metadata and returns the URL and headers for the raw "
            "byte upload. This reservation changes case_revision."
        ),
        tags=["attachments"],
    )
    async def prepare_attachment(
        case_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID of the Case that will own the file.",
                examples=[_CASE_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        body: PrepareAttachmentBody,
    ) -> JSONResponse:
        operation = "prepare_attachment"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "body": body.model_dump(mode="json", by_alias=True),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            command = PrepareAttachment(
                idempotency_key=body.request_id,
                case_id=typed_case_id,
                expected_case_revision=body.expected_case_revision,
                name=body.name,
                content_type=body.content_type,
                declared_size=body.declared_size,
                declared_sha256=body.declared_sha256,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        descriptor = web_upload_descriptor(
            response,
            public_base_url=public_base_url,
            content_type=body.content_type,
            declared_size=body.declared_size,
            declared_sha256=body.declared_sha256,
        )
        return _json(
            success_envelope(
                {"application_response": response, "upload": descriptor}
            )
        )

    @app.put(
        "/api/v1/attachments/{attachment_id}/content",
        response_model=UploadReadySuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="upload_attachment",
        operation_id="upload_attachment",
        summary="Upload attachment bytes",
        description=(
            "Streams one prepared archive, verifies its content type, length, and SHA-256, "
            "then marks it READY and advances case_revision."
        ),
        tags=["attachments"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "description": (
                    "Raw archive bytes. Pass a File or Blob body so Chrome generates "
                    "Content-Length; JavaScript must not set that forbidden header."
                ),
                "content": _UPLOAD_REQUEST_CONTENT,
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "description": "Must exactly equal attachment_id.",
                    "schema": {
                        **_UUID_PATH_SCHEMA,
                        "example": _ATTACHMENT_ID_EXAMPLE,
                    },
                },
                {
                    "name": "Content-Type",
                    "in": "header",
                    "required": True,
                    "description": (
                        "Must equal the media type declared while reserving the upload."
                    ),
                    "schema": {
                        "type": "string",
                        "pattern": (
                            "^[a-z0-9][a-z0-9!#$&^_.+\\-]{0,62}/"
                            "[a-z0-9][a-z0-9!#$&^_.+\\-]{0,62}$"
                        ),
                        "example": "application/zip",
                    },
                },
                {
                    "name": "X-Content-SHA256",
                    "in": "header",
                    "required": True,
                    "description": "Must equal the lowercase digest declared at prepare time.",
                    "schema": {
                        "type": "string",
                        "pattern": _SHA256_PATTERN,
                        "example": _SHA256_EXAMPLE,
                    },
                },
                {
                    "name": "Content-Length",
                    "in": "header",
                    "required": True,
                    "description": "Generated by Chrome for a File or Blob body; browser JavaScript must not set it.",
                    "schema": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_ATTACHMENT_BYTES,
                        "example": 1024,
                    },
                },
            ],
        },
    )
    async def upload_attachment(
        attachment_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID returned by prepare_attachment.",
                examples=[_ATTACHMENT_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        request: Request,
    ) -> JSONResponse:
        operation = "upload_attachment"
        arguments: dict[str, Any] = {
            "attachment_id": attachment_id,
            "headers": [
                [
                    bytes(name).decode("latin-1", errors="replace"),
                    bytes(value).decode("latin-1", errors="replace"),
                ]
                for name, value in request.scope.get("headers", [])
            ],
        }
        try:
            headers = parse_upload_headers(request, attachment_id)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))

        arguments["upload"] = {
            "idempotency_key": headers.idempotency_key,
            "content_type": headers.content_type,
            "content_length": headers.content_length,
            "content_sha256": headers.content_sha256,
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )

        stream = AsyncRequestBinaryStream(
            request.stream(),
            loop=asyncio.get_running_loop(),
        )
        try:
            try:
                command = UploadAttachmentContent.model_validate(
                    {
                        "idempotency_key": headers.idempotency_key,
                        "attachment_id": attachment_id,
                        "expected_content_type": headers.content_type,
                        "expected_size": headers.content_length,
                        "expected_sha256": headers.content_sha256,
                        "byte_stream": stream,
                    }
                )
            except ValidationError as exc:
                _log_http_validation_failure(operation, arguments, exc)
                error = validation_error_from(exc)
                return _json(error_envelope(error), status_code=http_status_for(error))
            try:
                response = await _port_call(
                    command_port.execute,
                    command,
                    on_cancel=stream.abort,
                )
            except ApplicationPortError as exc:
                return _port_error_response(
                    exc,
                    operation=operation,
                    arguments=arguments,
                )
        finally:
            await stream.aclose()

        response_case_id, response_case_revision = _response_case_identity(response)
        return _json(
            success_envelope(
                {
                    "attachment_id": attachment_id,
                    "case_id": response_case_id,
                    "status": "READY",
                    "case_revision": response_case_revision,
                }
            )
        )

    @app.get(
        "/api/v1/cases/{case_id}/artifacts",
        response_model=ArtifactListSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="list_artifacts",
        operation_id="list_artifacts",
        summary="List downloadable Case artifacts",
        description=(
            "Returns public immutable artifacts for one Case. This operation accepts no "
            "query parameters."
        ),
        tags=["artifacts"],
    )
    async def list_artifacts(
        case_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID of the Case whose artifacts to list.",
                examples=[_CASE_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        request: Request,
    ) -> JSONResponse:
        operation = "list_artifacts"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            if request.query_params:
                raise ValueError("artifact list does not accept query parameters")
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(
                query_port.list_artifacts,
                typed_case_id,
                False,
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        views = [
            artifact_view(
                summary,
                case_id=typed_case_id,
                public_base_url=public_base_url,
            )
            for summary in response.artifacts
        ]
        return _json(success_envelope({"artifacts": views}))

    @app.get(
        "/api/v1/artifacts/{artifact_id}/content",
        responses={
            200: {
                "description": "Immutable downloadable artifact bytes.",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
                "headers": {
                    "Content-Length": {
                        "description": "Exact byte length of the immutable response body.",
                        "schema": {"type": "integer", "minimum": 0}
                    },
                    "Content-Type": {
                        "description": "Canonical media type recorded for the artifact.",
                        "schema": {"type": "string"},
                    },
                    "X-Content-SHA256": {
                        "description": (
                            "Lowercase SHA-256 digest of the complete response body."
                        ),
                        "schema": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        }
                    },
                    "X-Problem-Locator-Correlation-ID": {
                        "description": (
                            "Server-generated UUID for correlating this response with logs."
                        ),
                        "schema": {"type": "string"}
                    },
                },
            },
            **_ERROR_RESPONSES,
        },
        name="download_artifact",
        operation_id="download_artifact",
        summary="Download immutable artifact bytes",
        description=(
            "Streams one artifact owned by case_id. Verify Content-Length and "
            "X-Content-SHA256 before using the downloaded bytes."
        ),
        tags=["artifacts"],
        openapi_extra={
            "parameters": [
                {
                    "name": "case_id",
                    "in": "query",
                    "required": True,
                    "description": (
                        "Canonical lowercase UUID of the Case that owns the artifact; it "
                        "must be the sole query parameter."
                    ),
                    "schema": {
                        **_UUID_PATH_SCHEMA,
                        "example": _CASE_ID_EXAMPLE,
                    },
                }
            ]
        },
    )
    async def download_artifact(
        artifact_id: Annotated[
            str,
            Path(
                description="Canonical lowercase UUID of the artifact to download.",
                examples=[_ARTIFACT_ID_EXAMPLE],
                json_schema_extra={"format": "uuid"},
            ),
        ],
        request: Request,
    ):
        operation = "download_artifact"
        arguments: dict[str, Any] = {
            "artifact_id": artifact_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_artifact_id = _OPAQUE_ID.validate_python(artifact_id)
            query_items = list(request.query_params.multi_items())
            if len(query_items) != 1 or query_items[0][0] != "case_id":
                raise ValueError("case_id must be the sole query parameter")
            case_id = _OPAQUE_ID.validate_python(query_items[0][1])
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))

        try:
            result = await _port_call(
                query_port.open_artifact,
                case_id,
                typed_artifact_id,
                dispose_cancelled_result=lambda item: item.stream.close(),
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _ClosingStreamingResponse(
            result.stream,
            iterate_binary_stream(result.stream),
            media_type=None,
            headers={
                "Content-Length": str(result.artifact.size),
                "Content-Type": result.artifact.content_type,
                "X-Content-SHA256": result.artifact.sha256,
            },
        )

    # Route order matters: keep the raw MCP ASGI endpoint after all FastAPI
    # routes so no catch-all transport can shadow /live or /api/v1.
    app.add_route(
        "/mcp",
        mcp.asgi_application,
        methods=["GET", "POST", "DELETE"],
        name="mcp",
    )
    return app


__all__ = [
    "CreateCaseBody",
    "PrepareAttachmentBody",
    "SubmitSupplementBody",
    "UploadHeaders",
    "create_http_app",
    "parse_upload_headers",
]
