"""Public website Agent routes; model execution never runs in an HTTP reader."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from problem_locator.agent.models import (
    AgentAttachment,
    AgentEvent,
    AgentStoreError,
    ConversationDetail,
    ConversationReceipt,
    ConversationList,
    ConversationSummary,
    StopReceipt,
    DeleteReceipt,
    MessageReceipt,
    PublicArtifactData,
)
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import NonEmptyText, OpaqueId, Sha256
from problem_locator.contracts.serialization import canonical_json_bytes

from .error_mapping import error_envelope, http_status_for, model_json, success_envelope
from .http_streaming import AsyncRequestBinaryStream
from .projections import append_public_path
from .rest_models import SuccessEnvelope, WebUploadDescriptor, WebUploadRequiredHeaders


_LOGGER = logging.getLogger(__name__)
_PREFIX = "/api/v1/agent"
_CURSOR = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_EVENT_BATCH_SIZE = 20
_EVENT_POLL_SECONDS = 0.5
_HEARTBEAT_SECONDS = 15.0
_MAX_EVENT_BYTES = 1024 * 1024
_CONVERSATION_INCLUDES = ("history", "report", "artifacts")
_OWNER = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_OWNER_PARAMETER = {"name": "X-Agent-Owner-Key", "in": "header", "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "description": "可信网站后端从已认证身份派生的稳定归属键；禁止采用浏览器自报值。"}


class _AgentHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateConversationBody(_AgentHttpModel):
    request_id: NonEmptyText = Field(description="同一次创建重试时保持不变。")


class RenameConversationBody(_AgentHttpModel):
    title: str = Field(min_length=1, max_length=80, pattern=r"\S")


class StopConversationBody(_AgentHttpModel):
    request_id: NonEmptyText
    run_id: OpaqueId


class SendMessageBody(_AgentHttpModel):
    request_id: NonEmptyText = Field(description="同一条消息重试时保持不变。")
    text: NonEmptyText | None = None
    attachment_ids: list[OpaqueId] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_content(self) -> SendMessageBody:
        if not self.text and not self.attachment_ids:
            raise ValueError("请输入问题或选择已经上传的日志附件。")
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("附件列表不能重复。")
        return self


class PrepareAgentAttachmentBody(_AgentHttpModel):
    conversation_id: OpaqueId
    request_id: NonEmptyText
    name: NonEmptyText
    content_type: Literal["application/zip", "application/gzip", "application/x-tar"]
    declared_size: int = Field(ge=1, le=MAX_ATTACHMENT_BYTES)
    declared_sha256: Sha256


class PreparedAgentAttachment(_AgentHttpModel):
    attachment: AgentAttachment
    upload: WebUploadDescriptor


class ConversationDownloadArtifact(PublicArtifactData):
    download_url: NonEmptyText


class ConversationDetailResponse(ConversationDetail):
    artifacts: list[ConversationDownloadArtifact] | None = None


class AgentHttpError(_AgentHttpModel):
    code: str
    message: str
    details: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    retryable: bool = False


class AgentErrorEnvelope(_AgentHttpModel):
    ok: Literal[False] = False
    data: None = None
    error: AgentHttpError


class AgentEventBatch(_AgentHttpModel):
    events: list[AgentEvent] = Field(max_length=_EVENT_BATCH_SIZE)
    stream_closed: bool


class _SseResponse(StreamingResponse):
    media_type = "text/event-stream"


def _failure(code: str, message: str, status: int, *, retryable: bool = False, details=None) -> JSONResponse:
    value = AgentErrorEnvelope(error=AgentHttpError(
        code=code, message=message, retryable=retryable, details=details or [],
    ))
    return JSONResponse(value.model_dump(mode="json"), status_code=status)


def _invalid() -> JSONResponse:
    return _failure("VALIDATION_ERROR", "请求参数不符合接口要求。", 400)


def _no_query(request: Request) -> None:
    if request.query_params:
        raise ValueError("此接口不接受查询参数。")


def _owner_key(request: Request, *, required: bool = True) -> str | None:
    values = request.headers.getlist("x-agent-owner-key")
    if not values and not required:
        return None
    if len(values) != 1 or _OWNER.fullmatch(values[0]) is None:
        raise ValueError("X-Agent-Owner-Key 必须是可信后端提供的稳定归属键。")
    return values[0]


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    items = list(request.query_params.multi_items())
    if len(items) != len({key for key, _ in items}) or any(key not in allowed or not value for key, value in items):
        raise ValueError("查询参数无效或重复。")
    return dict(items)


def _limit(value: str | None, default: int) -> int:
    if value is None:
        return default
    if not re.fullmatch(r"[1-9][0-9]{0,2}", value) or int(value) > 100:
        raise ValueError("分页条数必须是 1 到 100 的整数。")
    return int(value)


def _conversation_include(request: Request) -> tuple[str, ...]:
    _query(request, {"include", "run_id", "history_before", "history_limit"})
    values = request.query_params.getlist("include")
    if not values:
        return _CONVERSATION_INCLUDES
    if len(values) != 1:
        raise ValueError("include 只能指定一次。")
    if values[0] == "none":
        return ()
    selected = values[0].split(",")
    if len(selected) != len(set(selected)) or any(item not in _CONVERSATION_INCLUDES for item in selected):
        raise ValueError("include 必须是 none 或不重复的 history、report、artifacts 组合。")
    return tuple(item for item in _CONVERSATION_INCLUDES if item in selected)


def _event_cursor(request: Request) -> int:
    _no_query(request)
    headers = request.headers.getlist("last-event-id")
    if not headers:
        return 0
    if len(headers) != 1 or _CURSOR.fullmatch(headers[0]) is None:
        raise ValueError("Last-Event-ID 必须是非负十进制事件序号。")
    cursor = int(headers[0])
    if cursor > 9_223_372_036_854_775_807:
        raise ValueError("Last-Event-ID 超出支持范围。")
    return cursor


def _checked_batch(raw: Any, conversation_id: str, cursor: int) -> AgentEventBatch:
    batch = AgentEventBatch.model_validate(model_json(raw))
    for event in batch.events:
        if event.conversation_id != conversation_id or event.sequence != cursor + 1:
            raise RuntimeError("Agent event stream identity or sequence is invalid")
        if len(canonical_json_bytes(event.model_dump(mode="json"))) > _MAX_EVENT_BYTES:
            raise RuntimeError("Agent event exceeds the public stream frame limit")
        cursor = event.sequence
    return batch


async def _events(
    request: Request, service: Any, conversation_id: str, cursor: int,
    first: AgentEventBatch, owner_key: str | None = None,
) -> AsyncIterator[bytes]:
    """Read one bounded durable batch at a time; disconnect never cancels a Job."""
    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + _HEARTBEAT_SECONDS
    batch = first
    try:
        # Flush an initial comment even before any business event exists.
        # This lets fetch/EventSource establish the stream before a message POST.
        yield b": connected\n\n"
        while True:
            for event in batch.events:
                # The canonical serializer includes a file terminator. Remove
                # only that LF so every SSE business frame has exactly one
                # data line and one empty delimiter line. User newlines stay
                # JSON-escaped; sequence/type remain in the unchanged payload.
                payload = canonical_json_bytes(event.model_dump(mode="json")).removesuffix(b"\n")
                yield b"data: " + payload + b"\n\n"
                cursor = event.sequence
                heartbeat_at = loop.time() + _HEARTBEAT_SECONDS
            if batch.stream_closed and len(batch.events) < _EVENT_BATCH_SIZE:
                return
            if await request.is_disconnected():
                return
            if len(batch.events) < _EVENT_BATCH_SIZE:
                await asyncio.sleep(_EVENT_POLL_SECONDS)
            if loop.time() >= heartbeat_at:
                yield b": heartbeat\n\n"
                heartbeat_at = loop.time() + _HEARTBEAT_SECONDS
            raw = await asyncio.to_thread(
                service.list_events, conversation_id, after_sequence=cursor,
                limit=_EVENT_BATCH_SIZE,
                owner_key=owner_key,
            )
            batch = _checked_batch(raw, conversation_id, cursor)
    except Exception:
        # Headers have already been sent. Close without inventing a durable
        # event or revealing internal error text; clients reconnect with cursor.
        _LOGGER.exception("Agent SSE stream failed after response start")


def register_agent_routes(app: FastAPI, service: Any | None, public_base_url: str, query_port: Any | None = None) -> None:
    """Register all Agent routes even when its service is not configured."""
    # HTTP helpers are imported at registration time to avoid the composition
    # module's import cycle. Upload cancellation follows the established port.
    from .http_app import _port_call, parse_upload_headers

    errors = {
        status: {"model": AgentErrorEnvelope}
        for status in (400, 404, 409, 413, 422, 500, 503)
    }

    async def call(function: str, **kwargs: Any) -> Any:
        if service is None:
            raise AgentStoreError("AGENT_UNAVAILABLE", "Agent 服务尚未配置。", 503)
        return await _port_call(lambda: getattr(service, function)(**kwargs))

    async def respond(function: str, request: Request, **kwargs: Any) -> JSONResponse:
        try:
            kwargs["owner_key"] = _owner_key(request)
            if function == "get_conversation":
                kwargs["include"] = _conversation_include(request)
                query = request.query_params
                run_id = query.get("run_id")
                if run_id is not None and _UUID.fullmatch(run_id) is None:
                    raise ValueError("轮次标识无效。")
                kwargs.update(run_id=run_id, history_before=query.get("history_before"),
                              history_limit=_limit(query.get("history_limit"), 50))
            elif function == "list_conversations":
                query = _query(request, {"cursor", "limit"})
                kwargs.update(cursor=query.get("cursor"), limit=_limit(query.get("limit"), 20))
            else:
                _no_query(request)
        except ValueError:
            return _invalid()
        try:
            result = await call(function, **kwargs)
            result_model = {
                "create_conversation": ConversationReceipt,
                "send_message": MessageReceipt,
                "get_conversation": ConversationDetail,
                "prepare_attachment": AgentAttachment,
                "list_conversations": ConversationList,
                "rename_conversation": ConversationSummary,
                "stop_conversation": StopReceipt,
                "delete_conversation": DeleteReceipt,
            }[function]
            if function == "get_conversation":
                # The shared service already validated nested report content.
                # Dictionary adapters still pass through the complete contract.
                if not isinstance(result, result_model):
                    result = result_model.model_validate_json(canonical_json_bytes(model_json(result)))
                if result.conversation_id != kwargs["conversation_id"] or tuple(result.included) != kwargs["include"]:
                    raise RuntimeError("Conversation response identity or projection does not match the request")
                artifacts = result.artifacts
                if artifacts:
                    if result.case_id is None:
                        raise RuntimeError("Downloadable artifacts require an owning Case")
                    artifacts = [ConversationDownloadArtifact(
                        **artifact.model_dump(exclude={"download_url"}),
                        download_url=append_public_path(
                            public_base_url, f"{_PREFIX}/conversations/{result.conversation_id}/files/{artifact.artifact_id}/content",
                        ) + f"?run_id={result.selected_run_id}",
                    ) for artifact in artifacts]
                # All source fields and the newly built descriptors are typed;
                # preserve the already-validated report model by reference.
                values = {name: getattr(result, name) for name in ConversationDetail.model_fields}
                result = ConversationDetailResponse.model_construct(**{**values, "artifacts": artifacts})
            else:
                result = result_model.model_validate(model_json(result))
            return JSONResponse(success_envelope(result))
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent HTTP operation failed")
            return _failure("STATE_WRITE_FAILED", "Agent 服务暂时无法完成请求，请稍后重试。", 500, retryable=True)

    @app.post(
        f"{_PREFIX}/conversations", tags=["Agent"],
        response_model=SuccessEnvelope[ConversationReceipt], responses=errors,
        summary="创建 Agent 会话",
        description="建立一次定位会话；相同 request_id 幂等重试，不调用模型。",
        operation_id="create_agent_conversation",
    )
    async def create_conversation(body: CreateConversationBody, request: Request) -> JSONResponse:
        return await respond("create_conversation", request, request_id=body.request_id)

    @app.get(f"{_PREFIX}/conversations", tags=["Agent"],
             response_model=SuccessEnvelope[ConversationList], responses=errors,
             summary="读取当前用户的会话目录", operation_id="list_agent_conversations",
             description="按归属键分页读取会话摘要；默认 20 条、最多 100 条，不加载历史或报告。",
             openapi_extra={"parameters": [
                 {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}},
                 {"name": "cursor", "in": "query", "schema": {"type": "string"}, "description": "原样传回 next_cursor。"},
             ]})
    async def list_conversations(request: Request) -> JSONResponse:
        return await respond("list_conversations", request)

    @app.patch(f"{_PREFIX}/conversations/{{conversation_id}}", tags=["Agent"],
               response_model=SuccessEnvelope[ConversationSummary], responses=errors,
               summary="修改会话标题", operation_id="rename_agent_conversation",
               description="持久保存标题并返回会话摘要；同一标题赋值可重复。")
    async def rename_conversation(conversation_id: Annotated[OpaqueId, Path()], body: RenameConversationBody, request: Request):
        return await respond("rename_conversation", request, conversation_id=conversation_id, title=body.title)

    @app.post(f"{_PREFIX}/conversations/{{conversation_id}}/stop", tags=["Agent"],
              response_model=SuccessEnvelope[StopReceipt], responses=errors,
              summary="停止指定诊断轮次", operation_id="stop_agent_conversation",
              description="request_id 与 run_id 共同冻结停止目标；CANCELLING 表示等待后台安全退出。")
    async def stop_conversation(conversation_id: Annotated[OpaqueId, Path()], body: StopConversationBody, request: Request):
        return await respond("stop_conversation", request, conversation_id=conversation_id, **body.model_dump())

    @app.delete(f"{_PREFIX}/conversations/{{conversation_id}}", tags=["Agent"],
                response_model=SuccessEnvelope[DeleteReceipt], responses=errors,
                summary="删除会话及其诊断数据", operation_id="delete_agent_conversation",
                description="按会话 ID 幂等删除；先对新请求隐藏，等待活跃资源使用结束后清理文件。")
    async def delete_conversation(conversation_id: Annotated[OpaqueId, Path()], request: Request):
        return await respond("delete_conversation", request, conversation_id=conversation_id)

    @app.post(
        f"{_PREFIX}/conversations/{{conversation_id}}/messages", tags=["Agent"],
        response_model=SuccessEnvelope[MessageReceipt], responses=errors,
        summary="发送用户消息或日志附件",
        operation_id="send_agent_message",
        description="持久接收后立即返回；整理问题和定位任务在后台继续执行。",
    )
    async def send_message(
        conversation_id: Annotated[OpaqueId, Path()], body: SendMessageBody, request: Request,
    ) -> JSONResponse:
        return await respond(
            "send_message", request, conversation_id=conversation_id,
            request_id=body.request_id, text=body.text or "", attachment_ids=body.attachment_ids,
        )

    @app.get(
        f"{_PREFIX}/conversations/{{conversation_id}}", tags=["Agent"],
        response_model=SuccessEnvelope[ConversationDetailResponse], responses=errors,
        summary="读取会话、报告和下载入口",
        description=("默认返回消息和附件历史、正式报告及产物下载入口；include=none 只读轻量状态。"
                     "可按需选择 history、report、artifacts，未加载的字段为 null。"
                     "读取不运行模型，报告就绪后即可展示，无需等待归档。"),
        operation_id="get_agent_conversation",
        openapi_extra={"parameters": [{
            "name": "include", "in": "query", "required": False,
            "schema": {"type": "string", "default": "history,report,artifacts"},
            "description": "none 或以逗号分隔、不重复的 history、report、artifacts；不得重复指定参数。",
        }, *[{"name": name, "in": "query", "required": False, "schema": schema}
             for name, schema in (("run_id", {"type": "string", "format": "uuid"}),
                                  ("history_before", {"type": "string"}),
                                  ("history_limit", {"type": "integer", "default": 50, "minimum": 1, "maximum": 100}))]]},
    )
    async def get_conversation(
        conversation_id: Annotated[OpaqueId, Path()], request: Request,
    ) -> JSONResponse:
        return await respond("get_conversation", request, conversation_id=conversation_id)

    @app.get(
        f"{_PREFIX}/conversations/{{conversation_id}}/events", tags=["Agent"],
        response_class=_SseResponse, response_model=None,
        operation_id="subscribe_agent_events",
        description="回放 Last-Event-ID 之后的事件并持续推送；网站按 sequence 去重，断线不取消诊断。",
        responses={**{
            status: {"description": "订阅尚未建立时返回结构化 JSON 错误。", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/AgentErrorEnvelope"}},
            }} for status in errors
        }, 200: {
            "model": AgentEvent,
            "content": {"text/event-stream": {"schema": {"type": "object"}}},
            "description": (
                "每条业务消息仅发送一行 data: AgentEvent JSON，并以空行结束；"
                "不发送 event、id 或 retry 字段，前端使用 onmessage，按 JSON type 分派。"
                "sequence 保留在 JSON 中；精确续传需显式传 Last-Event-ID，"
                "原生 EventSource 自动重连会回放历史，前端须去重。"
                "首次发送 connected 注释，每 15 秒发送注释心跳。"
                "会话终态且归档已收口后关闭；断线不停止后台任务。"
            ),
        }},
        openapi_extra={"parameters": [{
            "name": "Last-Event-ID", "in": "header", "required": False,
            "schema": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"},
            "description": "最后一个已处理事件的 sequence；只返回其后的事件。",
        }]},
        summary="订阅实时事件并回放历史",
    )
    async def events(
        conversation_id: Annotated[OpaqueId, Path()], request: Request,
    ) -> JSONResponse | StreamingResponse:
        try:
            cursor = _event_cursor(request)
            owner_key = _owner_key(request)
        except ValueError:
            return _invalid()
        try:
            first = _checked_batch(await call(
                "list_events", conversation_id=conversation_id,
                after_sequence=cursor, limit=_EVENT_BATCH_SIZE,
                owner_key=owner_key,
            ), conversation_id, cursor)
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent SSE initial read failed")
            return _failure("STATE_CORRUPT", "暂时无法读取会话事件，请稍后重试。", 503)
        return _SseResponse(
            _events(request, service, conversation_id, cursor, first, owner_key),
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post(
        f"{_PREFIX}/attachments", tags=["Agent"],
        response_model=SuccessEnvelope[PreparedAgentAttachment], responses=errors,
        summary="预约会话日志附件上传",
        description="在关联 Case 创建前后均可预约日志，收到 READY 后再在消息中引用附件 ID。",
        operation_id="prepare_agent_attachment",
    )
    async def prepare_attachment(
        body: PrepareAgentAttachmentBody, request: Request,
    ) -> JSONResponse:
        result = await respond(
            "prepare_attachment", request, **body.model_dump(),
        )
        if result.status_code != 200:
            return result
        import json
        attachment = AgentAttachment.model_validate(json.loads(result.body)["data"])
        upload = WebUploadDescriptor(
            attachment_id=attachment.attachment_id, method="PUT",
            url=append_public_path(
                public_base_url, f"{_PREFIX}/attachments/{attachment.attachment_id}/content",
            ),
            required_headers=WebUploadRequiredHeaders(
                idempotency_key=attachment.attachment_id, content_type=attachment.content_type,
                content_sha256=attachment.sha256,
            ),
            expected_content_length=attachment.size, max_bytes=MAX_ATTACHMENT_BYTES,
            expires_at=None,
        )
        return JSONResponse(success_envelope(PreparedAgentAttachment(attachment=attachment, upload=upload)))

    @app.put(
        f"{_PREFIX}/attachments/{{attachment_id}}/content", tags=["Agent"],
        response_model=SuccessEnvelope[AgentAttachment], responses=errors,
        summary="上传会话日志文件原始字节",
        description="上传预约声明的原始归档；完整校验长度和 SHA-256 后才公开 READY，失败可重试同一附件。",
        operation_id="upload_agent_attachment",
        openapi_extra={
            "requestBody": {"required": True, "content": {
                media: {"schema": {"type": "string", "format": "binary"}}
                for media in ("application/zip", "application/gzip", "application/x-tar")
            }},
            "parameters": [
                {"name": name, "in": "header", "required": True, "schema": schema}
                for name, schema in (
                    ("Idempotency-Key", {"type": "string", "format": "uuid"}),
                    ("Content-Type", {"type": "string", "enum": ["application/zip", "application/gzip", "application/x-tar"]}),
                    ("Content-Length", {"type": "integer", "minimum": 1, "maximum": MAX_ATTACHMENT_BYTES}),
                    ("X-Content-SHA256", {"type": "string", "pattern": "^[0-9a-f]{64}$"}),
                )
            ],
        },
    )
    async def upload_attachment(
        attachment_id: Annotated[OpaqueId, Path()], request: Request,
    ) -> JSONResponse:
        try:
            _no_query(request)
            owner_key = _owner_key(request)
            headers = parse_upload_headers(request, attachment_id)
            if headers.content_length < 1:
                raise ValueError("附件不能为空。")
        except (ValueError, ValidationError, TypeError):
            return _invalid()
        if service is None:
            return _failure("AGENT_UNAVAILABLE", "Agent 服务尚未配置。", 503)
        stream = AsyncRequestBinaryStream(request.stream(), loop=asyncio.get_running_loop())
        try:
            result = await _port_call(lambda: service.upload_attachment(
                attachment_id=attachment_id, request_id=headers.idempotency_key,
                content_type=headers.content_type, content_length=headers.content_length,
                content_sha256=headers.content_sha256, content=stream,
                owner_key=owner_key,
            ), on_cancel=stream.abort)
            result = AgentAttachment.model_validate(model_json(result))
            return JSONResponse(success_envelope(result))
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent attachment upload failed")
            return _failure("UPLOAD_INCOMPLETE", "上传未完成，请使用相同附件标识重试。", 409, retryable=True)
        finally:
            await stream.aclose()

    @app.get(f"{_PREFIX}/conversations/{{conversation_id}}/files/{{artifact_id}}/content", tags=["Agent"],
             response_class=StreamingResponse, response_model=None, responses=errors,
             operation_id="download_agent_file", summary="下载指定会话轮次的文件",
             description="校验归属及轮次中的产物身份后传输不可变字节；资源使用租约保持到传输结束。",
             openapi_extra={"parameters": [{"name": "run_id", "in": "query", "required": False,
                                            "schema": {"type": "string", "format": "uuid"}}]})
    async def download_file(conversation_id: Annotated[OpaqueId, Path()], artifact_id: Annotated[OpaqueId, Path()],
                            request: Request):
        from .http_app import _ClosingStreamingResponse
        from .http_streaming import iterate_binary_stream

        try:
            owner_key = _owner_key(request)
            query = _query(request, {"run_id"})
            run_id = query.get("run_id")
            if run_id is not None and _UUID.fullmatch(run_id) is None:
                raise ValueError("轮次标识无效。")
        except ValueError:
            return _invalid()
        if service is None or query_port is None:
            return _failure("AGENT_UNAVAILABLE", "Agent 服务尚未配置。", 503)

        def open_owned():
            lease = service.operation_lease(conversation_id, owner_key=owner_key)
            lease.__enter__()
            try:
                detail = service.get_conversation(conversation_id, include=("artifacts",),
                                                  run_id=run_id, owner_key=owner_key)
                artifact = next((item for item in detail.artifacts or [] if item.artifact_id == artifact_id), None)
                if artifact is None or detail.case_id is None:
                    raise AgentStoreError("ARTIFACT_NOT_FOUND", "产物不存在。", 404)
                opened = query_port.open_artifact(detail.case_id, artifact_id)
            except BaseException:
                lease.__exit__(None, None, None)
                raise

            class LeasedStream:
                closed = False

                def read(self, size=-1):
                    return opened.stream.read(size)

                def close(self):
                    if not self.closed:
                        self.closed = True
                        try:
                            opened.stream.close()
                        finally:
                            lease.__exit__(None, None, None)

            return opened.artifact, LeasedStream()

        try:
            artifact, stream = await _port_call(open_owned, dispose_cancelled_result=lambda item: item[1].close())
            return _ClosingStreamingResponse(stream, iterate_binary_stream(stream), media_type=None, headers={
                "Content-Type": artifact.content_type, "Content-Length": str(artifact.size),
                "X-Content-SHA256": artifact.sha256, "Cache-Control": "no-store",
            })
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent file download failed")
            return _failure("STATE_CORRUPT", "暂时无法读取诊断文件。", 503)

    # Keep the trust boundary visible in generated OpenAPI on every Agent route.
    for route in app.routes:
        if getattr(route, "path", "").startswith(_PREFIX + "/"):
            extra = dict(getattr(route, "openapi_extra", None) or {})
            extra["parameters"] = [*(extra.get("parameters") or []), _OWNER_PARAMETER]
            route.openapi_extra = extra


__all__ = ["register_agent_routes"]
