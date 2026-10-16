"""Website drafts and linear report versions; no new public MCP arguments."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from problem_locator.agent.models import AgentAttachment
from problem_locator.contracts.models import OpaqueId


class ContinuationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateDraftRequest(ContinuationModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    text: str = Field(default="", max_length=65536)
    effective_problem_text: str | None = Field(default=None, min_length=1, max_length=65536, pattern=r"\S")
    change_note: str = Field(default="", max_length=65536)
    diagnosis_goal: str = Field(default="", max_length=8192)
    log_mode: Literal["APPEND", "REPLACE"] = "APPEND"
    attachment_ids: list[OpaqueId] = Field(default_factory=list, max_length=20)

    @field_validator("text", "effective_problem_text", "change_note", "diagnosis_goal")
    @classmethod
    def text_bytes(cls, value):
        if value is not None and len(value.encode("utf-8")) > 65536:
            raise ValueError("输入内容不能超过 65536 字节。")
        return value

    @field_validator("attachment_ids")
    @classmethod
    def unique_attachments(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("附件列表不能重复。")
        return value


class UpdateDraftRequest(CreateDraftRequest):
    expected_revision: int = Field(ge=1)


class ConfirmDraftRequest(ContinuationModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    expected_revision: int = Field(ge=1)


class InputChanges(ContinuationModel):
    before_problem_text: str
    after_problem_text: str
    added_attachments: list[AgentAttachment] = Field(default_factory=list)
    removed_attachments: list[AgentAttachment] = Field(default_factory=list)


class ContinuationDraft(ContinuationModel):
    conversation_id: OpaqueId
    draft_id: OpaqueId
    parent_run_id: OpaqueId
    revision: int = Field(ge=1)
    status: Literal["PREPARING", "DRAFT", "FOLLOWUP", "FAILED", "CONFIRMED"]
    source_problem_text: str
    effective_problem_text: str
    change_note: str
    diagnosis_goal: str
    log_mode: Literal["APPEND", "REPLACE"]
    attachment_ids: list[OpaqueId]
    attachments: list[AgentAttachment]
    input_changes: InputChanges
    followup_text: str = ""
    confirmed_run_id: OpaqueId | None = None
    failure_message: str | None = None
    created_at: str
    updated_at: str


class ContinuationReceipt(ContinuationModel):
    conversation_id: OpaqueId
    run_id: OpaqueId
    parent_run_id: OpaqueId
    request_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"


class ReportVersion(ContinuationModel):
    run_id: OpaqueId
    parent_run_id: OpaqueId | None = None
    version: int = Field(ge=1)
    created_at: str
    input_changes: InputChanges | None = None
    diagnosis_goal: str = ""
    runtime_changes: list[str] = Field(default_factory=list)


class ReportVersions(ContinuationModel):
    conversation_id: OpaqueId
    root_run_id: OpaqueId
    latest_run_id: OpaqueId
    versions: list[ReportVersion]
    can_continue: bool
