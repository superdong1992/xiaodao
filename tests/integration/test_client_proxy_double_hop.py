from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import uvicorn

from problem_locator.contracts.enums import ErrorCode
from problem_locator.interfaces.http_app import create_http_app
from tests.unit.interfaces.fakes import (
    FakeApplicationService,
    FakeQuery,
    FakeStateAdmin,
)
from tests.unit.interfaces.helpers import (
    application_response,
    problem_spec_input,
    readiness,
)


ROOT = Path(__file__).resolve().parents[2]
CREATE_CASE = "problem_locator_create_case"
REQUEST_ID = "10000000-0000-0000-0000-000000000001"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_server(app: object, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "loopback MCP test server did not start"
    return server, thread


def test_stdio_proxy_preserves_nested_object_across_http_and_logs_string_drift(
    tmp_path: Path,
) -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase", revision=1)]
    )
    port = _free_loopback_port()
    upstream_url = f"http://127.0.0.1:{port}/mcp"
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url=f"http://127.0.0.1:{port}",
    )
    server, thread = _start_server(app, port)
    log_file = tmp_path / "client-dfx.jsonl"

    child_env = dict(os.environ)
    existing_pythonpath = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(ROOT / "src"), existing_pythonpath)
        if part
    )
    child_env.update(
        {
            "PROBLEM_LOCATOR_MCP_URL": upstream_url,
            "PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE": str(log_file),
            "PROBLEM_LOCATOR_CLIENT_DFX_LOG_LEVEL": "INFO",
            "PROBLEM_LOCATOR_CLIENT_SCHEMA_MODE": "strict",
        }
    )

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "problem_locator.interfaces.client_proxy"],
            env=child_env,
            cwd=str(ROOT),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                create_tool = next(
                    tool for tool in listed.tools if tool.name == CREATE_CASE
                )
                create_validator = Draft202012Validator(create_tool.inputSchema)
                object_arguments = {
                    "request_id": REQUEST_ID,
                    "problem_spec": problem_spec_input(),
                    "initial_user_facts": [],
                    "wait_seconds": 0,
                }
                assert create_validator.is_valid(object_arguments)
                assert create_validator.is_valid(
                    {
                        **object_arguments,
                        "problem_spec": json.dumps(problem_spec_input()),
                    }
                ) is False

                created = await session.call_tool(CREATE_CASE, object_arguments)
                assert created.structuredContent["ok"] is True

                encoded = await session.call_tool(
                    CREATE_CASE,
                    {
                        **object_arguments,
                        "request_id": (
                            "10000000-0000-0000-0000-000000000002"
                        ),
                        "problem_spec": json.dumps(problem_spec_input()),
                    },
                )
                assert encoded.structuredContent["ok"] is False
                assert encoded.structuredContent["error"]["code"] == (
                    ErrorCode.VALIDATION_ERROR.value
                )

    try:
        anyio.run(scenario)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert thread.is_alive() is False

    assert len(command.calls) == 1
    events = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
    ]
    discovered = next(
        event
        for event in events
        if event["event"] == "client.proxy.tools.discovered"
    )
    assert discovered["schema_mode"] == "strict"
    assert discovered["client_proxy_version"] == "1.0.1"
    assert len(discovered["advertised_schema_sha256"][CREATE_CASE]) == 64
    started = [
        event
        for event in events
        if event["event"] == "client.mcp.attempt.started"
    ]
    assert [event["argument_json_types"]["problem_spec"] for event in started] == [
        "object",
        "string",
    ]
    assert isinstance(started[0]["arguments"]["problem_spec"], dict)
    assert isinstance(started[1]["arguments"]["problem_spec"], str)
