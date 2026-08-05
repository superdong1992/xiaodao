from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp import types
import pytest

from problem_locator.interfaces.client_proxy import (
    CLIENT_PROXY_VERSION,
    SUPPORTED_TOOLS,
    ClientMcpProxy,
    ClientProxySettings,
    _permissive_input_schema,
    _settings,
    _strict_input_schema,
    _upstream_http_client,
)
from problem_locator.interfaces.mcp_server import (
    CancelCaseRequest,
    CreateCaseRequest,
    GetCaseRequest,
    ListArtifactsRequest,
    PrepareAttachmentRequest,
    ResumeCaseRequest,
    SubmitSupplementRequest,
)
from tests.unit.interfaces.helpers import problem_spec_input


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


def test_proxy_advertises_strict_schema_and_logs_schema_provenance() -> None:
    async def scenario() -> tuple[list[types.Tool], Events]:
        events = Events()
        proxy = ClientMcpProxy(FakeUpstream(), event_logger=events)
        return await proxy.list_tools(), events

    advertised, events = anyio.run(scenario)

    assert [tool.name for tool in advertised] == list(SUPPORTED_TOOLS)
    for tool in advertised:
        validator = Draft202012Validator(tool.inputSchema)
        assert validator.is_valid({"request_id": "request-1"})
        assert validator.is_valid(
            {
                "request_id": ["the client must reject this value"],
                "wait_seconds": "also intentionally wrong",
                "unexpected": {"nested": True},
            }
        ) is False
        assert tool.outputSchema is None
        request_description = tool.inputSchema["properties"]["request_id"][
            "description"
        ]
        assert request_description.startswith("Stable logical request ID.")
        assert "Required." in request_description
        assert "Expected JSON shape: string" in request_description
        assert tool.inputSchema["properties"]["request_id"]["type"] == "string"
        assert tool.inputSchema["required"] == ["request_id"]
        assert tool.inputSchema["additionalProperties"] is False

    event, fields = events.items[0]
    assert event == "client.proxy.tools.discovered"
    assert fields["missing_tools"] == []
    original = fields["upstream_tools"][0]["inputSchema"]
    assert original["required"] == ["request_id"]
    assert original["properties"]["wait_seconds"]["type"] == "integer"
    assert fields["advertised_tools"][0]["inputSchema"] == advertised[0].inputSchema
    assert fields["schema_mode"] == "strict"
    assert fields["client_proxy_version"] == CLIENT_PROXY_VERSION
    assert fields["package_version"] == "1.0.1"
    assert set(fields["advertised_schema_sha256"]) == set(SUPPORTED_TOOLS)
    assert all(
        len(value) == 64
        for value in fields["advertised_schema_sha256"].values()
    )


def test_diagnostic_mode_keeps_malformed_arguments_visible() -> None:
    async def scenario() -> list[types.Tool]:
        proxy = ClientMcpProxy(FakeUpstream(), schema_mode="diagnostic")
        return await proxy.list_tools()

    advertised = anyio.run(scenario)
    for tool in advertised:
        Draft202012Validator(tool.inputSchema).validate(
            {
                "request_id": ["the proxy must not reject this value"],
                "wait_seconds": "also intentionally wrong",
                "unexpected": {"nested": True},
            }
        )


def test_all_problem_locator_call_shapes_are_strict_and_diagnostic() -> None:
    request_types = {
        "problem_locator_create_case": CreateCaseRequest,
        "problem_locator_prepare_attachment": PrepareAttachmentRequest,
        "problem_locator_submit_supplement": SubmitSupplementRequest,
        "problem_locator_get_case": GetCaseRequest,
        "problem_locator_resume_case": ResumeCaseRequest,
        "problem_locator_cancel_case": CancelCaseRequest,
        "problem_locator_list_artifacts": ListArtifactsRequest,
    }
    expected_contract = {
        "problem_locator_create_case": (
            {"request_id", "problem_spec", "initial_user_facts", "wait_seconds"},
            {"request_id", "problem_spec"},
        ),
        "problem_locator_prepare_attachment": (
            {
                "request_id",
                "case_id",
                "expected_case_revision",
                "name",
                "content_type",
                "declared_size",
                "declared_sha256",
            },
            {
                "request_id",
                "case_id",
                "expected_case_revision",
                "name",
                "content_type",
            },
        ),
        "problem_locator_submit_supplement": (
            {
                "request_id",
                "case_id",
                "expected_case_revision",
                "inputs",
                "attachment_ids",
                "wait_seconds",
            },
            {
                "request_id",
                "case_id",
                "expected_case_revision",
                "inputs",
                "attachment_ids",
            },
        ),
        "problem_locator_get_case": (
            {"case_id", "wait_for_job_id", "wait_seconds"},
            {"case_id"},
        ),
        "problem_locator_resume_case": (
            {"request_id", "case_id", "expected_case_revision", "wait_seconds"},
            {"request_id", "case_id", "expected_case_revision"},
        ),
        "problem_locator_cancel_case": (
            {"request_id", "case_id", "expected_case_revision"},
            {"request_id", "case_id", "expected_case_revision"},
        ),
        "problem_locator_list_artifacts": ({"case_id"}, {"case_id"}),
    }
    authoritative = {
        name: request_type.model_json_schema(mode="validation")
        for name, request_type in request_types.items()
    }
    diagnostic = {
        name: _permissive_input_schema(schema)
        for name, schema in authoritative.items()
    }
    strict = {
        name: _strict_input_schema(schema)
        for name, schema in authoritative.items()
    }
    for name, schema in diagnostic.items():
        expected_properties, expected_required = expected_contract[name]
        assert set(authoritative[name]["properties"]) == expected_properties
        assert set(authoritative[name].get("required", [])) == expected_required
        assert set(schema["properties"]) == expected_properties
        assert schema["additionalProperties"] is True
        assert "required" not in schema
        for property_schema in schema["properties"].values():
            assert set(property_schema) == {"description"}
            assert "Expected JSON shape:" in property_schema["description"]

        # The proxy remains validation-free so malformed calls still cross
        # the client DFX boundary and reach the authoritative server.
        Draft202012Validator(schema).validate(
            {"unexpected": {"nested": True}}
        )

        strict_schema = strict[name]
        assert strict_schema["required"] == authoritative[name]["required"]
        assert strict_schema["additionalProperties"] is False
        assert set(strict_schema["properties"]) == expected_properties
        for property_name, property_schema in strict_schema["properties"].items():
            assert property_schema.keys() >= authoritative[name]["properties"][
                property_name
            ].keys()
            assert "Expected JSON shape:" in property_schema["description"]
        restored = copy.deepcopy(strict_schema)
        for property_name, original_property in authoritative[name][
            "properties"
        ].items():
            if "description" in original_property:
                restored["properties"][property_name]["description"] = (
                    original_property["description"]
                )
            else:
                restored["properties"][property_name].pop("description")
        assert restored == authoritative[name]
        Draft202012Validator.check_schema(strict_schema)

    authoritative_prepare = authoritative["problem_locator_prepare_attachment"]
    authoritative_supplement = authoritative[
        "problem_locator_submit_supplement"
    ]
    assert "name" in authoritative_prepare["properties"]
    assert "declared_size" in authoritative_prepare["properties"]
    assert "attachment_name" not in authoritative_prepare["properties"]
    assert "declared_byte_count" not in authoritative_prepare["properties"]
    assert authoritative_supplement["properties"]["inputs"]["type"] == "object"
    assert authoritative_supplement["properties"]["inputs"][
        "additionalProperties"
    ]["type"] == "string"

    create = diagnostic["problem_locator_create_case"]
    prepare = diagnostic["problem_locator_prepare_attachment"]
    supplement = diagnostic["problem_locator_submit_supplement"]
    get_case = diagnostic["problem_locator_get_case"]

    problem_spec_description = create["properties"]["problem_spec"]["description"]
    assert "directly as a JSON object" in problem_spec_description
    assert "do not pass a JSON-encoded string" in problem_spec_description
    for member in (
        "statement",
        "expected_behavior",
        "actual_behavior",
        "scope",
        "goals",
        "non_goals",
        "constraints",
        "completion_criteria",
    ):
        assert f"{member}:" in problem_spec_description
    assert "goals: array<string" in problem_spec_description
    assert "minItems=1" in problem_spec_description

    facts_description = create["properties"]["initial_user_facts"]["description"]
    assert "array<object{name: string" in facts_description
    assert "value: string" in facts_description
    assert "Pass directly as a JSON array" in facts_description
    assert "defaulting to an empty array" in facts_description

    assert "`name`" in prepare["properties"]["name"]["description"]
    assert "`attachment_name`" in prepare["properties"]["name"]["description"]
    assert "`declared_size`" in prepare["properties"]["declared_size"]["description"]
    assert (
        "`declared_byte_count`"
        in prepare["properties"]["declared_size"]["description"]
    )
    assert "object" in supplement["properties"]["inputs"]["description"]
    assert (
        "object<string, string>"
        in supplement["properties"]["inputs"]["description"]
    )
    assert "never a list" in supplement["properties"]["inputs"]["description"]
    assert (
        "array<string"
        in supplement["properties"]["attachment_ids"]["description"]
    )
    assert "Default: null" in prepare["properties"]["declared_size"]["description"]
    assert "| null" in prepare["properties"]["declared_size"]["description"]
    assert "Default: null" in get_case["properties"]["wait_for_job_id"]["description"]
    assert "| null" in get_case["properties"]["wait_for_job_id"]["description"]
    for name in (
        "problem_locator_create_case",
        "problem_locator_submit_supplement",
        "problem_locator_get_case",
        "problem_locator_resume_case",
    ):
        assert "Default: 0" in diagnostic[name]["properties"]["wait_seconds"][
            "description"
        ]

    Draft202012Validator(supplement).validate({"inputs": ["wrong shape"]})
    strict_create = Draft202012Validator(strict["problem_locator_create_case"])
    assert strict_create.is_valid(
        {
            "request_id": "10000000-0000-0000-0000-000000000001",
            "problem_spec": problem_spec_input(),
        }
    )
    assert strict_create.is_valid(
        {
            "request_id": "10000000-0000-0000-0000-000000000001",
            "problem_spec": '{"statement":"encoded string"}',
        }
    ) is False
    strict_supplement = Draft202012Validator(
        strict["problem_locator_submit_supplement"]
    )
    assert strict_supplement.is_valid(
        {
            "request_id": "10000000-0000-0000-0000-000000000002",
            "case_id": "00000000-0000-0000-0000-000000000001",
            "expected_case_revision": 1,
            "inputs": {"name": "value"},
            "attachment_ids": [],
        }
    )
    assert strict_supplement.is_valid(
        {
            "request_id": "10000000-0000-0000-0000-000000000002",
            "case_id": "00000000-0000-0000-0000-000000000001",
            "expected_case_revision": 1,
            "inputs": ["wrong shape"],
            "attachment_ids": [],
        }
    ) is False


def test_permissive_shape_hint_fails_open_for_recursive_or_unknown_schema() -> None:
    recursive = {
        "type": "object",
        "properties": {"payload": {"$ref": "#/$defs/Node"}},
        "required": ["payload"],
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    advertised = _permissive_input_schema(recursive)
    description = advertised["properties"]["payload"]["description"]

    assert "recursive schema" in description
    assert len(description) <= 2048
    Draft202012Validator(advertised).validate(
        {"payload": "still intentionally accepted", "extra": True}
    )

    broken = _permissive_input_schema(
        {
            "type": "object",
            "properties": {"payload": {"$ref": "#/missing"}},
        }
    )
    assert "Expected JSON shape: JSON value" in broken["properties"]["payload"][
        "description"
    ]


def test_proxy_preserves_json_encoded_problem_spec_for_authoritative_rejection() -> None:
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="VALIDATION_ERROR")],
        structuredContent={
            "ok": False,
            "data": None,
            "error": {"code": "VALIDATION_ERROR"},
        },
        isError=False,
    )
    upstream = FakeUpstream([result])
    events = Events()
    arguments = {
        "request_id": "request-stable",
        "problem_spec": '{"statement":"still a string"}',
    }

    async def scenario() -> None:
        await ClientMcpProxy(upstream, event_logger=events).call_tool(
            "problem_locator_create_case",
            arguments,
        )

    anyio.run(scenario)

    assert upstream.calls == [("problem_locator_create_case", arguments)]
    started = next(item for item in events.items if item[0].endswith(".started"))
    assert started[1]["arguments"] == arguments
    assert started[1]["argument_json_types"] == {
        "request_id": "string",
        "problem_spec": "string",
    }
    assert started[1]["schema_mode"] == "strict"


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
    assert [item[1]["argument_json_types"] for item in started] == [
        {
            "request_id": "string",
            "problem_spec": "object",
            "wait_seconds": "integer",
        },
        {
            "request_id": "string",
            "problem_spec": "object",
            "wait_seconds": "integer",
        },
    ]
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
    assert settings.schema_mode == "strict"


def test_proxy_settings_allow_explicit_diagnostic_schema_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROBLEM_LOCATOR_CLIENT_SCHEMA_MODE", "diagnostic")

    settings = _settings(["--url", "http://192.168.1.20:8000/mcp"])

    assert settings.schema_mode == "diagnostic"


def test_proxy_version_is_available_without_upstream_configuration(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        _settings(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == (
        f"problem-locator-client-proxy {CLIENT_PROXY_VERSION}"
    )


def test_upstream_http_client_disables_ambient_proxy_inheritance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_async_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "problem_locator.interfaces.client_proxy.httpx.AsyncClient",
        fake_async_client,
    )
    settings = ClientProxySettings(
        upstream_url="http://192.168.1.20:8000/mcp",
        log_file=tmp_path / "client.jsonl",
        log_level="INFO",
        headers={"X-Debug": "full-value"},
        timeout_seconds=17.0,
        schema_mode="strict",
    )

    actual = _upstream_http_client(settings)

    assert actual is sentinel
    assert captured["headers"] == {"X-Debug": "full-value"}
    assert captured["follow_redirects"] is True
    assert captured["trust_env"] is False
    timeout = captured["timeout"]
    assert timeout.connect == 17.0
    assert timeout.read == 17.0
