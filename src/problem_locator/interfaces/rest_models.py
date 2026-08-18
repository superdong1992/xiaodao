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
    statement: NonEmptyText
    expected_behavior: NonEmptyText
    actual_behavior: NonEmptyText
    scope: NonEmptyText
    goals: list[NonEmptyText] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    non_goals: list[NonEmptyText] = Field(
        json_schema_extra={"uniqueItems": True},
    )
    constraints: list[NonEmptyText] = Field(
        json_schema_extra={"uniqueItems": True},
    )
    completion_criteria: list[NonEmptyText] = Field(
        min_length=1,
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
    name: ContractName
    value: NonEmptyText


class CreateCaseBody(_RestModel):
    request_id: NonEmptyText
    raw_problem_text: NonEmptyText
    problem_spec: ProblemSpecBody
    initial_user_facts: list[NamedValueBody] = Field(
        default_factory=list,
        max_length=MAX_INITIAL_USER_FACTS,
    )
    wait_seconds: WaitSeconds = 0

    @model_validator(mode="after")
    def validate_fact_names(self) -> CreateCaseBody:
        _unique(
            [fact.name for fact in self.initial_user_facts],
            "initial_user_fact names",
        )
        return self


class PrepareAttachmentBody(_RestModel):
    request_id: NonEmptyText
    expected_case_revision: PositiveInt
    name: NonEmptyText
    content_type: ContentType
    declared_size: NonNegativeInt
    declared_sha256: Sha256


class SubmitSupplementBody(_RestModel):
    request_id: NonEmptyText
    expected_case_revision: PositiveInt
    inputs: list[NamedValueBody]
    attachment_ids: list[OpaqueId] = Field(
        json_schema_extra={"uniqueItems": True},
    )
    wait_seconds: WaitSeconds = 0

    @model_validator(mode="after")
    def validate_supplement(self) -> SubmitSupplementBody:
        if not self.inputs and not self.attachment_ids:
            raise ValueError("supplement must contain inputs or attachment_ids")
        _unique([item.name for item in self.inputs], "input names")
        _unique(self.attachment_ids, "attachment_ids")
        return self


class WebUploadRequiredHeaders(_RestModel):
    idempotency_key: OpaqueId = Field(alias="Idempotency-Key")
    content_type: ContentType = Field(alias="Content-Type")
    content_sha256: Sha256 = Field(alias="X-Content-SHA256")


class WebUploadDescriptor(_RestModel):
    attachment_id: OpaqueId
    method: Literal["PUT"]
    url: NonEmptyText
    required_headers: WebUploadRequiredHeaders
    expected_content_length: NonNegativeInt
    max_bytes: Literal[MAX_ATTACHMENT_BYTES]
    expires_at: None = None


class PrepareAttachmentData(_RestModel):
    application_response: ApplicationResponse
    upload: WebUploadDescriptor


class UploadReadyData(_RestModel):
    attachment_id: OpaqueId
    case_id: OpaqueId
    status: Literal["READY"]
    case_revision: PositiveInt


class ArtifactListData(_RestModel):
    artifacts: list[ArtifactView]


_DataT = TypeVar("_DataT")


class SuccessEnvelope(_RestModel, Generic[_DataT]):
    ok: Literal[True] = True
    data: _DataT
    error: None = None


class ErrorEnvelope(_RestModel):
    ok: Literal[False] = False
    data: None = None
    error: ApplicationError


ApplicationSuccessEnvelope = SuccessEnvelope[ApplicationResponse]
CaseQuerySuccessEnvelope = SuccessEnvelope[CaseQueryResponse]
PrepareAttachmentSuccessEnvelope = SuccessEnvelope[PrepareAttachmentData]
UploadReadySuccessEnvelope = SuccessEnvelope[UploadReadyData]
ArtifactListSuccessEnvelope = SuccessEnvelope[ArtifactListData]


__all__ = [
    "ApplicationSuccessEnvelope",
    "ArtifactListData",
    "ArtifactListSuccessEnvelope",
    "CaseQuerySuccessEnvelope",
    "CreateCaseBody",
    "ErrorEnvelope",
    "NamedValueBody",
    "PrepareAttachmentBody",
    "PrepareAttachmentData",
    "PrepareAttachmentSuccessEnvelope",
    "ProblemSpecBody",
    "SubmitSupplementBody",
    "SuccessEnvelope",
    "UploadReadyData",
    "UploadReadySuccessEnvelope",
    "WebUploadDescriptor",
    "WebUploadRequiredHeaders",
]
