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
    ConversationReceipt,
    ConversationView,
    MessageReceipt,
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


class _AgentHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateConversationBody(_AgentHttpModel):
    request_id: NonEmptyText = Field(description="同一次创建重试时保持不变。")


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
    request_id: NonEmptyText
    name: NonEmptyText
    content_type: Literal["application/zip", "application/gzip", "application/x-tar"]
    declared_size: int = Field(ge=1, le=MAX_ATTACHMENT_BYTES)
    declared_sha256: Sha256


class PreparedAgentAttachment(_AgentHttpModel):
    attachment: AgentAttachment
    upload: WebUploadDescriptor


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


def _failure(code: str, message: str, status: int, *, retryable: bool = False) -> JSONResponse:
    value = AgentErrorEnvelope(error=AgentHttpError(
        code=code, message=message, retryable=retryable,
    ))
    return JSONResponse(value.model_dump(mode="json"), status_code=status)


def _invalid() -> JSONResponse:
    return _failure("VALIDATION_ERROR", "请求参数不符合接口要求。", 400)


def _no_query(request: Request) -> None:
    if request.query_params:
        raise ValueError("此接口不接受查询参数。")


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
    first: AgentEventBatch,
) -> AsyncIterator[bytes]:
    """Read one bounded durable batch at a time; disconnect never cancels a Job."""
    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + _HEARTBEAT_SECONDS
    batch = first
    try:
        yield b"retry: 2000\n\n"
        while True:
            for event in batch.events:
                payload = canonical_json_bytes(event.model_dump(mode="json"))
                yield (
                    f"id: {event.sequence}\nevent: {event.type}\ndata: ".encode("ascii")
                    + payload + b"\n\n"
                )
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
            )
            batch = _checked_batch(raw, conversation_id, cursor)
    except Exception:
        # Headers have already been sent. Close without inventing a durable
        # event or revealing internal error text; clients reconnect with cursor.
        _LOGGER.exception("Agent SSE stream failed after response start")


def register_agent_routes(app: FastAPI, service: Any | None, public_base_url: str) -> None:
    """Register all six routes even when Agent service is not configured."""
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
            _no_query(request)
        except ValueError:
            return _invalid()
        try:
            result = await call(function, **kwargs)
            result_model = {
                "create_conversation": ConversationReceipt,
                "send_message": MessageReceipt,
                "get_conversation": ConversationView,
                "prepare_attachment": AgentAttachment,
            }[function]
            result = result_model.model_validate(model_json(result))
            return JSONResponse(success_envelope(result))
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code)
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
        response_model=SuccessEnvelope[ConversationView], responses=errors,
        summary="读取会话、追问和附件状态",
        description="只读返回持久历史和最新游标，不启动或重新运行后台任务。",
        operation_id="get_agent_conversation",
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
                "SSE 帧的 data 是 AgentEvent JSON，id 等于 sequence；首次连接回放历史。"
                "每 15 秒发送注释心跳。会话终态且归档已收口后关闭；断线不停止后台任务。"
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
        except ValueError:
            return _invalid()
        try:
            first = _checked_batch(await call(
                "list_events", conversation_id=conversation_id,
                after_sequence=cursor, limit=_EVENT_BATCH_SIZE,
            ), conversation_id, cursor)
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent SSE initial read failed")
            return _failure("STATE_CORRUPT", "暂时无法读取会话事件，请稍后重试。", 503)
        return _SseResponse(
            _events(request, service, conversation_id, cursor, first),
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post(
        f"{_PREFIX}/conversations/{{conversation_id}}/attachments", tags=["Agent"],
        response_model=SuccessEnvelope[PreparedAgentAttachment], responses=errors,
        summary="预约会话日志附件上传",
        description="在关联 Case 创建前后均可预约日志，收到 READY 后再在消息中引用附件 ID。",
        operation_id="prepare_agent_attachment",
    )
    async def prepare_attachment(
        conversation_id: Annotated[OpaqueId, Path()], body: PrepareAgentAttachmentBody,
        request: Request,
    ) -> JSONResponse:
        result = await respond(
            "prepare_attachment", request, conversation_id=conversation_id,
            **body.model_dump(),
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
            ), on_cancel=stream.abort)
            result = AgentAttachment.model_validate(model_json(result))
            return JSONResponse(success_envelope(result))
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code)
        except ApplicationPortError as exc:
            return JSONResponse(error_envelope(exc.error), status_code=http_status_for(exc.error))
        except Exception:
            _LOGGER.exception("Agent attachment upload failed")
            return _failure("UPLOAD_INCOMPLETE", "上传未完成，请使用相同附件标识重试。", 409, retryable=True)
        finally:
            await stream.aclose()


__all__ = ["register_agent_routes"]
