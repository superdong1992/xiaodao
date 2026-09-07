"""Version 1 website Agent contracts, independent of core Job permissions."""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from problem_locator.contracts.models import NonEmptyText, OpaqueId, UtcTimestamp

PUBLIC_PROGRESS_MESSAGES = {
    "INTAKE": "正在整理问题", "ROUTE": "正在选择定位方法",
    "LOGPARSE": "正在解析日志", "DIAGNOSE": "正在核对证据",
    "REVIEW": "正在审核结论", "ARCHIVE": "正在整理目标日志",
    "ROUTING": "正在选择定位方法", "DIAGNOSING": "正在分析日志",
    "VERIFYING": "正在核对证据", "REVIEWING": "正在审核结论",
    "REPORTING": "正在生成定位报告",
}

ConversationStatus = Literal["INTAKE", "WAITING_INPUT", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"]
MessageStatus = Literal["QUEUED", "PROCESSING", "APPLIED", "UNUSED"]
EventType = Literal["message.accepted", "message.updated", "assistant.question", "agent.progress",
                    "case.updated", "result.available", "archive.updated", "agent.failed",
                    "conversation.interrupted", "conversation.completed", "attachment.updated"]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class CreateConversationRequest(AgentModel):
    request_id: NonEmptyText


class SendMessageRequest(AgentModel):
    request_id: NonEmptyText
    text: Annotated[str, Field(max_length=65536)] = ""
    attachment_ids: Annotated[list[OpaqueId], Field(max_length=20)] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return "" if value is None else value

    @model_validator(mode="after")
    def validate_content(self):
        if len(self.text.encode("utf-8")) > 65536:
            raise ValueError("消息不能超过 65536 字节。")
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("请输入问题或选择已经上传的日志附件。")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("附件列表不能重复。")
        return self


class MessageReceipt(AgentModel):
    conversation_id: str
    message_id: str
    request_id: str
    event_id: int
    status: Literal["ACCEPTED"] = "ACCEPTED"


class ConversationReceipt(AgentModel):
    conversation_id: str
    request_id: str
    schema_version: Literal[1] = 1


class AgentMessage(AgentModel):
    message_id: str
    request_id: str
    text: str
    attachment_ids: list[str]
    status: MessageStatus
    created_at: str
    notice: str | None = None


class AgentAttachment(AgentModel):
    attachment_id: str
    conversation_id: str
    request_id: str
    name: str
    content_type: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["RESERVED", "UPLOADING", "READY", "IMPORTED", "FAILED"] = "RESERVED"
    created_at: str
    case_attachment_id: str | None = None


class AttachmentRecord(AgentAttachment):
    storage_path: str | None = Field(default=None, exclude=True)


class ConversationView(AgentModel):
    schema_version: Literal[1] = 1
    conversation_id: str
    status: ConversationStatus
    case_id: str | None = None
    job_id: str | None = None
    case_status: str | None = None
    archive_status: Literal["NOT_REQUIRED", "PENDING", "READY", "FAILED"] = "NOT_REQUIRED"
    current_questions: list[str] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    attachments: list[AgentAttachment] = Field(default_factory=list)
    last_event_id: int = 0
    created_at: str
    updated_at: str


class MessageUpdatedData(AgentModel):
    message_id: NonEmptyText
    status: MessageStatus
    notice: NonEmptyText | None


class AssistantQuestionData(AgentModel):
    questions: Annotated[list[NonEmptyText], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def bound_total_text(self):
        if sum(len(item.encode("utf-8")) for item in self.questions) > 65536:
            raise ValueError("追问总长度不能超过 65536 字节。")
        return self


class AgentProgressData(AgentModel):
    stage: Literal["INTAKE", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW", "ARCHIVE",
                   "ROUTING", "DIAGNOSING", "VERIFYING", "REVIEWING", "REPORTING"]
    message: NonEmptyText

    @model_validator(mode="after")
    def fixed_message(self):
        if self.message != PUBLIC_PROGRESS_MESSAGES[self.stage]:
            raise ValueError("进展消息必须使用对应阶段的固定文案。")
        return self


class CaseUpdatedData(AgentModel):
    status: Literal["NEW", "RUNNING", "WAITING_INPUT", "WAITING_ATTACHMENT", "REVIEWING",
                    "RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED", "INTERRUPTED"]
    case_revision: int = Field(ge=1)


class PublicArtifactData(AgentModel):
    """Only downloadable metadata; internal Artifact fields cannot be nested."""
    artifact_id: OpaqueId
    kind: Literal["USER_RESULT", "USER_RESULT_ARCHIVE", "GENERIC_REPORT", "AUDIT_BUNDLE"]
    name: NonEmptyText
    content_type: Literal["application/json", "application/zip", "text/markdown"]
    resource_kind: Literal["FILE"]
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by_job_id: OpaqueId
    created_at: UtcTimestamp
    downloadable: bool

    @model_validator(mode="after")
    def downloadable_shape(self):
        expected = {"USER_RESULT": "application/json", "USER_RESULT_ARCHIVE": "application/zip",
                    "GENERIC_REPORT": "text/markdown", "AUDIT_BUNDLE": "application/zip"}
        if not self.downloadable or self.content_type != expected[self.kind]:
            raise ValueError("事件产物必须是可下载且类型一致的公开文件。")
        return self


class ResultAvailableData(AgentModel):
    status: Literal["RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED"]
    artifacts: Annotated[list[PublicArtifactData], Field(max_length=1)]
    result_field: Literal["final_result", "unresolved_result", "generic_result", "generic_result_v2"]

    @model_validator(mode="after")
    def result_shape(self):
        if self.result_field == "generic_result":
            if self.artifacts:
                raise ValueError("Generic V1 结果从 Case 字段读取。")
        else:
            kind = "GENERIC_REPORT" if self.result_field == "generic_result_v2" else "USER_RESULT"
            if len(self.artifacts) != 1 or self.artifacts[0].kind != kind:
                raise ValueError("报告事件必须引用对应的唯一公开报告。")
        if (self.result_field == "unresolved_result" and self.status != "UNRESOLVED") or (
            self.result_field == "final_result" and self.status == "UNRESOLVED"
        ):
            raise ValueError("报告字段与任务状态不一致。")
        return self


class ArchiveUpdatedData(AgentModel):
    status: Literal["NOT_REQUIRED", "PENDING", "READY", "FAILED"]
    artifacts: Annotated[list[PublicArtifactData], Field(max_length=2)]

    @model_validator(mode="after")
    def archive_kinds(self):
        if any(item.kind not in {"USER_RESULT_ARCHIVE", "AUDIT_BUNDLE"} for item in self.artifacts):
            raise ValueError("归档事件只能引用日志归档或审计包。")
        return self


class AgentFailedData(AgentModel):
    code: Literal["AGENT_EXECUTION_FAILED"]
    message: Literal["本次定位未能完成，请重新发起任务。"]


class ConversationInterruptedData(AgentModel):
    code: Literal["AGENT_INTERRUPTED"]
    message: Literal["服务已重启，本次任务已中断，请重新发起。"]


class ConversationCompletedData(AgentModel):
    status: Literal["COMPLETED", "FAILED", "INTERRUPTED"]


EVENT_PAYLOAD_MODELS: dict[str, type[AgentModel]] = {
    "message.accepted": AgentMessage,
    "message.updated": MessageUpdatedData,
    "assistant.question": AssistantQuestionData,
    "agent.progress": AgentProgressData,
    "case.updated": CaseUpdatedData,
    "result.available": ResultAvailableData,
    "archive.updated": ArchiveUpdatedData,
    "agent.failed": AgentFailedData,
    "conversation.interrupted": ConversationInterruptedData,
    "conversation.completed": ConversationCompletedData,
    "attachment.updated": AgentAttachment,
}


def _inline_payload_schema(model):
    """Make one finite payload schema independent of an OpenAPI ref template."""
    root = deepcopy(model.model_json_schema())
    root["required"] = list(model.model_fields)
    definitions = root.pop("$defs", {})

    def inline(node):
        if isinstance(node, list):
            return [inline(item) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if reference:
            if not reference.startswith("#/$defs/"):
                raise ValueError("unexpected payload schema reference")
            target = deepcopy(definitions[reference.removeprefix("#/$defs/")])
            target.update({key: value for key, value in node.items() if key != "$ref"})
            return inline(target)
        return {key: inline(value) for key, value in node.items()}

    return inline(root)


class AgentEvent(AgentModel):
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    conversation_id: str
    case_id: str | None = None
    job_id: str | None = None
    type: EventType
    created_at: str
    data: dict[str, JsonValue]

    payload_models: ClassVar = EVENT_PAYLOAD_MODELS

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler.resolve_ref_schema(handler(core_schema))
        # Keep the wire object unchanged while documenting the correlation
        # between event type and its concrete data, including nested metadata.
        schema["properties"]["data"] = {"title": "Data"}
        schema["oneOf"] = [
            {"properties": {"type": {"const": kind, "type": "string"},
                            "data": _inline_payload_schema(model)}, "required": ["type", "data"]}
            for kind, model in cls.payload_models.items()
        ]
        return schema

    @model_validator(mode="after")
    def validate_public_data(self):
        model = self.payload_models[self.type]
        if set(self.data) != set(model.model_fields):
            raise ValueError("公共事件包含未定义的字段。")
        model.model_validate(self.data)
        return self


class AgentStoreError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


AgentError = AgentStoreError
