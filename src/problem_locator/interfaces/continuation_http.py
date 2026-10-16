"""Owner-scoped confirmed continuation drafts; GET never invokes a model."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Path, Request
from fastapi.responses import JSONResponse

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts.models import OpaqueId
from problem_locator.continuation.models import (
    ConfirmDraftRequest, ContinuationDraft, ContinuationReceipt, CreateDraftRequest,
    ReportVersions, UpdateDraftRequest,
)
from .agent_http import _PREFIX, _failure, _invalid, _no_query, _owner_key, AgentErrorEnvelope
from .error_mapping import success_envelope
from .rest_models import SuccessEnvelope

_LOGGER = logging.getLogger(__name__)
_BASE = _PREFIX + "/conversations/{conversation_id}"


def register_continuation_routes(app, agent):
    from .http_app import _port_call

    errors = {code: {"model": AgentErrorEnvelope} for code in (400, 404, 409, 410, 413, 422, 500, 503)}

    async def respond(request, cid, operation, identifier, body=None):
        try:
            _no_query(request)
            owner = _owner_key(request)
            if body is None and await request.body():
                return _invalid()
        except ValueError:
            return _invalid()
        try:
            service = getattr(agent, "continuations", None)
            if service is None:
                raise AgentStoreError("AGENT_UNAVAILABLE", "继续诊断服务暂时不可用。", 503)
            def call():
                method = getattr(service, operation)
                return method(cid, identifier, owner_key=owner) if body is None else method(cid, identifier, body, owner_key=owner)
            result = await _port_call(call)
            if result.conversation_id != cid:
                raise ValueError("continuation response owner mismatch")
            return JSONResponse(success_envelope(result))
        except AgentStoreError as error:
            return _failure(error.code, error.message, error.status_code, details=error.details, retryable=error.retryable)
        except Exception:
            _LOGGER.exception("Continuation HTTP operation failed")
            return _failure("STATE_WRITE_FAILED", "暂时无法处理继续诊断，请稍后重试。", 500, retryable=True)

    @app.post(_BASE + "/runs/{run_id}/continuation-drafts", tags=["Agent"],
              response_model=SuccessEnvelope[ContinuationDraft], responses=errors,
              operation_id="create_agent_continuation_draft", summary="准备继续诊断草稿",
              description="从最新 Markdown 报告准备草稿；自然语言输入异步判断意图并整理，纯提问返回 FOLLOWUP，直接编辑不调用模型。尚未创建新轮次。")
    async def create(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()],
                     body: CreateDraftRequest, request: Request):
        return await respond(request, conversation_id, "create", run_id, body)

    @app.get(_BASE + "/continuation-drafts/{draft_id}", tags=["Agent"],
             response_model=SuccessEnvelope[ContinuationDraft], responses=errors,
             operation_id="get_agent_continuation_draft", summary="读取继续诊断草稿",
             description="读取当前用户的草稿和准备状态，不接受请求体或查询参数，不启动模型。")
    async def get(conversation_id: Annotated[OpaqueId, Path()], draft_id: Annotated[OpaqueId, Path()], request: Request):
        return await respond(request, conversation_id, "get", draft_id)

    @app.patch(_BASE + "/continuation-drafts/{draft_id}", tags=["Agent"],
               response_model=SuccessEnvelope[ContinuationDraft], responses=errors,
               operation_id="update_agent_continuation_draft", summary="修改本轮问题和最终日志清单",
               description="按草稿版本保存直接编辑的完整问题、目标和最终日志集合；不启动正式诊断。")
    async def update(conversation_id: Annotated[OpaqueId, Path()], draft_id: Annotated[OpaqueId, Path()],
                     body: UpdateDraftRequest, request: Request):
        return await respond(request, conversation_id, "update", draft_id, body)

    @app.post(_BASE + "/continuation-drafts/{draft_id}/confirm", tags=["Agent"],
              response_model=SuccessEnvelope[ContinuationReceipt], responses=errors,
              operation_id="confirm_agent_continuation_draft", summary="确认信息并开始新一轮诊断",
              description="确认最新 DRAFT 草稿，幂等创建新 run 和独立 Case，使用既有诊断流程。FOLLOWUP 只允许转入父报告追问，不能确认。旧报告继续保留。")
    async def confirm(conversation_id: Annotated[OpaqueId, Path()], draft_id: Annotated[OpaqueId, Path()],
                      body: ConfirmDraftRequest, request: Request):
        return await respond(request, conversation_id, "confirm", draft_id, body)

    @app.get(_BASE + "/runs/{run_id}/report-versions", tags=["Agent"],
             response_model=SuccessEnvelope[ReportVersions], responses=errors,
             operation_id="list_agent_report_versions", summary="读取同一问题的报告版本",
             description="读取已发布报告版本和输入变化；按指定 run_id 沿用报告读取、下载和追问接口。")
    async def versions(conversation_id: Annotated[OpaqueId, Path()], run_id: Annotated[OpaqueId, Path()], request: Request):
        return await respond(request, conversation_id, "versions", run_id)
