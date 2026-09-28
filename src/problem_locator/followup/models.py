"""Public follow-up v1 contracts; the existing Agent contracts stay unchanged."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from problem_locator.contracts.models import OpaqueId, UtcTimestamp

MAX_TEXT_BYTES = 65_536
MAX_CONTEXT_BYTES = 262_144
MAX_TURNS = 100
ACTIVE = ("QUEUED", "RUNNING", "CANCELLING")
FollowupStatus = Literal["QUEUED", "RUNNING", "CANCELLING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"]
ContextMode = Literal["REPORT_ONLY", "REPORT_AND_LOGS"]
SnapshotStatus = Literal["DISABLED", "UNAVAILABLE", "PENDING", "BUILDING", "READY", "FAILED"]


class FollowupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FollowupRequest(FollowupModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES, pattern=r"\S")

    @field_validator("text")
    @classmethod
    def text_bytes(cls, value):
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("追问不能超过 65536 字节。")
        return value


class FollowupStopRequest(FollowupModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")


class FollowupFailure(FollowupModel):
    code: Literal["AGENT_FOLLOWUP_FAILED", "AGENT_FOLLOWUP_INTERRUPTED", "AGENT_FOLLOWUP_INPUT_CHANGED"]
    message: str


class FollowupItem(FollowupModel):
    followup_id: OpaqueId
    run_id: OpaqueId
    request_id: str
    ordinal: int = Field(ge=1)
    status: FollowupStatus
    context_mode: ContextMode
    text: str
    answer_markdown: str | None = None
    failure: FollowupFailure | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def coherent_result(self):
        if (self.status == "COMPLETED") != (self.answer_markdown is not None):
            raise ValueError("回答正文必须与追问完成状态一致。")
        if self.answer_markdown is not None and (
                not self.answer_markdown.strip() or len(self.answer_markdown.encode("utf-8")) > MAX_TEXT_BYTES):
            raise ValueError("回答正文无效或超过大小限制。")
        if (self.status in {"FAILED", "INTERRUPTED"}) != (self.failure is not None):
            raise ValueError("失败说明必须与追问状态一致。")
        return self


class FollowupView(FollowupModel):
    schema_version: Literal[1] = 1
    conversation_id: OpaqueId
    run_id: OpaqueId
    can_ask: bool
    reason: Literal["DISABLED", "UNSUPPORTED", "EXPIRED", "BUSY", "LIMIT_EXCEEDED", "CONTEXT_LIMIT"] | None
    snapshot_status: SnapshotStatus
    active_followup: FollowupItem | None
    items: list[FollowupItem]
    next_cursor: str | None
    last_event_id: int = Field(ge=0)


class FollowupReceipt(FollowupModel):
    conversation_id: OpaqueId
    run_id: OpaqueId
    followup_id: OpaqueId
    request_id: str
    event_id: int = Field(ge=1)
    status: Literal["ACCEPTED"] = "ACCEPTED"


class FollowupStopReceipt(FollowupModel):
    conversation_id: OpaqueId
    run_id: OpaqueId
    followup_id: OpaqueId
    request_id: str
    event_id: int = Field(ge=1)
    status: Literal["CANCELLING", "CANCELLED", "ALREADY_FINISHED"]


class FollowupEvent(FollowupModel):
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    conversation_id: OpaqueId
    run_id: OpaqueId
    followup_id: OpaqueId
    type: Literal["followup.accepted", "followup.updated"]
    created_at: UtcTimestamp
    data: FollowupItem

    @model_validator(mode="after")
    def matching_identity(self):
        if self.data.followup_id != self.followup_id or self.data.run_id != self.run_id:
            raise ValueError("追问事件的身份不一致。")
        return self


class FollowupEventBatch(FollowupModel):
    events: list[FollowupEvent]
    stream_closed: bool


@dataclass(frozen=True, slots=True)
class FollowupSource:
    case_id: str
    source_job_id: str
    problem_text: str
    report_markdown: str
    report_sha256: str
    source_kind: Literal["GENERIC", "SKILL_DIRECT"]
    has_logs: bool
    occurred_at: str
