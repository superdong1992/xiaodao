"""Local stdio MCP proxy with durable client-side diagnostics."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from problem_locator.diagnostics import configure_diagnostics, log_event


CLIENT_PROXY_NAME = "problem-locator-client-proxy"
CLIENT_PROXY_VERSION = "1.0.0"
DEFAULT_CLIENT_DFX_LOG = Path(".problem-locator/client-dfx.jsonl")
SUPPORTED_TOOLS = (
    "problem_locator_create_case",
    "problem_locator_prepare_attachment",
    "problem_locator_submit_supplement",
    "problem_locator_get_case",
    "problem_locator_resume_case",
    "problem_locator_cancel_case",
    "problem_locator_list_artifacts",
)


class UpstreamMcpSession(Protocol):
    async def list_tools(
        self,
        cursor: str | None = None,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListToolsResult: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: Any | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult: ...


EventLogger = Callable[..., None]


def _permissive_input_schema(original: Mapping[str, Any]) -> dict[str, Any]:
    """Keep field names/descriptions while moving validation behind the proxy."""

    properties: dict[str, dict[str, str]] = {}
    original_properties = original.get("properties")
    if isinstance(original_properties, Mapping):
        for raw_name, raw_schema in original_properties.items():
            name = str(raw_name)
            advertised: dict[str, str] = {}
            if isinstance(raw_schema, Mapping):
                description = raw_schema.get("description")
                if isinstance(description, str):
                    advertised["description"] = description
            properties[name] = advertised
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


def _operation_id(tool_name: str, arguments: Mapping[str, Any]) -> str:
    request_id = arguments.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    case_id = arguments.get("case_id")
    if isinstance(case_id, str) and case_id:
        return f"{tool_name}:{case_id}"
    return f"{tool_name}:unidentified"


def _result_is_error(result: types.CallToolResult) -> bool:
    if result.isError:
        return True
    structured = result.structuredContent
    return isinstance(structured, Mapping) and structured.get("ok") is False


class ClientMcpProxy:
    """Expose permissive local tools and forward their raw arguments upstream."""

    def __init__(
        self,
        upstream: UpstreamMcpSession,
        *,
        event_logger: EventLogger = log_event,
    ) -> None:
        self._upstream = upstream
        self._event_logger = event_logger
        self._attempt_counts: dict[str, int] = {}
        self._attempt_lock = anyio.Lock()

    async def list_tools(self) -> list[types.Tool]:
        upstream_tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            page = await self._upstream.list_tools(cursor=cursor)
            upstream_tools.extend(page.tools)
            cursor = page.nextCursor
            if cursor is None:
                break

        by_name = {tool.name: tool for tool in upstream_tools}
        missing = [name for name in SUPPORTED_TOOLS if name not in by_name]
        advertised_tools = (
            []
            if missing
            else [
                by_name[name].model_copy(
                    update={
                        "inputSchema": _permissive_input_schema(
                            by_name[name].inputSchema
                        ),
                        "outputSchema": None,
                    }
                )
                for name in SUPPORTED_TOOLS
            ]
        )
        self._event_logger(
            "client.proxy.tools.discovered",
            level=logging.ERROR if missing else logging.INFO,
            upstream_tools=[
                tool.model_dump(mode="json", by_alias=True, exclude_none=False)
                for tool in upstream_tools
            ],
            advertised_tools=[
                tool.model_dump(mode="json", by_alias=True, exclude_none=False)
                for tool in advertised_tools
            ],
            missing_tools=missing,
        )
        if missing:
            raise RuntimeError(
                "upstream MCP is missing Problem Locator tools: " + ", ".join(missing)
            )

        return advertised_tools

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
    ) -> types.CallToolResult:
        raw_arguments = dict(arguments or {})
        operation_id = _operation_id(name, raw_arguments)
        attempt_id = str(uuid.uuid4())
        async with self._attempt_lock:
            attempt_number = self._attempt_counts.get(operation_id, 0) + 1
            self._attempt_counts[operation_id] = attempt_number

        self._event_logger(
            "client.mcp.attempt.started",
            operation_id=operation_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            tool_name=name,
            arguments=raw_arguments,
        )
        started = time.perf_counter()

        if name not in SUPPORTED_TOOLS:
            result = types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Unsupported Problem Locator tool: {name}",
                    )
                ],
                isError=True,
            )
            self._event_logger(
                "client.mcp.attempt.completed",
                level=logging.ERROR,
                operation_id=operation_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                tool_name=name,
                arguments=raw_arguments,
                outcome="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                response=result.model_dump(
                    mode="json", by_alias=True, exclude_none=False
                ),
            )
            return result

        try:
            result = await self._upstream.call_tool(name, raw_arguments)
        except BaseException as exc:
            self._event_logger(
                "client.mcp.attempt.failed",
                level=logging.ERROR,
                operation_id=operation_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                tool_name=name,
                arguments=raw_arguments,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=exc,
            )
            raise

        outcome = "error" if _result_is_error(result) else "success"
        self._event_logger(
            "client.mcp.attempt.completed",
            level=logging.ERROR if outcome == "error" else logging.INFO,
            operation_id=operation_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            tool_name=name,
            arguments=raw_arguments,
            outcome=outcome,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            response=result.model_dump(mode="json", by_alias=True, exclude_none=False),
        )
        return result


@dataclass(frozen=True, slots=True)
class ClientProxySettings:
    upstream_url: str
    log_file: Path
    log_level: str
    headers: dict[str, str]
    timeout_seconds: float


def _parse_header(value: str) -> tuple[str, str]:
    name, separator, header_value = value.partition(":")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError("headers must use 'Name: value'")
    return name.strip(), header_value.lstrip()


def _settings(argv: Sequence[str] | None) -> ClientProxySettings:
    parser = argparse.ArgumentParser(prog="problem-locator-client-proxy")
    parser.add_argument(
        "--url",
        default=os.environ.get("PROBLEM_LOCATOR_MCP_URL"),
        help="upstream Streamable HTTP MCP URL",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=os.environ.get("PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE"),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PROBLEM_LOCATOR_CLIENT_DFX_LOG_LEVEL", "INFO"),
    )
    parser.add_argument(
        "--header",
        action="append",
        type=_parse_header,
        default=[],
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    arguments = parser.parse_args(argv)

    if not arguments.url:
        parser.error("--url or PROBLEM_LOCATOR_MCP_URL is required")
    if arguments.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    project_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
    log_file = (
        Path(arguments.log_file)
        if arguments.log_file is not None
        else DEFAULT_CLIENT_DFX_LOG
    )
    if not log_file.is_absolute():
        log_file = project_root / log_file

    return ClientProxySettings(
        upstream_url=str(arguments.url),
        log_file=log_file,
        log_level=str(arguments.log_level).upper(),
        headers=dict(arguments.header),
        timeout_seconds=float(arguments.timeout_seconds),
    )


def _upstream_http_client(settings: ClientProxySettings) -> httpx.AsyncClient:
    """Build a direct client that never inherits ambient proxy settings."""

    return httpx.AsyncClient(
        headers=settings.headers or None,
        timeout=httpx.Timeout(
            settings.timeout_seconds,
            read=settings.timeout_seconds,
        ),
        follow_redirects=True,
        trust_env=False,
    )


def _build_server(settings: ClientProxySettings) -> Server[ClientMcpProxy]:
    @asynccontextmanager
    async def lifespan(_server: Server[ClientMcpProxy]):
        log_event(
            "client.proxy.upstream.connecting",
            upstream_url=settings.upstream_url,
            headers=settings.headers,
            timeout_seconds=settings.timeout_seconds,
        )
        async with _upstream_http_client(settings) as http_client:
            async with streamable_http_client(
                settings.upstream_url,
                http_client=http_client,
            ) as (read_stream, write_stream, get_session_id):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(
                        seconds=settings.timeout_seconds
                    ),
                ) as session:
                    initialized = await session.initialize()
                    log_event(
                        "client.proxy.upstream.connected",
                        upstream_url=settings.upstream_url,
                        upstream_session_id=get_session_id(),
                        initialize_result=initialized,
                    )
                    yield ClientMcpProxy(session)
        log_event("client.proxy.upstream.disconnected")

    server: Server[ClientMcpProxy] = Server(
        CLIENT_PROXY_NAME,
        version=CLIENT_PROXY_VERSION,
        instructions=(
            "Client-side diagnostic proxy for the seven Problem Locator tools."
        ),
        lifespan=lifespan,
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await server.request_context.lifespan_context.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> types.CallToolResult:
        return await server.request_context.lifespan_context.call_tool(
            name,
            arguments,
        )

    return server


async def _run(settings: ClientProxySettings) -> None:
    server = _build_server(settings)
    log_event(
        "client.proxy.started",
        upstream_url=settings.upstream_url,
        client_log_file=settings.log_file,
        client_log_level=settings.log_level,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=CLIENT_PROXY_NAME,
                    server_version=CLIENT_PROXY_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        log_event("client.proxy.stopped")


def main(argv: Sequence[str] | None = None) -> int:
    settings = _settings(argv)
    try:
        configure_diagnostics(
            settings.log_level,
            log_file=settings.log_file,
        )
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"problem-locator-client-proxy: log setup failed: {exc}\n")
        return 3

    try:
        anyio.run(_run, settings)
    except BaseException as exc:
        log_event("client.proxy.failed", level=logging.ERROR, error=exc)
        sys.stderr.write(f"problem-locator-client-proxy: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CLIENT_PROXY_NAME",
    "CLIENT_PROXY_VERSION",
    "ClientMcpProxy",
    "ClientProxySettings",
    "DEFAULT_CLIENT_DFX_LOG",
    "SUPPORTED_TOOLS",
    "main",
]
