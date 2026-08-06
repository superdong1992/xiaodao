from __future__ import annotations

import json
import os
from pathlib import Path
import platform
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from problem_locator import __version__
from problem_locator.contracts.enums import ErrorCode
from tests.unit.interfaces.helpers import problem_spec_input


CREATE_CASE = "problem_locator_create_case"
REMOTE_GATE = "PROBLEM_LOCATOR_WINDOWS_LINUX_GATE"
REMOTE_URL = "PROBLEM_LOCATOR_LINUX_MCP_URL"
REMOTE_HEADERS = "PROBLEM_LOCATOR_LINUX_MCP_HEADERS_JSON"
RELEASE_REQUIRED = "PROBLEM_LOCATOR_RELEASE_GATES_REQUIRED"
REAL_HOST_GATE = "PROBLEM_LOCATOR_REAL_HOST_FLAT_GATE"
REAL_HOST_SERVER_LOG = "PROBLEM_LOCATOR_REAL_HOST_SERVER_DFX_LOG"
REAL_HOST_REQUEST_ID = "PROBLEM_LOCATOR_REAL_HOST_REQUEST_ID"
REAL_HOST_CLAUDE_VERSION = "PROBLEM_LOCATOR_REAL_HOST_CLAUDE_VERSION"
UNUSABLE_PROXY_URL = "http://127.0.0.1:9"
SCALAR_TYPES = {"boolean", "integer", "number", "string"}


def _skip_unless_windows_gate(name: str, reason: str) -> None:
    enabled = platform.system() == "Windows" and os.environ.get(name) == "1"
    if enabled:
        return
    if os.environ.get(RELEASE_REQUIRED) == "1":
        pytest.fail(f"required release gate is unavailable: {name}: {reason}")
    pytest.skip(reason)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for this E2E gate"
    return value


def _headers() -> dict[str, str]:
    raw = os.environ.get(REMOTE_HEADERS, "{}")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict), f"{REMOTE_HEADERS} must be a JSON object"
    assert all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in parsed.items()
    ), f"{REMOTE_HEADERS} names and values must be strings"
    return parsed


def _validation_fields(result: object) -> set[str]:
    structured = getattr(result, "structuredContent", None)
    assert isinstance(structured, dict)
    assert structured["ok"] is False
    error = structured["error"]
    assert error["code"] == ErrorCode.VALIDATION_ERROR.value
    return {
        str(detail["field"])
        for detail in error["details"]
        if isinstance(detail, dict) and "field" in detail
    }


def _read_events(path: Path) -> list[dict[str, object]]:
    assert path.is_file(), f"server DFX log does not exist: {path}"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_no_proxy_covers_remote(remote_url: str) -> None:
    hostname = urlsplit(remote_url).hostname
    assert hostname, f"invalid remote MCP URL: {remote_url}"
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = {
        item.strip().split(":", 1)[0]
        for comma_part in no_proxy.split(",")
        for item in comma_part.split()
        if item.strip()
    }
    assert "*" not in entries, "release gates forbid NO_PROXY=*"
    assert hostname in entries, f"NO_PROXY must contain the MCP host {hostname}"


def _assert_unusable_proxies_are_configured() -> None:
    assert os.environ.get("HTTP_PROXY") == UNUSABLE_PROXY_URL
    assert os.environ.get("HTTPS_PROXY") == UNUSABLE_PROXY_URL


def _is_nullable_scalar(schema: dict[str, Any]) -> bool:
    if schema.get("type") in SCALAR_TYPES:
        return True
    variants = schema.get("anyOf")
    if not isinstance(variants, list) or not variants:
        return False
    types = {
        item.get("type") for item in variants if isinstance(item, dict)
    }
    return "null" in types and types - {"null"} <= SCALAR_TYPES


def _assert_flat_schema(schema: dict[str, Any]) -> None:
    assert schema.get("type") == "object"
    assert "$defs" not in schema
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    for name, value in properties.items():
        assert isinstance(value, dict), name
        assert "$ref" not in value, name
        if _is_nullable_scalar(value):
            continue
        assert value.get("type") == "array", name
        items = value.get("items")
        assert isinstance(items, dict) and _is_nullable_scalar(items), name


def _flat_create_arguments(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        **problem_spec_input(),
        "initial_user_fact_names": [],
        "initial_user_fact_values": [],
        "wait_seconds": 0,
    }


def test_windows_direct_http_to_real_linux_mcp_uses_only_flat_inputs() -> None:
    _skip_unless_windows_gate(
        REMOTE_GATE,
        "requires the explicit Windows-to-Linux HTTP release gate",
    )
    upstream_url = _required_environment(REMOTE_URL)
    assert upstream_url.startswith(("http://", "https://"))
    assert urlsplit(upstream_url).path.endswith("/mcp")
    _assert_no_proxy_covers_remote(upstream_url)
    if os.environ.get(RELEASE_REQUIRED) == "1":
        _assert_unusable_proxies_are_configured()

    async def scenario() -> None:
        async with httpx.AsyncClient(headers=_headers(), timeout=30) as http_client:
            async with streamable_http_client(
                upstream_url,
                http_client=http_client,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.version == __version__
                    listed = await session.list_tools()
                    assert len(listed.tools) == 7
                    for tool in listed.tools:
                        _assert_flat_schema(tool.inputSchema)

                    create = next(
                        tool for tool in listed.tools if tool.name == CREATE_CASE
                    )
                    validator = Draft202012Validator(create.inputSchema)
                    flat = _flat_create_arguments("")
                    assert validator.is_valid(flat) is False
                    assert validator.is_valid(
                        {**flat, "problem_spec": problem_spec_input()}
                    ) is False

                    flat_result = await session.call_tool(CREATE_CASE, flat)
                    flat_errors = _validation_fields(flat_result)
                    assert "request_id" in flat_errors
                    assert "statement" not in flat_errors

                    legacy_result = await session.call_tool(
                        CREATE_CASE,
                        {**flat, "problem_spec": problem_spec_input()},
                    )
                    legacy_errors = _validation_fields(legacy_result)
                    assert {"request_id", "problem_spec"}.issubset(legacy_errors)

    anyio.run(scenario)


def test_real_host_sends_flat_inputs_to_the_linux_service() -> None:
    _skip_unless_windows_gate(
        REAL_HOST_GATE,
        "requires real Claude Code, Skill, and Linux service evidence",
    )
    request_id = _required_environment(REAL_HOST_REQUEST_ID)
    events = _read_events(Path(_required_environment(REAL_HOST_SERVER_LOG)))

    listed = [event for event in events if event.get("event") == "mcp.tools.listed"]
    assert listed and listed[-1]["server_version"] == __version__
    advertised = listed[-1]["tools"]
    assert isinstance(advertised, list)
    for tool in advertised:
        assert isinstance(tool, dict)
        _assert_flat_schema(tool["input_schema"])

    started = [
        event
        for event in events
        if event.get("event") == "mcp.tool.started"
        and event.get("request_id") == request_id
        and event.get("tool") == CREATE_CASE
    ]
    assert started, f"no server-side flat create_case event for {request_id}"
    arguments = started[-1]["arguments"]
    assert isinstance(arguments, dict)
    assert "problem_spec" not in arguments
    assert "initial_user_facts" not in arguments
    assert isinstance(arguments.get("statement"), str)
    for name in (
        "goals",
        "non_goals",
        "constraints",
        "completion_criteria",
        "initial_user_fact_names",
        "initial_user_fact_values",
    ):
        assert isinstance(arguments.get(name), list), name

    completed = [
        event
        for event in events
        if event.get("event") == "mcp.tool.completed"
        and event.get("request_id") == request_id
        and event.get("tool") == CREATE_CASE
    ]
    assert completed and completed[-1].get("ok") is True

    version = _required_environment(REAL_HOST_CLAUDE_VERSION)
    assert version == "2.1.89 (Claude Code)"
    if os.environ.get(RELEASE_REQUIRED) == "1":
        _assert_no_proxy_covers_remote(_required_environment(REMOTE_URL))
