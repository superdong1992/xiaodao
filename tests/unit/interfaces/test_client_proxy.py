from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp import types

from problem_locator.interfaces.client_proxy import (
    SUPPORTED_TOOLS,
    ClientMcpProxy,
    _settings,
)


class FakeUpstream:
    def __init__(self, results: list[types.CallToolResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(
        self,
        cursor: str | None = None,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListToolsResult:
        assert cursor is None and params is None
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=f"Call {name}",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "request_id": {
                                "type": "string",
                                "description": "Stable logical request ID.",
                            },
                            "wait_seconds": {
                                "type": "integer",
                                "minimum": 0,
                            },
                        },
                        "required": ["request_id"],
                        "additionalProperties": False,
                    },
                    outputSchema={"type": "object"},
                )
                for name in SUPPORTED_TOOLS
            ]
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: Any | None = None,
        progress_callback: Any | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        assert read_timeout_seconds is None
        assert progress_callback is None and meta is None
        copied = dict(arguments or {})
        self.calls.append((name, copied))
        return self.results.pop(0)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, **fields: Any) -> None:
        self.items.append((event, fields))


def test_proxy_advertises_permissive_schema_but_logs_the_upstream_schema() -> None:
    async def scenario() -> tuple[list[types.Tool], Events]:
        events = Events()
        proxy = ClientMcpProxy(FakeUpstream(), event_logger=events)
        return await proxy.list_tools(), events

    advertised, events = anyio.run(scenario)

    assert [tool.name for tool in advertised] == list(SUPPORTED_TOOLS)
    for tool in advertised:
        Draft202012Validator(tool.inputSchema).validate(
            {
                "request_id": ["the client must not reject this value"],
                "wait_seconds": "also intentionally wrong",
                "unexpected": {"nested": True},
            }
        )
        assert tool.outputSchema is None
        assert tool.inputSchema["properties"]["request_id"] == {
            "description": "Stable logical request ID."
        }

    event, fields = events.items[0]
    assert event == "client.proxy.tools.discovered"
    assert fields["missing_tools"] == []
    original = fields["upstream_tools"][0]["inputSchema"]
    assert original["required"] == ["request_id"]
    assert original["properties"]["wait_seconds"]["type"] == "integer"
    assert fields["advertised_tools"][0]["inputSchema"] == advertised[0].inputSchema


def test_proxy_logs_every_retry_with_full_arguments_and_response() -> None:
    rejected = types.CallToolResult(
        content=[types.TextContent(type="text", text="VALIDATION_ERROR")],
        structuredContent={
            "ok": False,
            "data": None,
            "error": {"code": "VALIDATION_ERROR", "details": ["bad field"]},
        },
        isError=False,
    )
    succeeded = types.CallToolResult(
        content=[types.TextContent(type="text", text="created")],
        structuredContent={"ok": True, "data": {"case_id": "case-1"}, "error": None},
        isError=False,
    )
    upstream = FakeUpstream([rejected, succeeded])
    events = Events()
    arguments = {
        "request_id": "request-stable",
        "problem_spec": {"statement": "complete raw user input"},
        "wait_seconds": 30,
    }

    async def scenario() -> None:
        proxy = ClientMcpProxy(upstream, event_logger=events)
        await proxy.call_tool("problem_locator_create_case", arguments)
        await proxy.call_tool("problem_locator_create_case", arguments)

    anyio.run(scenario)

    assert upstream.calls == [
        ("problem_locator_create_case", arguments),
        ("problem_locator_create_case", arguments),
    ]
    started = [item for item in events.items if item[0].endswith(".started")]
    completed = [item for item in events.items if item[0].endswith(".completed")]
    assert [item[1]["attempt_number"] for item in started] == [1, 2]
    assert [item[1]["operation_id"] for item in started] == [
        "request-stable",
        "request-stable",
    ]
    assert [item[1]["arguments"] for item in started] == [arguments, arguments]
    assert [item[1]["outcome"] for item in completed] == ["error", "success"]
    assert completed[0][1]["response"]["structuredContent"]["error"] == {
        "code": "VALIDATION_ERROR",
        "details": ["bad field"],
    }
    assert completed[1][1]["response"]["structuredContent"]["data"] == {
        "case_id": "case-1"
    }
    assert started[0][1]["attempt_id"] == completed[0][1]["attempt_id"]
    assert started[1][1]["attempt_id"] == completed[1][1]["attempt_id"]


def test_proxy_settings_default_log_under_claude_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE", raising=False)

    settings = _settings(
        [
            "--url",
            "http://192.168.1.20:8000/mcp",
            "--header",
            "X-Debug: full-value",
        ]
    )

    assert settings.log_file == tmp_path / ".problem-locator/client-dfx.jsonl"
    assert settings.headers == {"X-Debug": "full-value"}
    assert settings.upstream_url == "http://192.168.1.20:8000/mcp"
