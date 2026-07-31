from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from problem_locator.contracts.commands import ArtifactListResponse, CaseQueryResponse
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import ApplicationError, ApplicationErrorDetail
from problem_locator.contracts.ports import (
    ApplicationCommandPort,
    ApplicationQueryPort,
    StateAdminPort,
)
from problem_locator.interfaces.mcp_server import McpAdapter, create_mcp_transport
from tests.unit.interfaces.fakes import FakeApplicationService, FakeQuery, FakeStateAdmin
from tests.unit.interfaces.helpers import (
    ARTIFACT_ID,
    ATTACHMENT_ID,
    CASE_ID,
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


def _structured(result):
    return result.structuredContent


def test_interface_fakes_conform_to_the_frozen_ports() -> None:
    assert isinstance(FakeApplicationService(), ApplicationCommandPort)
    assert isinstance(FakeQuery(), ApplicationQueryPort)
    assert isinstance(FakeStateAdmin(readiness=readiness()), StateAdminPort)


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
    arguments = {
        "request_id": REQUEST_IDS[0],
        "problem_spec": problem_spec_input(),
        "initial_user_facts": [],
        "wait_seconds": 0,
    }

    first = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))
    replay = asyncio.run(adapter.call(TOOL_NAMES[0], arguments))
    conflict = asyncio.run(
        adapter.call(
            TOOL_NAMES[0],
            {
                **arguments,
                "problem_spec": {
                    **problem_spec_input(),
                    "statement": "A different problem statement.",
                },
            },
        )
    )

    assert first == replay
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(service.calls) == 3


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
            {
                "request_id": REQUEST_IDS[0],
                "problem_spec": problem_spec_input(),
                "initial_user_facts": [],
                "wait_seconds": 0,
            },
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
            {
                "request_id": REQUEST_IDS[0],
                "problem_spec": problem_spec_input(),
                "initial_user_facts": [],
                "wait_seconds": 0,
            },
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
                "inputs": {"order_id": "order-1"},
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


def test_official_sdk_calls_all_seven_stateless_tools() -> None:
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
                        await session.initialize()
                        listed = await session.list_tools()
                        assert [tool.name for tool in listed.tools] == TOOL_NAMES
                        assert all(
                            tool.inputSchema.get("additionalProperties") is False
                            for tool in listed.tools
                        )
                        assert get_session_id() is None

                        create = await session.call_tool(
                            TOOL_NAMES[0],
                            {
                                "request_id": REQUEST_IDS[0],
                                "problem_spec": problem_spec_input(),
                                "initial_user_facts": [],
                                "wait_seconds": 0,
                            },
                        )
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
                                "inputs": {"order_id": "order-1"},
                                "attachment_ids": [],
                                "wait_seconds": 30,
                            },
                        )
                        assert _structured(submit)["ok"] is True

                        get_case = await session.call_tool(
                            TOOL_NAMES[3],
                            {"case_id": CASE_ID, "wait_for_job_id": None, "wait_seconds": 0},
                        )
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
                        assert _structured(resume)["ok"] is True

                        cancel = await session.call_tool(
                            TOOL_NAMES[5],
                            {
                                "request_id": REQUEST_IDS[4],
                                "case_id": CASE_ID,
                                "expected_case_revision": 4,
                            },
                        )
                        assert _structured(cancel)["ok"] is True
                        assert _structured(cancel)["data"]["case_view"] is None

                        artifacts = await session.call_tool(
                            TOOL_NAMES[6],
                            {"case_id": CASE_ID},
                        )
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
                        assert _structured(above_limit) == {
                            "ok": False,
                            "data": None,
                            "error": resource_limit_error.model_dump(mode="json"),
                        }

                        invalid = await session.call_tool(
                            TOOL_NAMES[0],
                            {
                                "request_id": REQUEST_IDS[0],
                                "problem_spec": problem_spec_input(),
                                "initial_user_facts": [],
                                "wait_seconds": 0,
                                "unexpected": "forbidden",
                            },
                        )
                        assert invalid.isError is False
                        invalid_body = _structured(invalid)
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
