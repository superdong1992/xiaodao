"""Strict browser-facing REST request and response DTOs."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from problem_locator.contracts.commands import (
    ApplicationResponse,
    ArtifactView,
    CaseQueryResponse,
)
from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_INITIAL_USER_FACTS,
)
from problem_locator.contracts.models import (
    ApplicationError,
    ContentType,
    ContractName,
    NonEmptyText,
    NonNegativeInt,
    OpaqueId,
    PositiveInt,
    Sha256,
    WaitSeconds,
)


class _RestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class ProblemSpecBody(_RestModel):
    """Structured problem definition supplied when a Case is created."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "statement": "RPC request times out.",
                    "expected_behavior": "The RPC succeeds within the deadline.",
                    "actual_behavior": "The caller reports a timeout.",
                    "scope": "Payment to inventory RPC.",
                    "goals": ["Locate the timeout cause."],
                    "non_goals": [],
                    "constraints": ["Use only supplied evidence."],
                    "completion_criteria": ["Identify an evidenced cause."],
                }
            ]
        }
    )

    statement: NonEmptyText = Field(
        description="Concise statement of the problem to diagnose.",
        examples=["RPC request times out."],
    )
    expected_behavior: NonEmptyText = Field(
        description="Behavior that should occur when the system works correctly.",
        examples=["The RPC succeeds within the deadline."],
    )
    actual_behavior: NonEmptyText = Field(
        description="Behavior currently observed by the user.",
        examples=["The caller reports a timeout."],
    )
    scope: NonEmptyText = Field(
        description="System boundary within which the diagnosis should operate.",
        examples=["Payment to inventory RPC."],
    )
    goals: list[NonEmptyText] = Field(
        min_length=1,
        description="Outcomes the diagnosis should achieve; values must be unique.",
        examples=[["Locate the timeout cause."]],
        json_schema_extra={"uniqueItems": True},
    )
    non_goals: list[NonEmptyText] = Field(
        description="Explicitly excluded outcomes; values must be unique.",
        examples=[[]],
        json_schema_extra={"uniqueItems": True},
    )
    constraints: list[NonEmptyText] = Field(
        description="Operational or evidence constraints; values must be unique.",
        examples=[["Use only supplied evidence."]],
        json_schema_extra={"uniqueItems": True},
    )
    completion_criteria: list[NonEmptyText] = Field(
        min_length=1,
        description="Observable criteria used to decide that the work is complete.",
        examples=[["Identify an evidenced cause."]],
        json_schema_extra={"uniqueItems": True},
    )

    @model_validator(mode="after")
    def validate_unique_lists(self) -> ProblemSpecBody:
        for field_name in (
            "goals",
            "non_goals",
            "constraints",
            "completion_criteria",
        ):
            _unique(getattr(self, field_name), field_name)
        return self


class NamedValueBody(_RestModel):
    """One named user-supplied fact or supplemental input."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"name": "problem_time", "value": "2026-08-18 10:30"}]
        }
    )

    name: ContractName = Field(
        description="Stable input name requested by the server or chosen for an initial fact.",
        examples=["problem_time"],
    )
    value: NonEmptyText = Field(
        description="Non-empty text value associated with the input name.",
        examples=["2026-08-18 10:30"],
    )


class CreateCaseBody(_RestModel):
    """Request body for creating a new diagnosis Case."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "10000000-0000-0000-0000-000000000001",
                    "raw_problem_text": "RPC request times out.",
                    "problem_spec": ProblemSpecBody.model_config["json_schema_extra"][
                        "examples"
                    ][0],
                    "initial_user_facts": [
                        {
                            "name": "problem_time",
                            "value": "2026-08-18 10:30",
                        }
                    ],
                    "wait_seconds": 0,
                }
            ]
        }
    )

    request_id: NonEmptyText = Field(
        description=(
            "Client-generated idempotency key for this logical write. Reuse the same "
            "value and business content when retrying the request."
        ),
        examples=["10000000-0000-0000-0000-000000000001"],
    )
    raw_problem_text: NonEmptyText = Field(
        description="Original human-readable problem statement retained with the Case.",
        examples=["RPC request times out."],
    )
    problem_spec: ProblemSpecBody = Field(
        description="Structured diagnosis scope and completion definition."
    )
    initial_user_facts: list[NamedValueBody] = Field(
        default_factory=list,
        max_length=MAX_INITIAL_USER_FACTS,
        description=(
            "Optional initial facts. Names must be unique and the list may contain at "
            f"most {MAX_INITIAL_USER_FACTS} entries."
        ),
        json_schema_extra={"default": []},
    )
    wait_seconds: WaitSeconds = Field(
        default=0,
        description=(
            "How long the server may wait for the created Job to change state before "
            "returning; 0 returns without long polling."
        ),
        examples=[0, 30],
    )

    @model_validator(mode="after")
    def validate_fact_names(self) -> CreateCaseBody:
        _unique(
            [fact.name for fact in self.initial_user_facts],
            "initial_user_fact names",
        )
        return self


class PrepareAttachmentBody(_RestModel):
    """Metadata used to reserve one immutable attachment upload."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "10000000-0000-0000-0000-000000000002",
                    "expected_case_revision": 1,
                    "name": "logs.zip",
                    "content_type": "application/zip",
                    "declared_size": 1024,
                    "declared_sha256": "a" * 64,
                }
            ]
        }
    )

    request_id: NonEmptyText = Field(
        description="Client-generated idempotency key for this attachment reservation.",
        examples=["10000000-0000-0000-0000-000000000002"],
    )
    expected_case_revision: PositiveInt = Field(
        description=(
            "Latest case_revision observed by the client. A stale value produces a "
            "revision conflict."
        ),
        examples=[1],
    )
    name: NonEmptyText = Field(
        description="Original filename, including an allowed archive suffix.",
        examples=["logs.zip"],
    )
    content_type: ContentType = Field(
        description="Canonical media type of the bytes that will be uploaded.",
        examples=["application/zip"],
    )
    declared_size: NonNegativeInt = Field(
        description="Exact byte length of the file that will be uploaded.",
        examples=[1024],
        json_schema_extra={"maximum": MAX_ATTACHMENT_BYTES},
    )
    declared_sha256: Sha256 = Field(
        description="Lowercase hexadecimal SHA-256 digest of the complete file bytes.",
        examples=["a" * 64],
    )


class SubmitSupplementBody(_RestModel):
    """Facts and READY attachments submitted in response to open requirements."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "10000000-0000-0000-0000-000000000003",
                    "expected_case_revision": 2,
                    "inputs": [{"name": "problem_time", "value": "10:30"}],
                    "attachment_ids": [
                        "00000000-0000-0000-0000-000000000003"
                    ],
                    "wait_seconds": 0,
                }
            ]
        }
    )

    request_id: NonEmptyText = Field(
        description="Client-generated idempotency key for this logical supplement.",
        examples=["10000000-0000-0000-0000-000000000003"],
    )
    expected_case_revision: PositiveInt = Field(
        description=(
            "Latest case_revision observed after any attachment upload. Do not use "
            "diagnosis_state_revision."
        ),
        examples=[2],
    )
    inputs: list[NamedValueBody] = Field(
        description="Named text inputs; names must be unique within the request."
    )
    attachment_ids: list[OpaqueId] = Field(
        description=(
            "READY attachment UUIDs belonging to this Case; values must be unique."
        ),
        examples=[["00000000-0000-0000-0000-000000000003"]],
        json_schema_extra={"uniqueItems": True},
    )
    wait_seconds: WaitSeconds = Field(
        default=0,
        description=(
            "How long the server may wait for the resulting Job state change; 0 returns "
            "without long polling."
        ),
        examples=[0, 30],
    )

    @model_validator(mode="after")
    def validate_supplement(self) -> SubmitSupplementBody:
        if not self.inputs and not self.attachment_ids:
            raise ValueError("supplement must contain inputs or attachment_ids")
        _unique([item.name for item in self.inputs], "input names")
        _unique(self.attachment_ids, "attachment_ids")
        return self


class WebUploadRequiredHeaders(_RestModel):
    """Header values the browser must use for the raw upload request."""

    idempotency_key: OpaqueId = Field(
        alias="Idempotency-Key",
        description="Must exactly equal the attachment_id in the upload URL.",
    )
    content_type: ContentType = Field(
        alias="Content-Type",
        description="Must equal the content_type declared while preparing the upload.",
    )
    content_sha256: Sha256 = Field(
        alias="X-Content-SHA256",
        description="Must equal the declared lowercase SHA-256 digest.",
    )


class WebUploadDescriptor(_RestModel):
    """Browser-ready instructions returned after reserving an attachment."""

    attachment_id: OpaqueId = Field(
        description="UUID of the reserved attachment and upload idempotency key."
    )
    method: Literal["PUT"] = Field(description="HTTP method required by the upload URL.")
    url: NonEmptyText = Field(description="Absolute raw-byte upload URL.")
    required_headers: WebUploadRequiredHeaders = Field(
        description="Headers JavaScript must set on the raw upload request."
    )
    expected_content_length: NonNegativeInt = Field(
        description="Exact byte length the browser-generated Content-Length must contain."
    )
    max_bytes: Literal[MAX_ATTACHMENT_BYTES] = Field(
        description="Maximum accepted attachment size in bytes for this API version."
    )
    expires_at: None = Field(
        default=None,
        description="Always null in V1 because reserved upload URLs do not expire.",
    )


class PrepareAttachmentData(_RestModel):
    """Case mutation receipt plus the reserved raw upload instructions."""

    application_response: ApplicationResponse = Field(
        description="Mutation receipt and current Case view, when immediately available."
    )
    upload: WebUploadDescriptor = Field(
        description="Instructions for the next raw-byte PUT request."
    )


class UploadReadyData(_RestModel):
    """Confirmation that uploaded bytes were verified and made READY."""

    attachment_id: OpaqueId = Field(description="UUID of the verified attachment.")
    case_id: OpaqueId = Field(description="UUID of the Case that owns the attachment.")
    status: Literal["READY"] = Field(
        description="READY means the attachment may be referenced by a supplement."
    )
    case_revision: PositiveInt = Field(
        description="New Case revision after the upload became READY."
    )


class ArtifactListData(_RestModel):
    """Downloadable artifact metadata visible to browser clients."""

    artifacts: list[ArtifactView] = Field(
        description="Public downloadable artifacts belonging to the requested Case."
    )


class LiveData(_RestModel):
    """Process liveness result."""

    status: Literal["live"] = Field(
        description="Literal liveness marker returned while the process can serve HTTP."
    )


class ReadinessCheckData(_RestModel):
    """One sanitized service readiness check."""

    name: Literal["CONFIG", "INSTANCE_LOCK", "STATE", "DATA_DIRECTORIES", "RECOVERY"] = (
        Field(description="Stable readiness subsystem name.")
    )
    passed: bool = Field(description="Whether this subsystem is ready to serve requests.")
    message: None = Field(
        default=None,
        description="Always null; infrastructure details are not exposed to clients.",
    )


class ReadinessData(_RestModel):
    """Successful service readiness result."""

    ready: Literal[True] = Field(description="True when every readiness check passed.")
    checks: list[ReadinessCheckData] = Field(
        description="Sanitized result for every required service subsystem."
    )
    error: None = Field(
        default=None,
        description="Always null in a successful readiness response.",
    )


_DataT = TypeVar("_DataT")


class SuccessEnvelope(_RestModel, Generic[_DataT]):
    """Uniform envelope for every successful JSON response."""

    ok: Literal[True] = Field(
        default=True,
        description="Always true for a successful response.",
    )
    data: _DataT = Field(description="Operation-specific successful response payload.")
    error: None = Field(
        default=None,
        description="Always null for a successful response.",
    )


class ErrorEnvelope(_RestModel):
    """Uniform envelope for every JSON error response."""

    ok: Literal[False] = Field(
        default=False,
        description="Always false for an error response.",
    )
    data: None = Field(
        default=None,
        description="Always null for an error response.",
    )
    error: ApplicationError = Field(
        description="Stable machine-readable error code, safe message, and details."
    )


ApplicationSuccessEnvelope = SuccessEnvelope[ApplicationResponse]
CaseQuerySuccessEnvelope = SuccessEnvelope[CaseQueryResponse]
PrepareAttachmentSuccessEnvelope = SuccessEnvelope[PrepareAttachmentData]
UploadReadySuccessEnvelope = SuccessEnvelope[UploadReadyData]
ArtifactListSuccessEnvelope = SuccessEnvelope[ArtifactListData]
LiveSuccessEnvelope = SuccessEnvelope[LiveData]
ReadinessSuccessEnvelope = SuccessEnvelope[ReadinessData]


__all__ = [
    "ApplicationSuccessEnvelope",
    "ArtifactListData",
    "ArtifactListSuccessEnvelope",
    "CaseQuerySuccessEnvelope",
    "CreateCaseBody",
    "ErrorEnvelope",
    "LiveData",
    "LiveSuccessEnvelope",
    "NamedValueBody",
    "PrepareAttachmentBody",
    "PrepareAttachmentData",
    "PrepareAttachmentSuccessEnvelope",
    "ProblemSpecBody",
    "ReadinessCheckData",
    "ReadinessData",
    "ReadinessSuccessEnvelope",
    "SubmitSupplementBody",
    "SuccessEnvelope",
    "UploadReadyData",
    "UploadReadySuccessEnvelope",
    "WebUploadDescriptor",
    "WebUploadRequiredHeaders",
]
