"""Website Agent contracts: conversation detail v3 and run-aware events v2."""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from problem_locator.contracts.models import GenericResult, NonEmptyText, OpaqueId, UserResultPayloadV3, UtcTimestamp

PUBLIC_PROGRESS_MESSAGES = {
    "INTAKE": "正在整理问题", "ROUTE": "正在选择定位方法",
    "LOGPARSE": "正在解析日志", "DIAGNOSE": "正在分析日志",
    "REVIEW": "正在审核结论", "ARCHIVE": "正在整理目标日志",
    "ROUTING": "正在选择定位方法", "DIAGNOSING": "正在分析日志",
    "VERIFYING": "正在核对证据", "REVIEWING": "正在审核结论",
    "REPORTING": "正在生成定位报告",
}

ConversationStatus = Literal["INTAKE", "WAITING_INPUT", "RUNNING", "CANCELLING", "CANCELLED", "COMPLETED", "FAILED", "INTERRUPTED"]
MessageStatus = Literal["QUEUED", "PROCESSING", "APPLIED", "UNUSED"]
EventType = Literal["message.accepted", "message.updated", "assistant.question", "agent.progress",
                    "case.updated", "result.available", "archive.updated", "agent.failed",
                    "conversation.interrupted", "conversation.completed", "attachment.updated", "run.started", "run.stopping"]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class AgentPublicFailure(AgentModel):
    """Safe failure/operational diagnostics; never permission to rerun a model."""

    code: NonEmptyText
    message: NonEmptyText
    details: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    retryable: Literal[False] = False


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
    run_id: OpaqueId | None = None


class ConversationReceipt(AgentModel):
    conversation_id: str
    request_id: str
    schema_version: Literal[1] = 1
    run_id: OpaqueId | None = None


class AgentMessage(AgentModel):
    message_id: str
    request_id: str
    text: str
    attachment_ids: list[str]
    status: MessageStatus
    created_at: str
    notice: str | None = None
    run_id: OpaqueId | None = None


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
    failure: AgentPublicFailure | None = None
    messages: list[AgentMessage] = Field(default_factory=list)
    attachments: list[AgentAttachment] = Field(default_factory=list)
    last_event_id: int = 0
    created_at: str
    updated_at: str
    run_id: OpaqueId | None = None


class ConversationStatusView(AgentModel):
    """会话的轻量状态，不加载消息正文或附件历史。"""

    schema_version: Literal[1] = 1
    conversation_id: OpaqueId
    status: ConversationStatus
    case_id: OpaqueId | None = None
    job_id: OpaqueId | None = None
    case_status: str | None = None
    archive_status: Literal["NOT_REQUIRED", "PENDING", "READY", "FAILED"] = "NOT_REQUIRED"
    current_questions: list[str] = Field(default_factory=list)
    failure: AgentPublicFailure | None = None
    report_state: Literal["PENDING", "READY", "UNAVAILABLE"]
    last_event_id: int = 0
    created_at: str
    updated_at: str
    run_id: OpaqueId | None = None


class ConversationRun(AgentModel):
    run_id: OpaqueId
    ordinal: int = Field(ge=1)
    status: ConversationStatus
    case_id: OpaqueId | None = None
    job_id: OpaqueId | None = None
    case_status: str | None = None
    archive_status: Literal["NOT_REQUIRED", "PENDING", "READY", "FAILED"] = "NOT_REQUIRED"
    report_state: Literal["PENDING", "READY", "UNAVAILABLE"]
    created_at: str
    updated_at: str


class ConversationCapabilities(AgentModel):
    can_send: bool
    can_stop: bool
    can_rediagnose: bool
    can_rename: bool
    can_delete: bool


class ConversationResultSummary(AgentModel):
    status: Literal["COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"]
    case_id: OpaqueId | None = None
    case_status: str | None = None
    report_state: Literal["READY", "UNAVAILABLE"]
    source_job_id: OpaqueId | None = None
    failure: AgentPublicFailure | None = None


class ConversationHistoryEntry(AgentModel):
    id: str
    run_id: OpaqueId
    type: Literal["user.message", "assistant.question", "diagnosis.result"]
    created_at: str
    message: AgentMessage | None = None
    questions: list[str] | None = None
    result: ConversationResultSummary | None = None

    @model_validator(mode="after")
    def entry_shape(self):
        field = {"user.message": "message", "assistant.question": "questions", "diagnosis.result": "result"}[self.type]
        if getattr(self, field) is None or any(getattr(self, other) is not None for other in {"message", "questions", "result"} - {field}):
            raise ValueError("历史条目的内容必须与类型一致。")
        if self.message is not None and self.message.run_id != self.run_id:
            raise ValueError("历史消息必须属于对应诊断。")
        return self


class ConversationSummary(AgentModel):
    conversation_id: OpaqueId
    title: str
    current_run: ConversationRun
    capabilities: ConversationCapabilities
    created_at: str
    updated_at: str


class ConversationList(AgentModel):
    items: list[ConversationSummary]
    next_cursor: str | None = None


class StopReceipt(AgentModel):
    conversation_id: OpaqueId
    run_id: OpaqueId
    request_id: str
    status: Literal["CANCELLING", "CANCELLED", "ALREADY_FINISHED"]


class DeleteReceipt(AgentModel):
    conversation_id: OpaqueId
    status: Literal["DELETING", "DELETED"]


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


class ConversationReportView(AgentModel):
    """按会话读取已发布报告；报告可读性与归档状态相互独立。"""

    schema_version: Literal[1] = 1
    conversation_id: OpaqueId
    case_id: OpaqueId | None = None
    case_revision: int | None = Field(default=None, ge=1)
    case_status: str | None = None
    archive_status: Literal["NOT_REQUIRED", "PENDING", "READY", "FAILED"] = "NOT_REQUIRED"
    report_state: Literal["PENDING", "READY", "UNAVAILABLE"]
    source_job_id: OpaqueId | None = None
    format: Literal["problem-locator-diagnosis-v3", "markdown", "generic-v1"] | None = None
    report: UserResultPayloadV3 | GenericResult | None = None
    markdown: str | None = None
    artifact: PublicArtifactData | None = None
    failure: AgentPublicFailure | None = None

    @model_validator(mode="after")
    def report_shape(self):
        if self.report_state != "READY":
            if any(value is not None for value in (
                self.source_job_id, self.format, self.report, self.markdown, self.artifact,
            )):
                raise ValueError("报告未就绪时不能返回报告正文或产物。")
            return self
        if self.case_id is None or self.case_revision is None or self.source_job_id is None:
            raise ValueError("正式报告必须关联 Case、版本及来源任务。")
        if self.format == "problem-locator-diagnosis-v3":
            expected = {"RESOLVED": "COMPLETED", "PARTIALLY_RESOLVED": "PARTIAL", "UNRESOLVED": "INCONCLUSIVE"}
            if (not isinstance(self.report, UserResultPayloadV3) or self.markdown is not None
                    or self.artifact is None or self.artifact.kind != "USER_RESULT"
                    or self.report.status != expected.get(self.case_status)):
                raise ValueError("诊断报告的格式、正文或状态不一致。")
        elif self.format == "markdown":
            if (self.report is not None or not self.markdown or self.artifact is None
                    or self.artifact.kind != "GENERIC_REPORT"):
                raise ValueError("Markdown 报告必须包含正文及对应产物。")
        elif self.format == "generic-v1":
            if (not isinstance(self.report, GenericResult) or self.markdown is not None
                    or self.artifact is not None or self.report.source_job_id != self.source_job_id):
                raise ValueError("历史通用诊断报告必须保留原始结果及来源任务。")
        else:
            raise ValueError("正式报告必须声明支持的格式。")
        if self.artifact is not None and self.artifact.created_by_job_id != self.source_job_id:
            raise ValueError("报告产物与来源任务不一致。")
        return self


class ConversationArtifact(PublicArtifactData):
    """应用返回产物身份；HTTP 入口补充实际下载地址。"""

    download_url: str | None = None


class ConversationDetail(ConversationStatusView):
    """一个会话读取入口；未请求的内容为 null，不代表内容为空。"""

    schema_version: Literal[3] = 3
    title: str = "新诊断"
    current_run: ConversationRun | None = None
    capabilities: ConversationCapabilities | None = None
    selected_run_id: OpaqueId | None = None
    history: list[ConversationHistoryEntry] | None = None
    history_next_cursor: str | None = None
    case_revision: int | None = Field(default=None, ge=1)
    source_job_id: OpaqueId | None = None
    progress: AgentProgressData | None = None
    included: list[Literal["history", "report", "artifacts"]] = Field(default_factory=list)
    attachments: list[AgentAttachment] | None = None
    result: ConversationReportView | None = None
    artifacts: list[ConversationArtifact] | None = None

    @property
    def messages(self):
        return None if self.history is None else [item.message for item in self.history if item.type == "user.message"]

    @model_validator(mode="after")
    def included_content(self):
        if len(self.included) != len(set(self.included)):
            raise ValueError("会话内容选项不能重复。")
        if ("history" in self.included) != (self.history is not None and self.attachments is not None):
            raise ValueError("消息和附件历史必须与请求的内容选项一致。")
        if "history" not in self.included and (self.history is not None or self.attachments is not None):
            raise ValueError("未请求历史时不能返回部分历史。")
        if ("report" in self.included) != (self.result is not None):
            raise ValueError("报告结果必须与请求的内容选项一致。")
        if ("artifacts" in self.included) != (self.artifacts is not None):
            raise ValueError("产物列表必须与请求的内容选项一致。")
        if self.result is not None and (
                self.result.conversation_id != self.conversation_id
                or self.result.case_id != self.case_id
                or self.result.case_revision != self.case_revision
                or self.result.report_state != self.report_state
                or self.result.source_job_id != self.source_job_id
                or self.result.case_status != self.case_status
                or self.result.archive_status != self.archive_status):
            raise ValueError("报告必须与当前会话的任务快照一致。")
        if self.artifacts is not None:
            identities = [artifact.artifact_id for artifact in self.artifacts]
            if len(identities) != len(set(identities)) or any(
                    self.source_job_id is None or artifact.created_by_job_id != self.source_job_id
                    for artifact in self.artifacts):
                raise ValueError("会话产物必须唯一且属于当前结果任务。")
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
    status: Literal["COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"]


class RunStartedData(AgentModel):
    ordinal: int = Field(ge=1)


class RunStoppingData(AgentModel):
    status: Literal["CANCELLING"] = "CANCELLING"


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
    "run.started": RunStartedData,
    "run.stopping": RunStoppingData,
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
    schema_version: Literal[2] = 2
    sequence: int = Field(ge=1)
    conversation_id: str
    case_id: str | None = None
    job_id: str | None = None
    run_id: OpaqueId
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
        expected = set(model.model_fields)
        if set(self.data) != expected:
            raise ValueError("公共事件包含未定义的字段。")
        model.model_validate(self.data)
        if self.type == "message.accepted" and self.data["run_id"] != self.run_id:
            raise ValueError("事件消息必须属于对应诊断。")
        return self


class AgentStoreError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, *, details=None, retryable=False):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code
        self.details, self.retryable = details or [], retryable


AgentError = AgentStoreError
