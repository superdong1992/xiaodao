"""Official MCP SDK adapter exposing the seven frozen S06 tools."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from mcp import types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from problem_locator.contracts.commands import (
    ApplicationResponse,
    ArtifactView,
    CancelCase,
    CaseQueryResponse,
    CreateCase,
    PrepareAttachment,
    ResumeCase,
    SubmitSupplement,
    UploadDescriptor,
)
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.models import (
    ApplicationError,
    ContentType,
    NonEmptyText,
    NonNegativeInt,
    OpaqueId,
    PositiveInt,
    ProblemSpecInput,
    Sha256,
    UserFactInput,
    WaitSeconds,
)
from problem_locator.contracts.ports import ApplicationCommandPort, ApplicationQueryPort
from problem_locator.diagnostics import bind_diagnostics, log_event

from .error_mapping import (
    error_envelope,
    success_envelope,
    validation_diagnostics,
    validation_error_from,
)
from .projections import artifact_view, upload_descriptor


def _log_validation_failure(
    name: str,
    arguments: dict[str, Any],
    error: ValidationError | ValueError | TypeError,
) -> None:
    log_event(
        "mcp.tool.validation_failed",
        level=logging.WARNING,
        tool=name,
        arguments=arguments,
        validation_errors=validation_diagnostics(error),
        error=error,
    )


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateCaseRequest(_RequestModel):
    request_id: NonEmptyText
    problem_spec: ProblemSpecInput
    initial_user_facts: list[UserFactInput] = Field(default_factory=list)
    wait_seconds: WaitSeconds = 0


class PrepareAttachmentRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    name: NonEmptyText
    content_type: ContentType
    declared_size: NonNegativeInt | None = None
    declared_sha256: Sha256 | None = None


class SubmitSupplementRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    inputs: dict[str, str]
    attachment_ids: list[OpaqueId]
    wait_seconds: WaitSeconds = 0


class GetCaseRequest(_RequestModel):
    case_id: OpaqueId
    wait_for_job_id: OpaqueId | None = None
    wait_seconds: WaitSeconds = 0


class ResumeCaseRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    wait_seconds: WaitSeconds = 0


class CancelCaseRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt


class ListArtifactsRequest(_RequestModel):
    case_id: OpaqueId


class _PrepareData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_response: ApplicationResponse
    upload: UploadDescriptor


class _ArtifactData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[ArtifactView]


class _SuccessEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True]
    data: Any
    error: None


class _FailureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[False]
    data: None
    error: ApplicationError


_REQUESTS: dict[str, type[_RequestModel]] = {
    "problem_locator_create_case": CreateCaseRequest,
    "problem_locator_prepare_attachment": PrepareAttachmentRequest,
    "problem_locator_submit_supplement": SubmitSupplementRequest,
    "problem_locator_get_case": GetCaseRequest,
    "problem_locator_resume_case": ResumeCaseRequest,
    "problem_locator_cancel_case": CancelCaseRequest,
    "problem_locator_list_artifacts": ListArtifactsRequest,
}

_DESCRIPTIONS = {
    "problem_locator_create_case": "Create a new diagnosis case.",
    "problem_locator_prepare_attachment": "Prepare an immutable attachment upload.",
    "problem_locator_submit_supplement": "Submit facts and READY attachments to a waiting case.",
    "problem_locator_get_case": "Read the current public case view.",
    "problem_locator_resume_case": "Resume a persisted pending or interrupted case.",
    "problem_locator_cancel_case": "Cancel a case using its current revision.",
    "problem_locator_list_artifacts": "List user-downloadable case artifacts.",
}


def _output_schema(data_type: Any) -> dict[str, Any]:
    success = type(
        f"Success_{getattr(data_type, '__name__', 'Data')}",
        (_SuccessEnvelope,),
        {"__annotations__": {"data": data_type}},
    )
    schema = TypeAdapter(success | _FailureEnvelope).json_schema(mode="serialization")
    root_type = schema.get("type")
    if root_type not in (None, "object"):
        raise RuntimeError("MCP output envelope schema must have an object root.")
    schema["type"] = "object"
    return schema


_OUTPUT_SCHEMAS = {
    "problem_locator_create_case": _output_schema(ApplicationResponse),
    "problem_locator_prepare_attachment": _output_schema(_PrepareData),
    "problem_locator_submit_supplement": _output_schema(ApplicationResponse),
    "problem_locator_get_case": _output_schema(CaseQueryResponse),
    "problem_locator_resume_case": _output_schema(ApplicationResponse),
    "problem_locator_cancel_case": _output_schema(ApplicationResponse),
    "problem_locator_list_artifacts": _output_schema(_ArtifactData),
}


@dataclass(frozen=True, slots=True)
class McpTransport:
    server: Server
    session_manager: StreamableHTTPSessionManager
    asgi_application: "McpHttpApplication"


class McpHttpApplication:
    """Raw ASGI endpoint delegating to the official session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._manager.handle_request(scope, receive, send)


class McpAdapter:
    def __init__(
        self,
        command_port: ApplicationCommandPort,
        query_port: ApplicationQueryPort,
        *,
        public_base_url: str,
    ) -> None:
        self._command_port = command_port
        self._query_port = query_port
        self._public_base_url = public_base_url

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        request_id = arguments.get("request_id")
        with bind_diagnostics(
            transport="mcp",
            tool=name,
            request_id=request_id if isinstance(request_id, str) else None,
        ):
            log_event("mcp.tool.started", arguments=arguments)
            try:
                result = await self._call(name, arguments)
            except ApplicationPortError as exc:
                log_event(
                    "mcp.tool.application_error",
                    level=logging.WARNING,
                    arguments=arguments,
                    application_error=exc.error,
                    error_code=exc.error.code,
                    error=exc,
                )
                result = error_envelope(exc.error)
            except Exception as exc:
                log_event(
                    "mcp.tool.unhandled_error",
                    level=logging.ERROR,
                    arguments=arguments,
                    error=exc,
                )
                raise

            public_error = result.get("error") if result.get("ok") is False else None
            log_event(
                "mcp.tool.completed",
                level=logging.WARNING if public_error is not None else logging.INFO,
                ok=result.get("ok"),
                error_code=(
                    public_error.get("code")
                    if isinstance(public_error, dict)
                    else None
                ),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            return result

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_type = _REQUESTS.get(name)
        if request_type is None:
            raise ValueError("Unknown MCP tool.")
        try:
            request = request_type.model_validate(arguments)
        except ValidationError as exc:
            _log_validation_failure(name, arguments, exc)
            return error_envelope(validation_error_from(exc))

        if isinstance(request, CreateCaseRequest):
            try:
                command = CreateCase(
                    idempotency_key=request.request_id,
                    problem_spec=request.problem_spec,
                    initial_user_facts=request.initial_user_facts,
                    wait_seconds=request.wait_seconds,
                )
            except ValidationError as exc:
                _log_validation_failure(name, arguments, exc)
                return error_envelope(validation_error_from(exc))
            response = await asyncio.to_thread(self._command_port.execute, command)
            return success_envelope(response)

        if isinstance(request, PrepareAttachmentRequest):
            try:
                command = PrepareAttachment(
                    idempotency_key=request.request_id,
                    case_id=request.case_id,
                    expected_case_revision=request.expected_case_revision,
                    name=request.name,
                    content_type=request.content_type,
                    declared_size=request.declared_size,
                    declared_sha256=request.declared_sha256,
                )
            except ValidationError as exc:
                _log_validation_failure(name, arguments, exc)
                return error_envelope(validation_error_from(exc))
            response = await asyncio.to_thread(self._command_port.execute, command)
            descriptor = upload_descriptor(
                response,
                public_base_url=self._public_base_url,
                content_type=request.content_type,
                declared_size=request.declared_size,
                declared_sha256=request.declared_sha256,
            )
            return success_envelope(
                {"application_response": response, "upload": descriptor}
            )

        if isinstance(request, SubmitSupplementRequest):
            try:
                command = SubmitSupplement(
                    idempotency_key=request.request_id,
                    case_id=request.case_id,
                    expected_case_revision=request.expected_case_revision,
                    inputs=request.inputs,
                    attachment_ids=request.attachment_ids,
                    wait_seconds=request.wait_seconds,
                )
            except ValidationError as exc:
                _log_validation_failure(name, arguments, exc)
                return error_envelope(validation_error_from(exc))
            response = await asyncio.to_thread(self._command_port.execute, command)
            return success_envelope(response)

        if isinstance(request, GetCaseRequest):
            response = await asyncio.to_thread(
                self._query_port.get_case,
                request.case_id,
                request.wait_for_job_id,
                request.wait_seconds,
            )
            return success_envelope(response)

        if isinstance(request, ResumeCaseRequest):
            command = ResumeCase(
                idempotency_key=request.request_id,
                case_id=request.case_id,
                expected_case_revision=request.expected_case_revision,
                wait_seconds=request.wait_seconds,
            )
            response = await asyncio.to_thread(self._command_port.execute, command)
            return success_envelope(response)

        if isinstance(request, CancelCaseRequest):
            command = CancelCase(
                idempotency_key=request.request_id,
                case_id=request.case_id,
                expected_case_revision=request.expected_case_revision,
            )
            response = await asyncio.to_thread(self._command_port.execute, command)
            return success_envelope(response)

        assert isinstance(request, ListArtifactsRequest)
        response = await asyncio.to_thread(
            self._query_port.list_artifacts,
            request.case_id,
            False,
        )
        views = [
            artifact_view(
                summary,
                case_id=request.case_id,
                public_base_url=self._public_base_url,
            )
            for summary in response.artifacts
        ]
        return success_envelope({"artifacts": views})


def create_mcp_transport(
    command_port: ApplicationCommandPort,
    query_port: ApplicationQueryPort,
    *,
    public_base_url: str,
) -> McpTransport:
    """Create a stateless JSON-response Streamable HTTP MCP endpoint."""

    adapter = McpAdapter(
        command_port,
        query_port,
        public_base_url=public_base_url,
    )
    server = Server("problem-locator")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=name,
                description=_DESCRIPTIONS[name],
                inputSchema=request_type.model_json_schema(mode="validation"),
                outputSchema=_OUTPUT_SCHEMAS[name],
            )
            for name, request_type in _REQUESTS.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await adapter.call(name, arguments)

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
    )
    return McpTransport(
        server=server,
        session_manager=manager,
        asgi_application=McpHttpApplication(manager),
    )


__all__ = [
    "McpAdapter",
    "McpHttpApplication",
    "McpTransport",
    "create_mcp_transport",
]
