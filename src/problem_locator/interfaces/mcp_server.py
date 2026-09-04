"""Official MCP SDK adapter exposing the seven frozen S06 tools."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from mcp import types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from problem_locator import __version__
from problem_locator.contracts.commands import (
    CancelCase,
    CreateCase,
    PrepareAttachment,
    ResumeCase,
    SubmitSupplement,
)
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import MAX_INITIAL_USER_FACTS
from problem_locator.contracts.models import (
    ContractName,
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
from problem_locator.contracts.serialization import canonical_json_bytes
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
    raw_problem_text: NonEmptyText = Field(
        description=(
            "Original user problem text passed verbatim to the generic locator "
            "when no specialized Skill matches."
        )
    )
    statement: NonEmptyText = Field(
        description="Concise statement of the problem to diagnose."
    )
    expected_behavior: NonEmptyText = Field(
        description="Expected behavior when the system is healthy."
    )
    actual_behavior: NonEmptyText = Field(
        description="Observed behavior that differs from the expectation."
    )
    scope: NonEmptyText = Field(
        description="System, component, request, or incident scope."
    )
    goals: list[NonEmptyText] = Field(
        min_length=1,
        description="Non-empty array of diagnosis goals.",
    )
    non_goals: list[NonEmptyText] = Field(
        description="Array of work explicitly outside the diagnosis scope."
    )
    constraints: list[NonEmptyText] = Field(
        description="Array of constraints that the diagnosis must respect."
    )
    completion_criteria: list[NonEmptyText] = Field(
        min_length=1,
        description=(
            "Non-empty array of observable criteria that define completion."
        )
    )
    initial_user_fact_names: list[ContractName] = Field(
        default_factory=list,
        max_length=MAX_INITIAL_USER_FACTS,
        json_schema_extra={"uniqueItems": True},
        description=(
            "Optional fact names. Pair by index with initial_user_fact_values; "
            "names must be unique."
        ),
    )
    initial_user_fact_values: list[NonEmptyText] = Field(
        default_factory=list,
        max_length=MAX_INITIAL_USER_FACTS,
        description=(
            "Optional fact values. Pair by index with initial_user_fact_names."
        ),
    )
    wait_seconds: WaitSeconds = 0

    @model_validator(mode="after")
    def validate_initial_user_fact_pairs(self) -> CreateCaseRequest:
        if len(self.initial_user_fact_names) != len(self.initial_user_fact_values):
            raise ValueError(
                "initial_user_fact_names and initial_user_fact_values must have "
                "equal lengths"
            )
        if len(set(self.initial_user_fact_names)) != len(
            self.initial_user_fact_names
        ):
            raise ValueError("initial user fact names must be unique")
        return self


class PrepareAttachmentRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    name: NonEmptyText = Field(
        description=(
            "Attachment filename. The JSON member is named `name`; "
            "do not send `attachment_name`."
        )
    )
    content_type: ContentType
    declared_size: NonNegativeInt | None = Field(
        default=None,
        description=(
            "Optional byte count. The JSON member is named `declared_size`; "
            "do not send `declared_byte_count`."
        ),
    )
    declared_sha256: Sha256 | None = None


class SubmitSupplementRequest(_RequestModel):
    request_id: NonEmptyText
    case_id: OpaqueId
    expected_case_revision: PositiveInt
    input_names: list[ContractName] = Field(
        json_schema_extra={"uniqueItems": True},
        description=(
            "Requirement names. Pair by index with input_values; names must be "
            "unique."
        )
    )
    input_values: list[NonEmptyText] = Field(
        description="Requirement values paired by index with input_names."
    )
    attachment_ids: list[OpaqueId] = Field(
        description="JSON array of READY attachment IDs."
    )
    wait_seconds: WaitSeconds = 0

    @model_validator(mode="after")
    def validate_input_pairs(self) -> SubmitSupplementRequest:
        if len(self.input_names) != len(self.input_values):
            raise ValueError("input_names and input_values must have equal lengths")
        if len(set(self.input_names)) != len(self.input_names):
            raise ValueError("input names must be unique")
        return self


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
    "problem_locator_create_case": (
        "Create a new diagnosis case. Supply raw_problem_text and the eight problem specification "
        "members as flat root fields. Pair optional initial fact names and values "
        "by array index."
    ),
    "problem_locator_prepare_attachment": (
        "Prepare an immutable attachment upload. Use the exact input members "
        "`name` and `declared_size`; `attachment_name` and "
        "`declared_byte_count` are not aliases."
    ),
    "problem_locator_submit_supplement": (
        "Submit facts and READY attachments to a waiting case. Pair input_names "
        "and input_values by array index."
    ),
    "problem_locator_get_case": (
        "Read the current public case view and its downloadable Artifact transfer "
        "descriptors."
    ),
    "problem_locator_resume_case": "Resume a persisted pending or interrupted case.",
    "problem_locator_cancel_case": "Cancel a case using its current revision.",
    "problem_locator_list_artifacts": "List user-downloadable case artifacts.",
}


_OUTPUT_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {"type": ["object", "null"]},
        "error": {"type": ["object", "null"]},
    },
    "required": ["ok", "data", "error"],
    "additionalProperties": False,
    "oneOf": [
        {
            "properties": {
                "ok": {"const": True},
                "data": {"type": "object"},
                "error": {"type": "null"},
            }
        },
        {
            "properties": {
                "ok": {"const": False},
                "data": {"type": "null"},
                "error": {"type": "object"},
            }
        },
    ],
}

_OUTPUT_SCHEMAS = {
    name: copy.deepcopy(_OUTPUT_ENVELOPE_SCHEMA)
    for name in _REQUESTS
}


def _schema_sha256(schema: dict[str, Any]) -> str:
    canonical = json.dumps(
        schema,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
                problem_spec = ProblemSpecInput(
                    statement=request.statement,
                    expected_behavior=request.expected_behavior,
                    actual_behavior=request.actual_behavior,
                    scope=request.scope,
                    goals=request.goals,
                    non_goals=request.non_goals,
                    constraints=request.constraints,
                    completion_criteria=request.completion_criteria,
                )
                initial_user_facts = [
                    UserFactInput(name=name, value=value)
                    for name, value in zip(
                        request.initial_user_fact_names,
                        request.initial_user_fact_values,
                        strict=True,
                    )
                ]
                command = CreateCase(
                    idempotency_key=request.request_id,
                    raw_problem_text=request.raw_problem_text,
                    problem_spec=problem_spec,
                    initial_user_facts=initial_user_facts,
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
                    inputs=dict(
                        zip(request.input_names, request.input_values, strict=True)
                    ),
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
            # A terminal Case already exposes the authoritative downloadable
            # Artifact summaries.  Add their public transfer descriptors to
            # the same read so a Remote MCP client does not need a second
            # list_artifacts round trip merely to discover download URLs.
            # Keep the original CaseQueryResponse members unchanged for
            # compatibility with existing clients.
            views = [
                artifact_view(
                    summary,
                    case_id=request.case_id,
                    public_base_url=self._public_base_url,
                )
                for summary in response.case_view.artifacts
            ]
            return success_envelope(
                {
                    "case_view": response.case_view,
                    "wait_timed_out": response.wait_timed_out,
                    "artifact_views": views,
                }
            )

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
    server = Server("problem-locator", version=__version__)

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        tools = [
            mcp_types.Tool(
                name=name,
                description=_DESCRIPTIONS[name],
                inputSchema=request_type.model_json_schema(mode="validation"),
                outputSchema=_OUTPUT_SCHEMAS[name],
            )
            for name, request_type in _REQUESTS.items()
        ]
        log_event(
            "mcp.tools.listed",
            server_version=__version__,
            tools=[
                {
                    "name": tool.name,
                    "input_schema": tool.inputSchema,
                    "input_schema_sha256": _schema_sha256(tool.inputSchema),
                }
                for tool in tools
            ],
        )
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[list[mcp_types.TextContent], dict[str, Any]]:
        structured = await adapter.call(name, arguments)
        content = mcp_types.TextContent(
            type="text",
            text=canonical_json_bytes(structured).decode("utf-8"),
        )
        return [content], structured

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
