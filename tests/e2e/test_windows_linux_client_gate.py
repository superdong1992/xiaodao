from __future__ import annotations

import json
import os
from pathlib import Path
import platform

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from problem_locator import __version__
from problem_locator.contracts.enums import ErrorCode
from tests.unit.interfaces.helpers import problem_spec_input


CREATE_CASE = "problem_locator_create_case"
REMOTE_GATE = "PROBLEM_LOCATOR_WINDOWS_LINUX_GATE"
REMOTE_URL = "PROBLEM_LOCATOR_LINUX_MCP_URL"
REMOTE_HEADERS = "PROBLEM_LOCATOR_LINUX_MCP_HEADERS_JSON"
PROXY_COMMAND = "PROBLEM_LOCATOR_WINDOWS_PROXY_COMMAND"
REAL_HOST_GATE = "PROBLEM_LOCATOR_REAL_HOST_DFX_GATE"
REAL_HOST_LOG = "PROBLEM_LOCATOR_REAL_HOST_DFX_LOG"
REAL_HOST_REQUEST_ID = "PROBLEM_LOCATOR_REAL_HOST_REQUEST_ID"


def _skip_unless_windows_gate(name: str, reason: str) -> None:
    if platform.system() != "Windows" or os.environ.get(name) != "1":
        pytest.skip(reason)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for this explicit E2E gate"
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
    assert path.is_file(), f"client DFX log does not exist: {path}"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_discovered_strict_create_schema(
    events: list[dict[str, object]],
) -> None:
    discovered = next(
        event
        for event in reversed(events)
        if event.get("event") == "client.proxy.tools.discovered"
    )
    assert discovered["schema_mode"] == "strict"
    assert discovered["package_version"] == __version__
    assert discovered["client_proxy_version"] == __version__
    tools = discovered["advertised_tools"]
    assert isinstance(tools, list)
    create = next(tool for tool in tools if tool["name"] == CREATE_CASE)
    schema = create["inputSchema"]
    validator = Draft202012Validator(schema)
    valid = {
        "request_id": "10000000-0000-0000-0000-000000000001",
        "problem_spec": problem_spec_input(),
    }
    assert validator.is_valid(valid)
    assert validator.is_valid(
        {**valid, "problem_spec": json.dumps(problem_spec_input())}
    ) is False
    schema_hashes = discovered["advertised_schema_sha256"]
    assert len(schema_hashes[CREATE_CASE]) == 64


def test_windows_proxy_to_real_linux_mcp_preserves_compound_json_types(
    tmp_path: Path,
) -> None:
    """Exercise Windows stdio -> proxy -> real Linux HTTP without mutating state."""

    _skip_unless_windows_gate(
        REMOTE_GATE,
        "requires the explicit Windows-to-Linux client release gate",
    )
    upstream_url = _required_environment(REMOTE_URL)
    assert upstream_url.startswith(("http://", "https://"))
    log_file = tmp_path / "windows-linux-client-dfx.jsonl"
    arguments = [
        "--url",
        upstream_url,
        "--log-file",
        str(log_file),
        "--schema-mode",
        "strict",
    ]
    for name, value in sorted(_headers().items()):
        arguments.extend(["--header", f"{name}: {value}"])

    child_environment = dict(os.environ)
    child_environment["PROBLEM_LOCATOR_CLIENT_DFX_LOG_LEVEL"] = "INFO"

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=os.environ.get(
                PROXY_COMMAND,
                "problem-locator-client-proxy",
            ),
            args=arguments,
            env=child_environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.version == __version__
                listed = await session.list_tools()
                create = next(
                    tool for tool in listed.tools if tool.name == CREATE_CASE
                )
                validator = Draft202012Validator(create.inputSchema)
                base = {
                    # Deliberately invalid so the remote service performs no
                    # command and persists no Case.
                    "request_id": "",
                    "problem_spec": problem_spec_input(),
                }
                assert validator.is_valid(base) is False

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

    events = _read_events(log_file)
    _assert_discovered_strict_create_schema(events)
    started = [
        event
        for event in events
        if event.get("event") == "client.mcp.attempt.started"
        and event.get("tool_name") == CREATE_CASE
    ]
    assert [event["argument_json_types"]["problem_spec"] for event in started] == [
        "object",
        "string",
    ]


def test_real_host_dfx_proves_problem_spec_entered_proxy_as_an_object() -> None:
    """Validate evidence produced by an actual Agent/MCP Host acceptance call."""

    _skip_unless_windows_gate(
        REAL_HOST_GATE,
        "requires explicit DFX evidence from a real Windows Agent/MCP Host",
    )
    log_path = Path(_required_environment(REAL_HOST_LOG))
    request_id = _required_environment(REAL_HOST_REQUEST_ID)
    events = _read_events(log_path)
    _assert_discovered_strict_create_schema(events)

    attempts = [
        event
        for event in events
        if event.get("event") == "client.mcp.attempt.started"
        and event.get("tool_name") == CREATE_CASE
        and event.get("operation_id") == request_id
    ]
    assert attempts, f"no create_case attempt found for request_id={request_id}"
    latest = attempts[-1]
    assert latest["schema_mode"] == "strict"
    assert latest["client_proxy_version"] == __version__
    assert latest["argument_json_types"]["problem_spec"] == "object"
    assert isinstance(latest["arguments"]["problem_spec"], dict)
