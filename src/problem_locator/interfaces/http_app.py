"""FastAPI control/file routes sharing the S06 MCP application."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError

from problem_locator import __version__
from problem_locator.contracts.commands import (
    ApplicationResponse,
    CreateCase,
    PrepareAttachment,
    SubmitSupplement,
    UploadAttachmentContent,
)
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import (
    ContentType,
    OpaqueId,
    ProblemSpecInput,
    Sha256,
    UserFactInput,
    WaitSeconds,
)
from problem_locator.contracts.ports import (
    ApplicationCommandPort,
    ApplicationQueryPort,
    StateAdminPort,
)
from problem_locator.diagnostics import HttpDiagnosticsMiddleware, log_event

from .error_mapping import (
    error_envelope,
    http_status_for,
    success_envelope,
    validation_diagnostics,
    validation_error_from,
)
from .http_streaming import AsyncRequestBinaryStream, iterate_binary_stream
from .mcp_server import create_mcp_transport
from .projections import artifact_view, web_upload_descriptor
from .rest_models import (
    ApplicationSuccessEnvelope,
    ArtifactListSuccessEnvelope,
    CaseQuerySuccessEnvelope,
    CreateCaseBody,
    ErrorEnvelope,
    PrepareAttachmentBody,
    PrepareAttachmentSuccessEnvelope,
    SubmitSupplementBody,
    UploadReadySuccessEnvelope,
)


_OPAQUE_ID = TypeAdapter(OpaqueId)
_CONTENT_TYPE = TypeAdapter(ContentType)
_SHA256 = TypeAdapter(Sha256)
_WAIT_SECONDS = TypeAdapter(WaitSeconds)
_DECIMAL_BYTES = re.compile(r"(?:0|[1-9][0-9]*)")
_T = TypeVar("_T")


_ERROR_RESPONSES = {
    status: {"model": ErrorEnvelope, "description": "Problem Locator error envelope."}
    for status in (400, 404, 409, 413, 422, 500, 502, 503, 504)
}
_UPLOAD_REQUEST_CONTENT = {
    content_type: {"schema": {"type": "string", "format": "binary"}}
    for content_type in (
        "application/gzip",
        "application/zip",
        "application/x-tar",
    )
}


@dataclass(frozen=True, slots=True)
class UploadHeaders:
    idempotency_key: str
    content_type: str
    content_length: int
    content_sha256: str


def _json(data: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status_code)


def _log_http_validation_failure(
    operation: str,
    arguments: dict[str, Any],
    error: ValidationError | RequestValidationError | ValueError | TypeError,
) -> None:
    log_event(
        "http.operation.validation_failed",
        level=logging.WARNING,
        operation=operation,
        arguments=arguments,
        validation_errors=validation_diagnostics(error),
        error=error,
    )


def _port_error_response(
    exc: ApplicationPortError,
    *,
    operation: str,
    arguments: dict[str, Any],
) -> JSONResponse:
    log_event(
        "http.operation.application_error",
        level=logging.WARNING,
        operation=operation,
        arguments=arguments,
        error_code=exc.error.code,
        application_error=exc.error,
        error=exc,
    )
    return _json(
        error_envelope(exc.error),
        status_code=http_status_for(exc.error),
    )


def _response_case_identity(response: ApplicationResponse) -> tuple[str, int]:
    """Use the durable receipt when r3 cannot reread the post-commit CaseView."""

    if response.case_view is not None:
        return response.case_view.case_id, response.case_view.case_revision
    receipt = response.business_receipt
    if receipt.case_id is None or receipt.case_revision is None:
        raise ValueError("application response contains no persisted case identity")
    return receipt.case_id, receipt.case_revision


async def _settle_worker(task: asyncio.Task[_T]) -> _T:
    """Wait for an uncancellable worker, tolerating repeated ASGI cancellation."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _port_call(
    function: Callable[..., _T],
    *args: Any,
    on_cancel: Callable[[], Awaitable[None]] | None = None,
    dispose_cancelled_result: Callable[[_T], None] | None = None,
) -> _T:
    """Run a synchronous Port without losing results or resources on cancel."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            await on_cancel()
        try:
            result = await _settle_worker(worker)
        except BaseException as exc:
            # Retrieve a worker failure so it is never logged as an unhandled
            # task exception; the cancelled HTTP request has no response sink.
            log_event(
                "http.cancelled_worker.failed",
                level=logging.ERROR,
                error=exc,
            )
        else:
            if dispose_cancelled_result is not None:
                dispose_cancelled_result(result)
        raise


class _ClosingStreamingResponse(StreamingResponse):
    """Close the frozen stream even if ASGI fails before iteration begins."""

    def __init__(self, stream: Any, *args: Any, **kwargs: Any) -> None:
        self._source_stream = stream
        super().__init__(*args, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BinaryStream.close is deliberately synchronous and idempotent.
            # Calling it here covers response-start/send failures where the
            # async body iterator's own finally block is never entered.
            self._source_stream.close()


def parse_upload_headers(request: Request, attachment_id: str) -> UploadHeaders:
    """Validate all four upload headers before exposing the request body."""

    typed_attachment_id = _OPAQUE_ID.validate_python(attachment_id)
    values: dict[bytes, list[bytes]] = {}
    for name, value in request.scope.get("headers", []):
        values.setdefault(name.lower(), []).append(value)

    required = {
        b"idempotency-key": "Idempotency-Key",
        b"content-type": "Content-Type",
        b"content-length": "Content-Length",
        b"x-content-sha256": "X-Content-SHA256",
    }
    decoded: dict[str, str] = {}
    for raw_name, public_name in required.items():
        matches = values.get(raw_name, [])
        if len(matches) != 1:
            raise ValueError(f"{public_name} must appear exactly once")
        try:
            decoded[public_name] = matches[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{public_name} must be ASCII") from exc

    if decoded["Idempotency-Key"] != typed_attachment_id:
        raise ValueError("Idempotency-Key must equal attachment_id")
    content_type = _CONTENT_TYPE.validate_python(decoded["Content-Type"])
    raw_length = decoded["Content-Length"]
    if _DECIMAL_BYTES.fullmatch(raw_length) is None:
        raise ValueError("Content-Length must be a canonical decimal integer")
    content_length = int(raw_length)
    if content_length > MAX_ATTACHMENT_BYTES:
        raise ValueError("Content-Length exceeds the V1 Attachment limit")
    content_sha256 = _SHA256.validate_python(decoded["X-Content-SHA256"])
    return UploadHeaders(
        idempotency_key=typed_attachment_id,
        content_type=content_type,
        content_length=content_length,
        content_sha256=content_sha256,
    )


def create_http_app(
    *,
    command_port: ApplicationCommandPort,
    query_port: ApplicationQueryPort,
    state_admin: StateAdminPort,
    public_base_url: str,
) -> FastAPI:
    """Create one ASGI application containing HTTP and stateless MCP routes."""

    mcp = create_mcp_transport(
        command_port,
        query_port,
        public_base_url=public_base_url,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="Problem Locator V1",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Idempotency-Key",
            "X-Content-SHA256",
        ],
        expose_headers=[
            "Content-Length",
            "Content-Type",
            "X-Content-SHA256",
            "X-Problem-Locator-Correlation-ID",
        ],
    )
    # Add diagnostics last so it wraps CORS-generated OPTIONS responses too.
    app.add_middleware(HttpDiagnosticsMiddleware)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        route = request.scope.get("route")
        operation = getattr(route, "name", None) or request.url.path
        arguments: dict[str, Any] = {
            "path": dict(request.path_params),
            "query": list(request.query_params.multi_items()),
        }
        if exc.body is not None:
            arguments["body"] = exc.body
        _log_http_validation_failure(operation, arguments, exc)
        error = validation_error_from(exc)
        return _json(error_envelope(error), status_code=http_status_for(error))

    @app.get("/live")
    async def live() -> JSONResponse:
        return _json(success_envelope({"status": "live"}))

    @app.get("/ready")
    async def ready() -> JSONResponse:
        report = await _port_call(state_admin.readiness)
        if report.ready:
            # Readiness check messages are not constrained by S00's safe
            # ApplicationError detail grammar.  Expose the frozen check names
            # and booleans, but never forward free-form infrastructure text.
            return _json(
                success_envelope(
                    {
                        "ready": True,
                        "checks": [
                            {
                                "name": check.name,
                                "passed": check.passed,
                                "message": None,
                            }
                            for check in report.checks
                        ],
                        "error": None,
                    }
                )
            )
        assert report.error is not None
        log_event(
            "service.readiness.failed",
            level=logging.ERROR,
            error_code=report.error.code,
            application_error=report.error,
            checks=report.checks,
        )
        return _json(
            error_envelope(report.error),
            status_code=http_status_for(report.error),
        )

    @app.post(
        "/api/v1/cases",
        response_model=ApplicationSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="create_case",
        tags=["cases"],
    )
    async def create_case(body: CreateCaseBody) -> JSONResponse:
        operation = "create_case"
        arguments: dict[str, Any] = {
            "body": body.model_dump(mode="json", by_alias=True)
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            problem_spec = ProblemSpecInput.model_validate(
                body.problem_spec.model_dump(mode="python"),
                strict=True,
            )
            initial_user_facts = [
                UserFactInput(name=fact.name, value=fact.value)
                for fact in body.initial_user_facts
            ]
            command = CreateCase(
                idempotency_key=body.request_id,
                raw_problem_text=body.raw_problem_text,
                problem_spec=problem_spec,
                initial_user_facts=initial_user_facts,
                wait_seconds=body.wait_seconds,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.get(
        "/api/v1/cases/{case_id}",
        response_model=CaseQuerySuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="get_case",
        tags=["cases"],
    )
    async def get_case(
        case_id: str,
        request: Request,
        wait_for_job_id: str | None = Query(default=None),
        wait_seconds: int = Query(default=0, ge=0, le=30),
    ) -> JSONResponse:
        operation = "get_case"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            query_items = list(request.query_params.multi_items())
            allowed = {"wait_for_job_id", "wait_seconds"}
            names = [name for name, _value in query_items]
            if any(name not in allowed for name in names):
                raise ValueError("query contains an unknown parameter")
            if len(names) != len(set(names)):
                raise ValueError("query parameters must appear at most once")
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            typed_wait_job_id = (
                None
                if wait_for_job_id is None
                else _OPAQUE_ID.validate_python(wait_for_job_id)
            )
            typed_wait_seconds = _WAIT_SECONDS.validate_python(wait_seconds)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(
                query_port.get_case,
                typed_case_id,
                typed_wait_job_id,
                typed_wait_seconds,
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.post(
        "/api/v1/cases/{case_id}/supplements",
        response_model=ApplicationSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="submit_supplement",
        tags=["cases"],
    )
    async def submit_supplement(
        case_id: str,
        body: SubmitSupplementBody,
    ) -> JSONResponse:
        operation = "submit_supplement"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "body": body.model_dump(mode="json", by_alias=True),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            command = SubmitSupplement(
                idempotency_key=body.request_id,
                case_id=typed_case_id,
                expected_case_revision=body.expected_case_revision,
                inputs={item.name: item.value for item in body.inputs},
                attachment_ids=body.attachment_ids,
                wait_seconds=body.wait_seconds,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _json(success_envelope(response))

    @app.post(
        "/api/v1/cases/{case_id}/attachments",
        response_model=PrepareAttachmentSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="prepare_attachment",
        tags=["attachments"],
    )
    async def prepare_attachment(
        case_id: str,
        body: PrepareAttachmentBody,
    ) -> JSONResponse:
        operation = "prepare_attachment"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "body": body.model_dump(mode="json", by_alias=True),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
            command = PrepareAttachment(
                idempotency_key=body.request_id,
                case_id=typed_case_id,
                expected_case_revision=body.expected_case_revision,
                name=body.name,
                content_type=body.content_type,
                declared_size=body.declared_size,
                declared_sha256=body.declared_sha256,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(command_port.execute, command)
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        descriptor = web_upload_descriptor(
            response,
            public_base_url=public_base_url,
            content_type=body.content_type,
            declared_size=body.declared_size,
            declared_sha256=body.declared_sha256,
        )
        return _json(
            success_envelope(
                {"application_response": response, "upload": descriptor}
            )
        )

    @app.put(
        "/api/v1/attachments/{attachment_id}/content",
        response_model=UploadReadySuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="upload_attachment",
        tags=["attachments"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": _UPLOAD_REQUEST_CONTENT,
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "X-Content-SHA256",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                {
                    "name": "Content-Length",
                    "in": "header",
                    "required": True,
                    "description": "Generated by Chrome for a File or Blob body; browser JavaScript must not set it.",
                    "schema": {"type": "integer", "minimum": 0},
                },
            ],
        },
    )
    async def upload_attachment(attachment_id: str, request: Request) -> JSONResponse:
        operation = "upload_attachment"
        arguments: dict[str, Any] = {
            "attachment_id": attachment_id,
            "headers": [
                [
                    bytes(name).decode("latin-1", errors="replace"),
                    bytes(value).decode("latin-1", errors="replace"),
                ]
                for name, value in request.scope.get("headers", [])
            ],
        }
        try:
            headers = parse_upload_headers(request, attachment_id)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))

        arguments["upload"] = {
            "idempotency_key": headers.idempotency_key,
            "content_type": headers.content_type,
            "content_length": headers.content_length,
            "content_sha256": headers.content_sha256,
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )

        stream = AsyncRequestBinaryStream(
            request.stream(),
            loop=asyncio.get_running_loop(),
        )
        try:
            try:
                command = UploadAttachmentContent.model_validate(
                    {
                        "idempotency_key": headers.idempotency_key,
                        "attachment_id": attachment_id,
                        "expected_content_type": headers.content_type,
                        "expected_size": headers.content_length,
                        "expected_sha256": headers.content_sha256,
                        "byte_stream": stream,
                    }
                )
            except ValidationError as exc:
                _log_http_validation_failure(operation, arguments, exc)
                error = validation_error_from(exc)
                return _json(error_envelope(error), status_code=http_status_for(error))
            try:
                response = await _port_call(
                    command_port.execute,
                    command,
                    on_cancel=stream.abort,
                )
            except ApplicationPortError as exc:
                return _port_error_response(
                    exc,
                    operation=operation,
                    arguments=arguments,
                )
        finally:
            await stream.aclose()

        response_case_id, response_case_revision = _response_case_identity(response)
        return _json(
            success_envelope(
                {
                    "attachment_id": attachment_id,
                    "case_id": response_case_id,
                    "status": "READY",
                    "case_revision": response_case_revision,
                }
            )
        )

    @app.get(
        "/api/v1/cases/{case_id}/artifacts",
        response_model=ArtifactListSuccessEnvelope,
        responses=_ERROR_RESPONSES,
        name="list_artifacts",
        tags=["artifacts"],
    )
    async def list_artifacts(case_id: str, request: Request) -> JSONResponse:
        operation = "list_artifacts"
        arguments: dict[str, Any] = {
            "case_id": case_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            if request.query_params:
                raise ValueError("artifact list does not accept query parameters")
            typed_case_id = _OPAQUE_ID.validate_python(case_id)
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))
        try:
            response = await _port_call(
                query_port.list_artifacts,
                typed_case_id,
                False,
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        views = [
            artifact_view(
                summary,
                case_id=typed_case_id,
                public_base_url=public_base_url,
            )
            for summary in response.artifacts
        ]
        return _json(success_envelope({"artifacts": views}))

    @app.get(
        "/api/v1/artifacts/{artifact_id}/content",
        responses={
            200: {
                "description": "Immutable downloadable artifact bytes.",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
                "headers": {
                    "Content-Length": {
                        "schema": {"type": "integer", "minimum": 0}
                    },
                    "Content-Type": {"schema": {"type": "string"}},
                    "X-Content-SHA256": {
                        "schema": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        }
                    },
                    "X-Problem-Locator-Correlation-ID": {
                        "schema": {"type": "string"}
                    },
                },
            },
            **_ERROR_RESPONSES,
        },
        name="download_artifact",
        tags=["artifacts"],
    )
    async def download_artifact(artifact_id: str, request: Request):
        operation = "download_artifact"
        arguments: dict[str, Any] = {
            "artifact_id": artifact_id,
            "query": list(request.query_params.multi_items()),
        }
        log_event(
            "http.operation.parameters",
            operation=operation,
            arguments=arguments,
        )
        try:
            typed_artifact_id = _OPAQUE_ID.validate_python(artifact_id)
            query_items = list(request.query_params.multi_items())
            if len(query_items) != 1 or query_items[0][0] != "case_id":
                raise ValueError("case_id must be the sole query parameter")
            case_id = _OPAQUE_ID.validate_python(query_items[0][1])
        except (ValidationError, ValueError, TypeError) as exc:
            _log_http_validation_failure(operation, arguments, exc)
            error = validation_error_from(exc)
            return _json(error_envelope(error), status_code=http_status_for(error))

        try:
            result = await _port_call(
                query_port.open_artifact,
                case_id,
                typed_artifact_id,
                dispose_cancelled_result=lambda item: item.stream.close(),
            )
        except ApplicationPortError as exc:
            return _port_error_response(
                exc,
                operation=operation,
                arguments=arguments,
            )
        return _ClosingStreamingResponse(
            result.stream,
            iterate_binary_stream(result.stream),
            media_type=None,
            headers={
                "Content-Length": str(result.artifact.size),
                "Content-Type": result.artifact.content_type,
                "X-Content-SHA256": result.artifact.sha256,
            },
        )

    # Route order matters: keep the raw MCP ASGI endpoint after all FastAPI
    # routes so no catch-all transport can shadow /live or /api/v1.
    app.add_route(
        "/mcp",
        mcp.asgi_application,
        methods=["GET", "POST", "DELETE"],
        name="mcp",
    )
    return app


__all__ = [
    "CreateCaseBody",
    "PrepareAttachmentBody",
    "SubmitSupplementBody",
    "UploadHeaders",
    "create_http_app",
    "parse_upload_headers",
]
