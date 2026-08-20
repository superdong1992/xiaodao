from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from problem_locator import __version__
from problem_locator.contracts.commands import ArtifactListResponse, CaseQueryResponse
from problem_locator.contracts.enums import (
    ArtifactKind,
    CaseStatus,
    ErrorCode,
    GenericResultStatus,
    ResourceKind,
)
from problem_locator.contracts.errors import (
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES, MAX_INITIAL_USER_FACTS
from problem_locator.contracts.models import (
    ApplicationError,
    ApplicationErrorDetail,
    ArtifactSummary,
    CaseView,
    GenericResultV2,
)
from problem_locator.contracts.ports import (
    ApplicationCommandPort,
    ApplicationQueryPort,
    StateAdminPort,
)
from problem_locator.interfaces.mcp_server import McpAdapter, create_mcp_transport
from tests.deterministic.unit.interfaces.fakes import FakeApplicationService, FakeQuery, FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import (
    ARTIFACT_ID,
    ATTACHMENT_ID,
    CASE_ID,
    FIXED_TIME,
    JOB_ID,
    application_response,
    artifact_summary,
    case_view,
    problem_spec_input,
    readiness,
)


TOOL_NAMES = [
    "problem_locator_create_case",
    "problem_locator_prepare_attachment",
    "problem_locator_submit_supplement",
    "problem_locator_get_case",
    "problem_locator_resume_case",
    "problem_locator_cancel_case",
    "problem_locator_list_artifacts",
]
REQUEST_IDS = [
    f"10000000-0000-0000-0000-{index:012d}"
    for index in range(1, 8)
]


def _create_case_arguments(
    request_id: str = REQUEST_IDS[0],
    *,
    fact_names: list[str] | None = None,
    fact_values: list[str] | None = None,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "raw_problem_text": "原始问题描述\nrequest-id: 请求-α-7",
        **problem_spec_input(),
        "initial_user_fact_names": fact_names or [],
        "initial_user_fact_values": fact_values or [],
        "wait_seconds": 0,
    }


def _structured(result):
    return result.structuredContent


def _generic_v2_public_view() -> tuple[CaseView, ArtifactSummary]:
    report = "# 通用定位报告\n\n```text\nrequest-id: 订单-α-42\n```\n"
    report_bytes = report.encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    result = GenericResultV2(
        format_version=2,
        status=GenericResultStatus.RESOLVED,
        report_markdown=report,
        report_utf8_size=len(report_bytes),
        report_sha256=report_sha256,
        report_artifact_id=ARTIFACT_ID,
        skill_name="generic-problem-locator-dual-mode",
        source_job_id=JOB_ID,
        source_outcome_id="00000000-0000-0000-0000-000000000005",
        occurred_at=FIXED_TIME,
    )
    artifact = ArtifactSummary(
        artifact_id=ARTIFACT_ID,
        kind=ArtifactKind.GENERIC_REPORT,
        name="generic-diagnosis-report.md",
        content_type="text/markdown",
        resource_kind=ResourceKind.FILE,
        size=len(report_bytes),
        sha256=report_sha256,
        created_by_job_id=JOB_ID,
        created_at=FIXED_TIME,
        downloadable=True,
    )
    payload = case_view().model_dump(mode="python")
    payload.update(
        status=CaseStatus.RESOLVED,
        generic_result=None,
        generic_result_v2=result,
        artifacts=[artifact],
    )
    return CaseView.model_validate(payload), artifact


def test_interface_fakes_conform_to_the_frozen_ports() -> None:
    assert isinstance(FakeApplicationService(), ApplicationCommandPort)
    assert isinstance(FakeQuery(), ApplicationQueryPort)
    assert isinstance(FakeStateAdmin(readiness=readiness()), StateAdminPort)


def test_mcp_get_case_and_list_artifacts_preserve_generic_v2_report_contract() -> None:
    view, artifact = _generic_v2_public_view()
    query = FakeQuery()
    query.queue(
        "get_case",
        CaseQueryResponse(case_view=view, wait_timed_out=False),
    )
    query.queue(
        "list_artifacts",
        ArtifactListResponse(artifacts=[artifact]),
    )
    adapter = McpAdapter(
        FakeApplicationService(),
        query,
        public_base_url="http://127.0.0.1:8000",
    )

    case_result = asyncio.run(
        adapter.call(
            "problem_locator_get_case",
            {"case_id": CASE_ID, "wait_for_job_id": None, "wait_seconds": 0},
        )
    )
    artifact_result = asyncio.run(
        adapter.call(
            "problem_locator_list_artifacts",
            {"case_id": CASE_ID},
        )
    )

    public_result = case_result["data"]["case_view"]["generic_result_v2"]
    assert public_result["report_markdown"] == view.generic_result_v2.report_markdown
    assert public_result["report_utf8_size"] == artifact.size
    assert public_result["report_sha256"] == artifact.sha256
    assert public_result["report_artifact_id"] == artifact.artifact_id
    assert "<<<GENERIC_DIAGNOSIS_RESULT_V2:" not in public_result["report_markdown"]
    public_artifact = artifact_result["data"]["artifacts"][0]
    assert public_artifact["kind"] == ArtifactKind.GENERIC_REPORT.value
    assert public_artifact["size"] == artifact.size
    assert public_artifact["sha256"] == artifact.sha256
    assert "storage_key" not in public_artifact
    assert "metadata" not in public_artifact


def test_fake_application_service_replays_same_request_and_rejects_changed_payload() -> None:
    service = FakeApplicationService(
        [application_response(operation="CreateCase", revision=1)],
        replay_idempotent=True,
    )
    adapter = McpAdapter(
        service,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )
    arguments = _create_case_arguments()

    first = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))
    replay = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))
    conflict = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            {
                **arguments,
                "statement": "A different problem statement.",
            },
        )
    )

    assert first == replay
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(service.calls) == 3


def test_mcp_validation_failure_returns_details_and_logs_full_arguments(caplog) -> None:
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    adapter = McpAdapter(
        FakeApplicationService(),
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )
    arguments = {**_create_case_arguments(), "unexpected": "forbidden"}

    result = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert result["error"]["details"][0]["field"] == "unexpected"
    assert result["error"]["details"][0]["expected"].startswith(
        "extra_forbidden:"
    )
    records = {
        getattr(record, "dfx_event", ""): record
        for record in caplog.records
        if record.name == "problem_locator.dfx"
    }
    assert records["mcp.tool.started"].dfx_fields["arguments"] == arguments
    failure = records["mcp.tool.validation_failed"]
    assert failure.dfx_fields["arguments"] == arguments
    assert failure.dfx_fields["validation_errors"][0]["loc"] == (
        "unexpected",
    )
    assert records["mcp.tool.completed"].dfx_fields["error_code"] == (
        ErrorCode.VALIDATION_ERROR.value
    )


def test_mcp_rejects_removed_composite_problem_fields_before_command_execution() -> None:
    command = FakeApplicationService()
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )
    arguments = {
        **_create_case_arguments(),
        "problem_spec": '{"statement":"encoded instead of nested"}',
        "initial_user_facts": [],
    }

    result = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    fields = {detail["field"] for detail in result["error"]["details"]}
    assert {"problem_spec", "initial_user_facts"}.issubset(fields)
    assert command.calls == []


def test_mcp_rejects_removed_composite_supplement_inputs_before_command_execution() -> None:
    command = FakeApplicationService()
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(
        adapter.call(
            TOOL_NAMES[2],
            {
                "request_id": REQUEST_IDS[0],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "input_names": ["order_id"],
                "input_values": ["order-1"],
                "inputs": {"order_id": "order-1"},
                "attachment_ids": [],
                "wait_seconds": 0,
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert "inputs" in {
        detail["field"] for detail in result["error"]["details"]
    }
    assert command.calls == []


def test_mcp_rebuilds_flat_create_case_inputs_without_data_loss() -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase", revision=1)]
    )
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            _create_case_arguments(
                fact_names=["host", "region"],
                fact_values=["node-1", "华北"],
            ),
        )
    )

    assert result["ok"] is True
    created = command.calls[0]
    assert created.raw_problem_text == "原始问题描述\nrequest-id: 请求-α-7"
    assert created.problem_spec.model_dump(mode="json") == problem_spec_input()
    assert [fact.model_dump(mode="json") for fact in created.initial_user_facts] == [
        {"name": "host", "value": "node-1"},
        {"name": "region", "value": "华北"},
    ]


def test_mcp_raw_problem_text_uses_a_strict_64_kib_utf8_limit() -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase", revision=1)]
    )
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )
    exact = ("界" * 21_845) + "a"
    assert len(exact.encode("utf-8")) == 65_536

    accepted = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            {**_create_case_arguments(), "raw_problem_text": exact},
        )
    )
    rejected = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            {**_create_case_arguments(), "raw_problem_text": "界" * 21_846},
        )
    )

    assert accepted["ok"] is True
    assert command.calls[0].raw_problem_text == exact
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


def test_mcp_accepts_exactly_64_flat_initial_fact_pairs() -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase", revision=1)]
    )
    names = [f"fact_{index}" for index in range(MAX_INITIAL_USER_FACTS)]
    values = [f"value-{index}" for index in range(MAX_INITIAL_USER_FACTS)]

    result = asyncio.run(
        McpAdapter(
            command,
            FakeQuery(),
            public_base_url="http://127.0.0.1:8000",
        ).call(
            TOOL_NAMES[0],
            _create_case_arguments(fact_names=names, fact_values=values),
        )
    )

    assert result["ok"] is True
    assert [fact.name for fact in command.calls[0].initial_user_facts] == names
    assert [fact.value for fact in command.calls[0].initial_user_facts] == values


@pytest.mark.parametrize(
    "arguments",
    [
        _create_case_arguments(fact_names=["host"], fact_values=[]),
        _create_case_arguments(
            fact_names=["host", "host"],
            fact_values=["node-1", "node-2"],
        ),
        _create_case_arguments(
            fact_names=[f"fact_{index}" for index in range(MAX_INITIAL_USER_FACTS + 1)],
            fact_values=["value"] * (MAX_INITIAL_USER_FACTS + 1),
        ),
        {
            **_create_case_arguments(),
            "initial_user_fact_names": '["host"]',
            "initial_user_fact_values": '["node-1"]',
        },
    ],
)
def test_mcp_rejects_invalid_flat_initial_fact_arrays(
    arguments: dict[str, object],
) -> None:
    command = FakeApplicationService()
    result = asyncio.run(
        McpAdapter(
            command,
            FakeQuery(),
            public_base_url="http://127.0.0.1:8000",
        ).call(TOOL_NAMES[0], arguments)
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


def test_mcp_rebuilds_flat_supplement_inputs_without_data_loss() -> None:
    command = FakeApplicationService(
        [application_response(operation="SubmitSupplement", revision=2)]
    )
    arguments = {
        "request_id": REQUEST_IDS[0],
        "case_id": CASE_ID,
        "expected_case_revision": 1,
        "input_names": ["order_id", "region"],
        "input_values": ["order-1", "华北"],
        "attachment_ids": [],
        "wait_seconds": 0,
    }

    result = asyncio.run(
        McpAdapter(
            command,
            FakeQuery(),
            public_base_url="http://127.0.0.1:8000",
        ).call(TOOL_NAMES[2], arguments)
    )

    assert result["ok"] is True
    assert command.calls[0].inputs == {"order_id": "order-1", "region": "华北"}


@pytest.mark.parametrize(
    ("input_names", "input_values", "attachment_ids"),
    [
        (["order_id"], [], []),
        (["order_id", "order_id"], ["one", "two"], []),
        ('["order_id"]', '["order-1"]', []),
        ([], [], []),
    ],
)
def test_mcp_rejects_invalid_flat_supplement_arrays(
    input_names: object,
    input_values: object,
    attachment_ids: list[str],
) -> None:
    command = FakeApplicationService()
    result = asyncio.run(
        McpAdapter(
            command,
            FakeQuery(),
            public_base_url="http://127.0.0.1:8000",
        ).call(
            TOOL_NAMES[2],
            {
                "request_id": REQUEST_IDS[0],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "input_names": input_names,
                "input_values": input_values,
                "attachment_ids": attachment_ids,
                "wait_seconds": 0,
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


def test_mcp_rejects_legacy_attachment_field_aliases_before_command_execution() -> None:
    command = FakeApplicationService()
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(
        adapter.call(
            TOOL_NAMES[1],
            {
                "request_id": REQUEST_IDS[0],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "attachment_name": "logs.zip",
                "content_type": "application/zip",
                "declared_byte_count": 10,
                "declared_sha256": None,
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    fields = {detail["field"] for detail in result["error"]["details"]}
    assert {"name", "attachment_name", "declared_byte_count"}.issubset(fields)
    assert command.calls == []


@pytest.mark.parametrize(
    "code",
    sorted(
        PORT_ERROR_CODES["ApplicationCommandPort.execute"],
        key=lambda item: item.value,
    ),
)
def test_mcp_preserves_every_frozen_application_command_error(code: ErrorCode) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {code.value} failure.",
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )
    adapter = McpAdapter(
        FakeApplicationService([ApplicationPortError(error)]),
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            _create_case_arguments(),
        )
    )

    assert result == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("logs.ZIP", "application/zip"),
        ("logs.zip", "application/gzip"),
    ],
)
def test_mcp_prepare_rejects_archive_suffix_drift_before_port_call(
    name: str,
    content_type: str,
) -> None:
    command = FakeApplicationService()
    adapter = McpAdapter(
        command,
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(
        adapter.call(
            TOOL_NAMES[1],
            {
                "request_id": REQUEST_IDS[0],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "name": name,
                "content_type": content_type,
                "declared_size": None,
                "declared_sha256": None,
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


@pytest.mark.parametrize(
    ("method_key", "method_name", "tool_name", "arguments", "code"),
    [
        (
            method_key,
            method_name,
            tool_name,
            arguments,
            code,
        )
        for method_key, method_name, tool_name, arguments in (
            (
                "ApplicationQueryPort.get_case",
                "get_case",
                "problem_locator_get_case",
                {
                    "case_id": CASE_ID,
                    "wait_for_job_id": None,
                    "wait_seconds": 0,
                },
            ),
            (
                "ApplicationQueryPort.list_artifacts",
                "list_artifacts",
                "problem_locator_list_artifacts",
                {"case_id": CASE_ID},
            ),
        )
        for code in sorted(PORT_ERROR_CODES[method_key], key=lambda item: item.value)
    ],
)
def test_mcp_preserves_every_frozen_application_query_error(
    method_key: str,
    method_name: str,
    tool_name: str,
    arguments: dict[str, object],
    code: ErrorCode,
) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {method_key} {code.value} failure.",
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )
    query = FakeQuery()
    query.queue(method_name, ApplicationPortError(error))
    adapter = McpAdapter(
        FakeApplicationService(),
        query,
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(adapter.call(tool_name, arguments))

    assert result == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }
    assert query.calls[0][0] == method_name


@pytest.mark.parametrize(
    ("tool_name", "arguments", "operation", "primary_resource_id"),
    [
        (
            "problem_locator_create_case",
            _create_case_arguments(),
            "CreateCase",
            CASE_ID,
        ),
        (
            "problem_locator_prepare_attachment",
            {
                "request_id": REQUEST_IDS[1],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
                "declared_size": None,
                "declared_sha256": None,
            },
            "PrepareAttachment",
            ATTACHMENT_ID,
        ),
        (
            "problem_locator_submit_supplement",
            {
                "request_id": REQUEST_IDS[2],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "input_names": ["order_id"],
                "input_values": ["order-1"],
                "attachment_ids": [],
                "wait_seconds": 0,
            },
            "SubmitSupplement",
            CASE_ID,
        ),
        (
            "problem_locator_resume_case",
            {
                "request_id": REQUEST_IDS[3],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "wait_seconds": 0,
            },
            "ResumeCase",
            CASE_ID,
        ),
        (
            "problem_locator_cancel_case",
            {
                "request_id": REQUEST_IDS[4],
                "case_id": CASE_ID,
                "expected_case_revision": 1,
            },
            "CancelCase",
            CASE_ID,
        ),
    ],
)
def test_mcp_serializes_r3_postcommit_success_without_case_view(
    tool_name: str,
    arguments: dict[str, object],
    operation: str,
    primary_resource_id: str,
) -> None:
    response = application_response(
        operation=operation,
        primary_resource_id=primary_resource_id,
        revision=2,
        with_case_view=False,
    )
    adapter = McpAdapter(
        FakeApplicationService([response]),
        FakeQuery(),
        public_base_url="http://127.0.0.1:8000",
    )

    result = asyncio.run(adapter.call(tool_name, arguments))

    data = result["data"]
    if tool_name == "problem_locator_prepare_attachment":
        data = data["application_response"]
    assert result["ok"] is True
    assert data["case_view"] is None
    assert data["business_receipt"]["case_revision"] == 2


def test_official_sdk_calls_all_seven_stateless_tools(caplog) -> None:
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    resource_limit_error = ApplicationError(
        code=ErrorCode.RESOURCE_LIMIT_EXCEEDED,
        message="Attachment size exceeds the V1 limit.",
        details=[
            ApplicationErrorDetail(
                field="declared_size",
                resource_type=None,
                resource_id=None,
                resource_ref=None,
                expected=None,
                actual=None,
                limit=MAX_ATTACHMENT_BYTES,
                observed=MAX_ATTACHMENT_BYTES + 1,
            )
        ],
        retryable=False,
    )
    command = FakeApplicationService(
        [
            application_response(operation="CreateCase", revision=1),
            application_response(
                operation="PrepareAttachment",
                primary_resource_id=ATTACHMENT_ID,
                revision=2,
            ),
            application_response(operation="SubmitSupplement", revision=3),
            application_response(operation="ResumeCase", revision=4),
            application_response(
                operation="CancelCase",
                revision=5,
                with_case_view=False,
            ),
            application_response(
                operation="PrepareAttachment",
                primary_resource_id=ATTACHMENT_ID,
                revision=6,
            ),
            ApplicationPortError(resource_limit_error),
        ]
    )
    query = FakeQuery()
    query.queue(
        "get_case",
        CaseQueryResponse(case_view=case_view(revision=3), wait_timed_out=True),
    )
    query.queue(
        "list_artifacts",
        ArtifactListResponse(artifacts=[artifact_summary()]),
    )
    transport = create_mcp_transport(
        command,
        query,
        public_base_url="http://127.0.0.1:8000/service",
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with transport.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route(
                "/mcp",
                endpoint=transport.asgi_application,
                methods=["GET", "POST", "DELETE"],
            )
        ],
        lifespan=lifespan,
    )

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as http_client:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, get_session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.version == __version__
                        listed = await session.list_tools()
                        assert [tool.name for tool in listed.tools] == TOOL_NAMES
                        expected_inputs = {
                            TOOL_NAMES[0]: (
                                {
                                    "request_id",
                                    "raw_problem_text",
                                    "statement",
                                    "expected_behavior",
                                    "actual_behavior",
                                    "scope",
                                    "goals",
                                    "non_goals",
                                    "constraints",
                                    "completion_criteria",
                                    "initial_user_fact_names",
                                    "initial_user_fact_values",
                                    "wait_seconds",
                                },
                                {
                                    "request_id",
                                    "raw_problem_text",
                                    "statement",
                                    "expected_behavior",
                                    "actual_behavior",
                                    "scope",
                                    "goals",
                                    "non_goals",
                                    "constraints",
                                    "completion_criteria",
                                },
                            ),
                            TOOL_NAMES[1]: (
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
                            TOOL_NAMES[2]: (
                                {
                                    "request_id",
                                    "case_id",
                                    "expected_case_revision",
                                    "input_names",
                                    "input_values",
                                    "attachment_ids",
                                    "wait_seconds",
                                },
                                {
                                    "request_id",
                                    "case_id",
                                    "expected_case_revision",
                                    "input_names",
                                    "input_values",
                                    "attachment_ids",
                                },
                            ),
                            TOOL_NAMES[3]: (
                                {"case_id", "wait_for_job_id", "wait_seconds"},
                                {"case_id"},
                            ),
                            TOOL_NAMES[4]: (
                                {
                                    "request_id",
                                    "case_id",
                                    "expected_case_revision",
                                    "wait_seconds",
                                },
                                {"request_id", "case_id", "expected_case_revision"},
                            ),
                            TOOL_NAMES[5]: (
                                {"request_id", "case_id", "expected_case_revision"},
                                {"request_id", "case_id", "expected_case_revision"},
                            ),
                            TOOL_NAMES[6]: ({"case_id"}, {"case_id"}),
                        }
                        input_validators: dict[str, Draft202012Validator] = {}
                        for tool in listed.tools:
                            schema = tool.inputSchema
                            Draft202012Validator.check_schema(schema)
                            assert set(schema["properties"]) == expected_inputs[tool.name][0]
                            assert set(schema["required"]) == expected_inputs[tool.name][1]
                            input_validators[tool.name] = Draft202012Validator(schema)
                        assert all(
                            tool.inputSchema.get("additionalProperties") is False
                            for tool in listed.tools
                        )
                        create_schema = next(
                            tool.inputSchema
                            for tool in listed.tools
                            if tool.name == TOOL_NAMES[0]
                        )
                        assert create_schema["properties"][
                            "initial_user_fact_names"
                        ]["uniqueItems"] is True
                        assert create_schema["properties"][
                            "initial_user_fact_names"
                        ]["maxItems"] == MAX_INITIAL_USER_FACTS
                        assert create_schema["properties"][
                            "initial_user_fact_values"
                        ]["maxItems"] == MAX_INITIAL_USER_FACTS
                        submit_schema = next(
                            tool.inputSchema
                            for tool in listed.tools
                            if tool.name == TOOL_NAMES[2]
                        )
                        assert submit_schema["properties"]["input_names"][
                            "uniqueItems"
                        ] is True
                        create_contract = _create_case_arguments()
                        assert input_validators[TOOL_NAMES[0]].is_valid(create_contract)
                        assert not input_validators[TOOL_NAMES[0]].is_valid(
                            {
                                **create_contract,
                                "problem_spec": json.dumps(problem_spec_input()),
                            }
                        )
                        assert not input_validators[TOOL_NAMES[0]].is_valid(
                            {
                                **create_contract,
                                "initial_user_fact_names": ["host", "host"],
                                "initial_user_fact_values": ["one", "two"],
                            }
                        )
                        submit_contract = {
                            "request_id": REQUEST_IDS[0],
                            "case_id": CASE_ID,
                            "expected_case_revision": 1,
                            "input_names": ["order_id"],
                            "input_values": ["order-1"],
                            "attachment_ids": [],
                        }
                        assert input_validators[TOOL_NAMES[2]].is_valid(submit_contract)
                        assert not input_validators[TOOL_NAMES[2]].is_valid(
                            {
                                **submit_contract,
                                "input_names": ["order_id", "order_id"],
                                "input_values": ["one", "two"],
                            }
                        )
                        assert not input_validators[TOOL_NAMES[2]].is_valid(
                            {
                                **submit_contract,
                                "inputs": {"order_id": "order-1"},
                            }
                        )
                        assert not input_validators[TOOL_NAMES[1]].is_valid(
                            {
                                "request_id": REQUEST_IDS[0],
                                "case_id": CASE_ID,
                                "expected_case_revision": 1,
                                "attachment_name": "logs.zip",
                                "content_type": "application/zip",
                                "declared_byte_count": 10,
                            }
                        )
                        output_validators: dict[str, Draft202012Validator] = {}
                        for tool in listed.tools:
                            schema = tool.outputSchema
                            assert schema is not None
                            assert schema.get("type") == "object"
                            assert "$defs" in schema
                            assert len(schema.get("anyOf", [])) == 2
                            Draft202012Validator.check_schema(schema)
                            validator = Draft202012Validator(schema)
                            assert validator.is_valid([]) is False
                            assert validator.is_valid("invalid") is False
                            assert validator.is_valid(
                                {"ok": False, "data": None}
                            ) is False
                            assert validator.is_valid(
                                {"ok": False, "data": None, "error": None}
                            ) is False
                            output_validators[tool.name] = validator
                        assert get_session_id() is None

                        create = await session.call_tool(
                            TOOL_NAMES[0],
                            _create_case_arguments(),
                        )
                        output_validators[TOOL_NAMES[0]].validate(_structured(create))
                        assert _structured(create)["data"]["case_view"]["case_id"] == CASE_ID

                        prepare = await session.call_tool(
                            TOOL_NAMES[1],
                            {
                                "request_id": REQUEST_IDS[1],
                                "case_id": CASE_ID,
                                "expected_case_revision": 1,
                                "name": "logs.zip",
                                "content_type": "application/zip",
                                "declared_size": None,
                                "declared_sha256": None,
                            },
                        )
                        output_validators[TOOL_NAMES[1]].validate(_structured(prepare))
                        upload = _structured(prepare)["data"]["upload"]
                        assert upload["attachment_id"] == ATTACHMENT_ID
                        assert upload["required_headers"] == {
                            "Idempotency-Key": ATTACHMENT_ID,
                            "Content-Type": "application/zip",
                            "Content-Length": None,
                            "X-Content-SHA256": None,
                        }

                        submit = await session.call_tool(
                            TOOL_NAMES[2],
                            {
                                "request_id": REQUEST_IDS[2],
                                "case_id": CASE_ID,
                                "expected_case_revision": 2,
                                "input_names": ["order_id"],
                                "input_values": ["order-1"],
                                "attachment_ids": [],
                                "wait_seconds": 30,
                            },
                        )
                        output_validators[TOOL_NAMES[2]].validate(_structured(submit))
                        assert _structured(submit)["ok"] is True

                        get_case = await session.call_tool(
                            TOOL_NAMES[3],
                            {"case_id": CASE_ID, "wait_for_job_id": None, "wait_seconds": 0},
                        )
                        output_validators[TOOL_NAMES[3]].validate(_structured(get_case))
                        assert _structured(get_case)["data"]["case_view"]["case_revision"] == 3
                        assert _structured(get_case)["data"]["wait_timed_out"] is True

                        resume = await session.call_tool(
                            TOOL_NAMES[4],
                            {
                                "request_id": REQUEST_IDS[3],
                                "case_id": CASE_ID,
                                "expected_case_revision": 3,
                                "wait_seconds": 0,
                            },
                        )
                        output_validators[TOOL_NAMES[4]].validate(_structured(resume))
                        assert _structured(resume)["ok"] is True

                        cancel = await session.call_tool(
                            TOOL_NAMES[5],
                            {
                                "request_id": REQUEST_IDS[4],
                                "case_id": CASE_ID,
                                "expected_case_revision": 4,
                            },
                        )
                        output_validators[TOOL_NAMES[5]].validate(_structured(cancel))
                        assert _structured(cancel)["ok"] is True
                        assert _structured(cancel)["data"]["case_view"] is None

                        artifacts = await session.call_tool(
                            TOOL_NAMES[6],
                            {"case_id": CASE_ID},
                        )
                        output_validators[TOOL_NAMES[6]].validate(_structured(artifacts))
                        public_artifact = _structured(artifacts)["data"]["artifacts"][0]
                        assert public_artifact["artifact_id"] == ARTIFACT_ID
                        assert "storage_key" not in public_artifact
                        assert public_artifact["download_url"].endswith(
                            f"/api/v1/artifacts/{ARTIFACT_ID}/content?case_id={CASE_ID}"
                        )

                        at_limit = await session.call_tool(
                            TOOL_NAMES[1],
                            {
                                "request_id": REQUEST_IDS[5],
                                "case_id": CASE_ID,
                                "expected_case_revision": 5,
                                "name": "large.zip",
                                "content_type": "application/zip",
                                "declared_size": MAX_ATTACHMENT_BYTES,
                                "declared_sha256": None,
                            },
                        )
                        output_validators[TOOL_NAMES[1]].validate(_structured(at_limit))
                        assert _structured(at_limit)["data"]["upload"][
                            "required_headers"
                        ]["Content-Length"] == str(MAX_ATTACHMENT_BYTES)

                        above_limit = await session.call_tool(
                            TOOL_NAMES[1],
                            {
                                "request_id": REQUEST_IDS[6],
                                "case_id": CASE_ID,
                                "expected_case_revision": 6,
                                "name": "too-large.zip",
                                "content_type": "application/zip",
                                "declared_size": MAX_ATTACHMENT_BYTES + 1,
                                "declared_sha256": None,
                            },
                        )
                        output_validators[TOOL_NAMES[1]].validate(_structured(above_limit))
                        assert _structured(above_limit) == {
                            "ok": False,
                            "data": None,
                            "error": resource_limit_error.model_dump(mode="json"),
                        }

                        invalid = await session.call_tool(
                            TOOL_NAMES[0],
                            {**_create_case_arguments(), "unexpected": "forbidden"},
                        )
                        assert invalid.isError is False
                        invalid_body = _structured(invalid)
                        output_validators[TOOL_NAMES[0]].validate(invalid_body)
                        assert invalid_body["ok"] is False
                        assert invalid_body["error"]["code"] == ErrorCode.VALIDATION_ERROR.value

    asyncio.run(scenario())

    assert [type(item).__name__ for item in command.calls] == [
        "CreateCase",
        "PrepareAttachment",
        "SubmitSupplement",
        "ResumeCase",
        "CancelCase",
        "PrepareAttachment",
        "PrepareAttachment",
    ]
    assert [item.idempotency_key for item in command.calls] == REQUEST_IDS
    assert query.calls == [
        ("get_case", (CASE_ID, None, 0)),
        ("list_artifacts", (CASE_ID, False)),
    ]
    listed_record = next(
        record
        for record in caplog.records
        if getattr(record, "dfx_event", "") == "mcp.tools.listed"
    )
    assert listed_record.dfx_fields["server_version"] == __version__
    advertised = listed_record.dfx_fields["tools"]
    assert [tool["name"] for tool in advertised] == TOOL_NAMES
    assert all(len(tool["input_schema_sha256"]) == 64 for tool in advertised)
    create_schema = advertised[0]["input_schema"]
    assert "$defs" not in create_schema
    assert create_schema["properties"]["statement"]["type"] == "string"
    assert create_schema["properties"]["goals"]["items"]["type"] == "string"
