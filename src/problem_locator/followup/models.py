"""Public follow-up v1 contracts; the existing Agent contracts stay unchanged."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from problem_locator.contracts.models import OpaqueId, ServiceTimestamp

MAX_TEXT_BYTES = 65_536
MAX_CONTEXT_BYTES = 262_144
ACTIVE = ("QUEUED", "RUNNING", "CANCELLING")
FollowupStatus = Literal["QUEUED", "RUNNING", "CANCELLING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"]
ContextMode = Literal["REPORT_ONLY", "REPORT_AND_LOGS"]
SnapshotStatus = Literal["DISABLED", "UNAVAILABLE", "PENDING", "BUILDING", "READY", "FAILED"]


class FollowupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FollowupRequest(FollowupModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES, pattern=r"\S")
    attachment_ids: list[OpaqueId] = Field(default_factory=list, max_length=20)

    intent: Literal["CORRECTION"] | None = None
    target_followup_id: OpaqueId | None = None

    @model_validator(mode="after")
    def correction_target(self):
        if self.target_followup_id is not None and self.intent != "CORRECTION":
            raise ValueError("只有纠正说明可以指定目标回答。")
        return self

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
    attachment_ids: list[OpaqueId] = Field(default_factory=list, max_length=20)
    answer_markdown: str | None = None
    diagnosis_run_id: OpaqueId | None = None
    failure: FollowupFailure | None = None
    created_at: ServiceTimestamp
    updated_at: ServiceTimestamp

    @model_validator(mode="after")
    def coherent_result(self):
        if self.diagnosis_run_id is not None and (self.status != "COMPLETED" or self.diagnosis_run_id == self.run_id):
            raise ValueError("继续诊断必须关联新的轮次，并已完成追问交接。")
        if (self.status == "COMPLETED") != (self.answer_markdown is not None):
            raise ValueError("回答正文必须与追问完成状态一致。")
        if self.answer_markdown is not None and (
                not self.answer_markdown.strip() or len(self.answer_markdown.encode("utf-8")) > MAX_TEXT_BYTES):
            raise ValueError("回答正文无效或超过大小限制。")
        if (self.status in {"FAILED", "INTERRUPTED"}) != (self.failure is not None):
            raise ValueError("失败说明必须与追问状态一致。")
        return self


RESULT_PROTOCOL = "xiaodao.followup.v1"


class FollowupResult(FollowupModel):
    """Internal model output; never exposed as a public MCP input."""

    protocol: Literal["xiaodao.followup.v1"]
    request_key: OpaqueId


class AnswerResult(FollowupResult):
    action: Literal["ANSWER", "REQUEST_INPUT"]
    answer_markdown: str = Field(min_length=1, max_length=MAX_TEXT_BYTES, pattern=r"\S")


class DiagnosisDecision(FollowupResult):
    """Internal handoff, produced by the same invocation that answers a question."""

    action: Literal["CONTINUE_DIAGNOSIS"]
    effective_problem_text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES, pattern=r"\S")
    diagnosis_goal: str = Field(min_length=1, max_length=8192, pattern=r"\S")
    reason: str = Field(min_length=1, max_length=4096, pattern=r"\S")
    attachment_ids: list[OpaqueId] = Field(max_length=20)

    @field_validator("effective_problem_text", "diagnosis_goal", "reason")
    @classmethod
    def text_bytes(cls, value):
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("继续诊断的单项内容不能超过 65536 字节。")
        return value


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
    created_at: ServiceTimestamp
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
    skill_name: str = ""
    has_log_manifest: bool = False
