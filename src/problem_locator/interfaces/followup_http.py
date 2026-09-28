"""Independent report-followup API; readers never start model work."""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import Path, Request
from fastapi.responses import JSONResponse, StreamingResponse

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts.models import OpaqueId
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.followup.models import (
    FollowupEvent, FollowupRequest, FollowupStopRequest, FollowupView,
    FollowupReceipt, FollowupStopReceipt,
)
from .agent_http import (
    _PREFIX, _failure, _invalid, _owner_key, _no_query, _query, _limit,
    _event_cursor, _SseResponse, AgentErrorEnvelope,
)
from .error_mapping import model_json, success_envelope
from .rest_models import SuccessEnvelope

_LOGGER = logging.getLogger(__name__)
_ROOT = _PREFIX + "/conversations/{conversation_id}/runs/{run_id}/followups"
_BATCH = 20


def _checked_batch(raw, cid, rid, cursor):
    raw = model_json(raw)
    if not isinstance(raw, dict) or type(raw.get("stream_closed")) is not bool:
        raise ValueError("invalid followup event batch")
    events = raw.get("events")
    if not isinstance(events, list) or len(events) > _BATCH:
        raise ValueError("invalid followup event batch size")
    checked = []
    for item in events:
        event = FollowupEvent.model_validate(model_json(item))
        if (event.conversation_id != cid or event.run_id != rid or event.sequence != cursor + 1
                or event.data.followup_id != event.followup_id or event.data.run_id != rid):
            raise ValueError("invalid followup event identity or sequence")
        payload = canonical_json_bytes(event.model_dump(mode="json"))
        if len(payload) > 1024 * 1024:
            raise ValueError("followup event too large")
        checked.append((event.sequence, payload.removesuffix(b"\n")))
        cursor = event.sequence
    return checked, raw["stream_closed"]


async def _events(request, service, cid, rid, owner, cursor, first):
    from .http_app import _port_call
    batch = first
    last_heartbeat = asyncio.get_running_loop().time()
    try:
        yield b": connected\n\n"
        while True:
            events, closed = batch
            for sequence, payload in events:
                if await request.is_disconnected():
                    return
                yield b"data: " + payload + b"\n\n"
                cursor = sequence
            if closed and len(events) < _BATCH:
                return
            if await request.is_disconnected():
                return
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= 15:
                yield b": heartbeat\n\n"
                last_heartbeat = now
            if len(events) < _BATCH:
                await asyncio.sleep(0.5)
            raw = await _port_call(lambda: service.list_events(
                cid, rid, after_sequence=cursor, limit=_BATCH, owner_key=owner))
            batch = _checked_batch(raw, cid, rid, cursor)
    except Exception:
        # Headers are already sent: disconnect so the next request can replay.
        _LOGGER.exception("Report followup SSE failed after response start")


def register_followup_routes(app, agent):
    from .http_app import _port_call

    errors = {status: {"model": AgentErrorEnvelope}
              for status in (400, 404, 409, 413, 422, 429, 500, 503)}

    def service():
        result = getattr(agent, "followups", None)
        if result is None:
            raise AgentStoreError("AGENT_UNAVAILABLE", "追问服务尚未配置。", 503)
        return result

    async def respond(operation, model, request, cid, rid, **kwargs):
        try:
            owner = _owner_key(request)
            if operation == "get":
                if await request.body():
                    return _invalid()
                query = _query(request, {"cursor", "limit"})
                kwargs.update(cursor=query.get("cursor"), limit=_limit(query.get("limit"), 50))
            else:
                _no_query(request)
        except ValueError:
            return _invalid()
        try:
            target = service()
            raw = await _port_call(lambda: getattr(target, operation)(cid, rid, owner_key=owner, **kwargs))
            result = model.model_validate(model_json(raw))
            if result.conversation_id != cid or result.run_id != rid:
                raise ValueError("followup response identity mismatch")
            if operation == "get" and any(item.run_id != rid for item in result.items):
                raise ValueError("followup history belongs to another run")
            if "request_id" in kwargs and result.request_id != kwargs["request_id"]:
                raise ValueError("followup receipt request identity mismatch")
            if "followup_id" in kwargs and result.followup_id != kwargs["followup_id"]:
                raise ValueError("followup stop identity mismatch")
            return JSONResponse(success_envelope(result))
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except Exception:
            _LOGGER.exception("Report followup HTTP operation failed")
            return _failure("STATE_WRITE_FAILED", "暂时无法处理追问，请稍后重试。", 500, retryable=True)

    @app.post(_ROOT, tags=["Agent"], response_model=SuccessEnvelope[FollowupReceipt], responses=errors,
              operation_id="submit_agent_followup", summary="提交报告追问",
              description="只接受 request_id 和 text；后台独立回答，不修改原报告。重试必须保持 ID 和原文，不得回退到普通消息接口。")
    async def submit(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()],
                     body: FollowupRequest, request: Request):
        return await respond("submit", FollowupReceipt, request, conversation_id, run_id, **body.model_dump())

    @app.get(_ROOT, tags=["Agent"], response_model=SuccessEnvelope[FollowupView], responses=errors,
             operation_id="list_agent_followups", summary="读取报告追问记录",
             description="读取追问资格、日志快照状态、会话内活跃追问和分页问答；开关关闭后仍可查询，不启动模型。",
             openapi_extra={"parameters": [
                 {"name": "cursor", "in": "query", "schema": {"type": "string"}, "description": "原样传回 next_cursor，读取更早记录。"},
                 {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100}, "description": "每页记录数，默认 50，最多 100。"},
             ]})
    async def get(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()], request: Request):
        return await respond("get", FollowupView, request, conversation_id, run_id)

    @app.post(_ROOT + "/{followup_id}/stop", tags=["Agent"],
              response_model=SuccessEnvelope[FollowupStopReceipt], responses=errors,
              operation_id="stop_agent_followup", summary="停止指定报告追问",
              description="按指定追问停止；开关关闭后仍可操作。停止不改变原报告，迟到答案不会覆盖停止状态。")
    async def stop(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()],
                   followup_id: Annotated[OpaqueId, Path()], body: FollowupStopRequest, request: Request):
        return await respond("stop", FollowupStopReceipt, request, conversation_id, run_id,
                             followup_id=followup_id, **body.model_dump())

    @app.get(_ROOT + "/events", tags=["Agent"], response_class=_SseResponse, response_model=None,
             operation_id="subscribe_agent_followup_events", summary="订阅报告追问事件",
             description="使用独立 Last-Event-ID 游标回放；data 为完整状态和回答，按 sequence 去重。查询与断线重连不启动模型。",
             responses={**{status: {"description": "订阅建立前返回结构化错误。", "content": {
                 "application/json": {"schema": {"$ref": "#/components/schemas/AgentErrorEnvelope"}}}}
                 for status in errors}, 200: {"model": FollowupEvent,
                 "content": {"text/event-stream": {"schema": {"type": "object"}}},
                 "description": "仅发送 data JSON 和注释心跳；终态且回放完毕后关闭。"}},
             openapi_extra={"parameters": [{"name": "Last-Event-ID", "in": "header", "required": False,
                 "schema": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"},
                 "description": "此轮追问最后处理的 sequence，不能使用诊断事件游标。"}]})
    async def subscribe(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()], request: Request):
        try:
            owner = _owner_key(request)
            cursor = _event_cursor(request)
            if await request.body():
                return _invalid()
        except ValueError:
            return _invalid()
        try:
            target = service()
            raw = await _port_call(lambda: target.list_events(conversation_id, run_id,
                after_sequence=cursor, limit=_BATCH, owner_key=owner))
            batch = _checked_batch(raw, conversation_id, run_id, cursor)
            return StreamingResponse(_events(request, target, conversation_id, run_id, owner, cursor, batch),
                media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        except AgentStoreError as exc:
            return _failure(exc.code, exc.message, exc.status_code, details=exc.details, retryable=exc.retryable)
        except Exception:
            _LOGGER.exception("Report followup event replay failed")
            return _failure("STATE_WRITE_FAILED", "暂时无法读取追问进度。", 500, retryable=True)
