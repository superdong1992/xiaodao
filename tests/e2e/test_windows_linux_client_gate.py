from __future__ import annotations

import json
import os
from pathlib import Path
import platform
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
REAL_HOST_GATE = "PROBLEM_LOCATOR_REAL_HOST_HOOK_GATE"
REAL_HOST_LOG = "PROBLEM_LOCATOR_REAL_HOST_HOOK_LOG"
REAL_HOST_SERVER_LOG = "PROBLEM_LOCATOR_REAL_HOST_SERVER_DFX_LOG"
REAL_HOST_REQUEST_ID = "PROBLEM_LOCATOR_REAL_HOST_REQUEST_ID"
REAL_HOST_CLAUDE_VERSION = "PROBLEM_LOCATOR_REAL_HOST_CLAUDE_VERSION"
LEGACY_HOST_GATE = "PROBLEM_LOCATOR_LEGACY_HOST_HOOK_GATE"
LEGACY_HOST_LOG = "PROBLEM_LOCATOR_LEGACY_HOST_HOOK_LOG"
LEGACY_HOST_SERVER_LOG = "PROBLEM_LOCATOR_LEGACY_HOST_SERVER_DFX_LOG"
LEGACY_HOST_REQUEST_ID = "PROBLEM_LOCATOR_LEGACY_HOST_REQUEST_ID"
LEGACY_HOST_CLAUDE_IDENTITY = "PROBLEM_LOCATOR_LEGACY_HOST_CLAUDE_IDENTITY"
OFFICIAL_CREATE_CASE = f"mcp__problem-locator__{CREATE_CASE}"
LEGACY_CREATE_CASE = f"problem_locator_{CREATE_CASE}"
UNUSABLE_PROXY_URL = "http://127.0.0.1:9"


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
    assert path.is_file(), f"DFX log does not exist: {path}"
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


def _assert_real_host_evidence(
    *,
    hook_log_environment: str,
    server_log_environment: str,
    request_id_environment: str,
    expected_full_tool_name: str,
) -> None:
    request_id = _required_environment(request_id_environment)
    hook_events = _read_events(Path(_required_environment(hook_log_environment)))
    started = [
        event
        for event in hook_events
        if event.get("event") == "client.hook.tool.started"
        and event.get("logical_tool") == CREATE_CASE
        and event.get("operation_id") == request_id
    ]
    assert started, f"no real Host create_case Hook event for {request_id}"
    latest = started[-1]
    assert latest["source"] == "claude_code_hook"
    assert latest["hook_version"] == __version__
    assert latest["tool_name"] == expected_full_tool_name
    # The DFX Hook runs alongside the compatibility Hook and therefore records
    # the unmodified Host boundary. The server event below must prove that the
    # same request was repaired before transport.
    assert latest["argument_json_types"]["problem_spec"] == "string"
    assert isinstance(latest["arguments"]["problem_spec"], str)

    terminal = [
        event
        for event in hook_events
        if event.get("event") in {
            "client.hook.tool.returned",
            "client.hook.tool.failed",
        }
        and event.get("session_id") == latest["session_id"]
        and event.get("tool_use_id") == latest["tool_use_id"]
    ]
    assert terminal, "real Host Hook evidence has no terminal tool event"

    server_log = os.environ.get(server_log_environment)
    if os.environ.get(RELEASE_REQUIRED) == "1":
        assert server_log, f"{server_log_environment} is required for release"
    if not server_log:
        return

    server_events = _read_events(Path(server_log))
    listed = [
        event for event in server_events if event.get("event") == "mcp.tools.listed"
    ]
    assert listed and listed[-1]["server_version"] == __version__
    server_started = [
        event
        for event in server_events
        if event.get("event") == "mcp.tool.started"
        and event.get("request_id") == request_id
        and event.get("tool") == CREATE_CASE
    ]
    assert server_started, f"no server-side create_case event for {request_id}"
    assert isinstance(server_started[-1]["arguments"]["problem_spec"], dict)


def test_windows_direct_http_to_real_linux_mcp_preserves_compound_json_types() -> None:
    """Probe the authoritative remote schema without a local MCP process."""

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
                    create = next(
                        tool for tool in listed.tools if tool.name == CREATE_CASE
                    )
                    validator = Draft202012Validator(create.inputSchema)
                    base = {
                        # Empty request_id forces validation before persistence.
                        "request_id": "",
                        "problem_spec": problem_spec_input(),
                    }
                    assert validator.is_valid(base) is False
                    assert validator.is_valid(
                        {**base, "problem_spec": json.dumps(problem_spec_input())}
                    ) is False

                    object_result = await session.call_tool(CREATE_CASE, base)
                    object_errors = _validation_fields(object_result)
                    assert "request_id" in object_errors
                    assert "problem_spec" not in object_errors

                    string_result = await session.call_tool(
                        CREATE_CASE,
                        {
                            **base,
                            "problem_spec": json.dumps(problem_spec_input()),
                        },
                    )
                    string_errors = _validation_fields(string_result)
                    assert "request_id" in string_errors
                    assert "problem_spec" in string_errors

    anyio.run(scenario)


def test_real_host_hook_repairs_string_before_the_linux_service() -> None:
    """Validate the real npm Claude Code 2.1.89 compatibility boundary."""

    _skip_unless_windows_gate(
        REAL_HOST_GATE,
        "requires real Claude Code, Skill, Hook, and Linux service evidence",
    )
    _assert_real_host_evidence(
        hook_log_environment=REAL_HOST_LOG,
        server_log_environment=REAL_HOST_SERVER_LOG,
        request_id_environment=REAL_HOST_REQUEST_ID,
        expected_full_tool_name=OFFICIAL_CREATE_CASE,
    )

    version = _required_environment(REAL_HOST_CLAUDE_VERSION)
    assert version == "2.1.89 (Claude Code)"
    if os.environ.get(RELEASE_REQUIRED) == "1":
        _assert_no_proxy_covers_remote(_required_environment(REMOTE_URL))



def test_legacy_host_hook_repairs_string_before_the_linux_service() -> None:
    """Require correlated deployment evidence from the legacy/custom Host."""

    _skip_unless_windows_gate(
        LEGACY_HOST_GATE,
        "requires legacy/custom Claude Code Hook and Linux service evidence",
    )
    _assert_real_host_evidence(
        hook_log_environment=LEGACY_HOST_LOG,
        server_log_environment=LEGACY_HOST_SERVER_LOG,
        request_id_environment=LEGACY_HOST_REQUEST_ID,
        expected_full_tool_name=LEGACY_CREATE_CASE,
    )

    if os.environ.get(RELEASE_REQUIRED) == "1":
        identity = _required_environment(LEGACY_HOST_CLAUDE_IDENTITY)
        assert identity.strip(), "legacy/custom Claude Code identity must be nonempty"
        _assert_no_proxy_covers_remote(_required_environment(REMOTE_URL))
