from __future__ import annotations

import asyncio
import hashlib
import threading

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.requests import ClientDisconnect

from problem_locator.contracts.commands import OpenArtifactResult, UploadAttachmentContent
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.errors import (
    ERROR_SPECS,
    PORT_ERROR_CODES,
    ApplicationPortError,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import ApplicationError, ReadinessReport
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.http_streaming import HTTP_STREAM_CHUNK_BYTES
from tests.contracts.fakes import InMemoryBinaryStream
from tests.unit.interfaces.fakes import (
    FakeApplicationService,
    FakeQuery,
    FakeStateAdmin,
    StreamingUploadFixture,
)
from tests.unit.interfaces.helpers import (
    ARTIFACT_ID,
    ATTACHMENT_ID,
    CASE_ID,
    SHA256_A,
    application_response,
    artifact_summary,
    readiness,
)


REQUEST_ID = "10000000-0000-0000-0000-000000000001"


def _run(app, operation):
    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client:
                return await operation(client)

    return asyncio.run(scenario())


def _artifact_scope() -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/v1/artifacts/{ARTIFACT_ID}/content",
        "raw_path": f"/api/v1/artifacts/{ARTIFACT_ID}/content".encode("ascii"),
        "query_string": f"case_id={CASE_ID}".encode("ascii"),
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


def _upload_scope(*, content_length: int, sha256: str) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": f"/api/v1/attachments/{ATTACHMENT_ID}/content",
        "raw_path": f"/api/v1/attachments/{ATTACHMENT_ID}/content".encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"idempotency-key", ATTACHMENT_ID.encode("ascii")),
            (b"content-type", b"application/zip"),
            (b"content-length", str(content_length).encode("ascii")),
            (b"x-content-sha256", sha256.encode("ascii")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_live_stays_up_while_ready_reports_frozen_error(code: ErrorCode) -> None:
    admin = FakeStateAdmin(readiness=readiness(ready=False, error_code=code))
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=FakeQuery(),
        state_admin=admin,
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.get("/live"), await client.get("/ready")

    live, ready_response = _run(app, operation)

    assert live.status_code == 200
    assert live.json() == {"ok": True, "data": {"status": "live"}, "error": None}
    assert "access-control-allow-origin" not in live.headers
    assert ready_response.status_code == 503
    body = ready_response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == code.value
    assert body["error"]["retryable"] is False
    assert admin.calls == ["readiness"]


def test_ready_success_returns_the_complete_frozen_report() -> None:
    base_report = readiness()
    secret = "/private/service/data/state.json"
    expected = ReadinessReport(
        ready=True,
        checks=[
            check.model_copy(update={"message": secret})
            for check in base_report.checks
        ],
        error=None,
    )
    admin = FakeStateAdmin(readiness=expected)
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=FakeQuery(),
        state_admin=admin,
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.get("/ready")

    response = _run(app, operation)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "ready": True,
            "checks": [
                {"name": check.name, "passed": True, "message": None}
                for check in expected.checks
            ],
            "error": None,
        },
        "error": None,
    }
    assert secret not in response.text


def test_prepare_post_uses_the_same_command_and_descriptor_projection() -> None:
    service_response = application_response(
        operation="PrepareAttachment",
        primary_resource_id=ATTACHMENT_ID,
        revision=2,
        with_case_view=False,
    )
    command = FakeApplicationService([service_response])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="https://service.example.test/root",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
                "declared_size": 10,
                "declared_sha256": SHA256_A,
            },
        )

    response = _run(app, operation)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["application_response"]["case_view"] is None
    upload = body["data"]["upload"]
    assert upload["attachment_id"] == ATTACHMENT_ID
    assert upload["url"] == (
        f"https://service.example.test/root/api/v1/attachments/{ATTACHMENT_ID}/content"
    )
    assert upload["required_headers"] == {
        "Idempotency-Key": ATTACHMENT_ID,
        "Content-Type": "application/zip",
        "Content-Length": "10",
        "X-Content-SHA256": SHA256_A,
    }
    assert len(command.calls) == 1
    recorded = command.calls[0]
    assert type(recorded).__name__ == "PrepareAttachment"
    assert recorded.idempotency_key == REQUEST_ID
    assert recorded.case_id == CASE_ID


@pytest.mark.parametrize(
    "code",
    sorted(
        PORT_ERROR_CODES["ApplicationCommandPort.execute"],
        key=lambda item: item.value,
    ),
)
def test_prepare_post_preserves_every_frozen_application_command_error(
    code: ErrorCode,
) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {code.value} application failure.",
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )
    command = FakeApplicationService([ApplicationPortError(error)])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
                "declared_size": None,
                "declared_sha256": None,
            },
        )

    response = _run(app, operation)

    assert response.status_code == ERROR_SPECS[code].http_status
    assert response.json() == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }
    assert len(command.calls) == 1


def test_prepare_rejects_extra_fields_without_calling_port() -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
                "declared_size": None,
                "declared_sha256": None,
                "unexpected": "forbidden",
            },
        )

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("logs.ZIP", "application/zip"),
        ("logs.zip", "application/gzip"),
        ("../logs.zip", "application/zip"),
    ],
)
def test_prepare_rejects_unsafe_or_content_type_mismatched_archive_name(
    name: str,
    content_type: str,
) -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": name,
                "content_type": content_type,
                "declared_size": None,
                "declared_sha256": None,
            },
        )

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


def test_prepare_size_limit_is_classified_by_application_port_not_http_validation() -> None:
    limit_error = ApplicationError(
        code=ErrorCode.RESOURCE_LIMIT_EXCEEDED,
        message="Attachment size exceeds the V1 limit.",
        details=[],
        retryable=False,
    )
    command = FakeApplicationService(
        [
            application_response(
                operation="PrepareAttachment",
                primary_resource_id=ATTACHMENT_ID,
                revision=2,
            ),
            ApplicationPortError(limit_error),
        ]
    )
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        common = {
            "expected_case_revision": 1,
            "name": "logs.zip",
            "content_type": "application/zip",
            "declared_sha256": None,
        }
        at_limit = await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                **common,
                "request_id": REQUEST_ID,
                "declared_size": MAX_ATTACHMENT_BYTES,
            },
        )
        above_limit = await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                **common,
                "request_id": "10000000-0000-0000-0000-000000000002",
                "declared_size": MAX_ATTACHMENT_BYTES + 1,
            },
        )
        return at_limit, above_limit

    at_limit, above_limit = _run(app, operation)
    assert at_limit.status_code == 200
    assert at_limit.json()["data"]["upload"]["required_headers"][
        "Content-Length"
    ] == str(MAX_ATTACHMENT_BYTES)
    assert above_limit.status_code == 413
    assert above_limit.json()["error"] == limit_error.model_dump(mode="json")
    assert [item.declared_size for item in command.calls] == [
        MAX_ATTACHMENT_BYTES,
        MAX_ATTACHMENT_BYTES + 1,
    ]


@pytest.mark.parametrize("declared_size", [-1, True, 1.0, "1"])
def test_prepare_declared_size_is_strict_nonnegative(declared_size: object) -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
                "declared_size": declared_size,
                "declared_sha256": None,
            },
        )

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []


def test_put_rejects_noncanonical_content_type_before_reading_body() -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    body = StreamingUploadFixture(
        [b"must not be read"],
        fail_on_chunk=1,
    )

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers={
                "Idempotency-Key": ATTACHMENT_ID,
                "Content-Type": "application/zip; charset=binary",
                "Content-Length": "16",
                "X-Content-SHA256": SHA256_A,
            },
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []
    assert body.read_calls == 0


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Idempotency-Key", REQUEST_ID),
        ("Content-Length", "01"),
        ("Content-Length", "2684354561"),
        ("X-Content-SHA256", "A" * 64),
    ],
)
def test_put_rejects_invalid_frozen_headers_before_reading_body(
    header: str,
    value: str,
) -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )
    headers = {
        "Idempotency-Key": ATTACHMENT_ID,
        "Content-Type": "application/zip",
        "Content-Length": "0",
        "X-Content-SHA256": SHA256_A,
    }
    headers[header] = value

    body = StreamingUploadFixture([b"unreachable"], fail_on_chunk=1)

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers=headers,
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []
    assert body.read_calls == 0


@pytest.mark.parametrize(
    "missing_header",
    [
        "Idempotency-Key",
        "Content-Type",
        "Content-Length",
        "X-Content-SHA256",
    ],
)
def test_put_requires_each_of_the_four_descriptor_headers_once(
    missing_header: str,
) -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )
    all_headers = [
        ("Idempotency-Key", ATTACHMENT_ID),
        ("Content-Type", "application/zip"),
        ("Content-Length", "0"),
        ("X-Content-SHA256", SHA256_A),
    ]
    headers = [item for item in all_headers if item[0] != missing_header]
    body = StreamingUploadFixture([b"unreachable"], fail_on_chunk=1)

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers=headers,
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []
    assert body.read_calls == 0


def test_put_rejects_duplicate_descriptor_header_before_body() -> None:
    command = FakeApplicationService()
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )
    body = StreamingUploadFixture([b"unreachable"], fail_on_chunk=1)

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers=[
                ("Idempotency-Key", ATTACHMENT_ID),
                ("Idempotency-Key", ATTACHMENT_ID),
                ("Content-Type", "application/zip"),
                ("Content-Length", "0"),
                ("X-Content-SHA256", SHA256_A),
            ],
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert command.calls == []
    assert body.read_calls == 0


def test_artifact_download_streams_exact_headers_and_closes() -> None:
    payload = b"chunked"
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    stream = InMemoryBinaryStream(payload)
    query = FakeQuery()
    query.queue(
        "open_artifact",
        OpenArtifactResult(
            artifact=artifact_summary(
                size=len(payload),
                sha256=payload_sha256,
            ),
            stream=stream,
        ),
    )
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/content",
            params={"case_id": CASE_ID},
        )

    response = _run(app, operation)
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-sha256"] == payload_sha256
    assert stream.closed
    assert query.calls == [("open_artifact", (CASE_ID, ARTIFACT_ID))]


@pytest.mark.parametrize("fail_on", ["start", "body"])
def test_artifact_stream_closes_when_asgi_send_fails(fail_on: str) -> None:
    payload = b"streamed result"
    stream = InMemoryBinaryStream(payload)
    query = FakeQuery()
    query.queue(
        "open_artifact",
        OpenArtifactResult(
            artifact=artifact_summary(
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            stream=stream,
        ),
    )
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def scenario() -> None:
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == f"http.response.{fail_on}":
                raise ConnectionError("injected ASGI send failure")

        with pytest.raises(ClientDisconnect):
            await app(_artifact_scope(), receive, send)

    asyncio.run(scenario())
    assert stream.closed
    assert stream.close_calls >= 1


def test_cancelled_artifact_lookup_disposes_late_stream_result() -> None:
    payload = b"late result"
    stream = InMemoryBinaryStream(payload)
    entered = threading.Event()
    release = threading.Event()
    query = FakeQuery()

    def delayed_result(_case_id: str, _artifact_id: str) -> OpenArtifactResult:
        entered.set()
        assert release.wait(timeout=5)
        return OpenArtifactResult(
            artifact=artifact_summary(
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            stream=stream,
        )

    query.queue("open_artifact", delayed_result)
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def scenario() -> None:
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            raise AssertionError("cancelled lookup must not start a response")

        request_task = asyncio.create_task(app(_artifact_scope(), receive, send))
        assert await asyncio.to_thread(entered.wait, 5)
        request_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    asyncio.run(scenario())
    assert stream.closed


def test_artifact_midstream_read_failure_still_closes_stream() -> None:
    payload = b"two chunks"
    stream = InMemoryBinaryStream(payload, fail_on_read_number=2)
    query = FakeQuery()
    query.queue(
        "open_artifact",
        OpenArtifactResult(
            artifact=artifact_summary(
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            stream=stream,
        ),
    )
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def scenario() -> None:
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        with pytest.raises(ClientDisconnect):
            await app(_artifact_scope(), receive, send)

    asyncio.run(scenario())
    assert stream.closed


def test_artifact_requires_case_id_as_the_only_query_before_port_call() -> None:
    query = FakeQuery()
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        missing = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/content"
        )
        extra = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/content",
            params={"case_id": CASE_ID, "unexpected": "value"},
        )
        return missing, extra

    missing, extra = _run(app, operation)
    assert missing.status_code == extra.status_code == 400
    assert missing.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert extra.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert query.calls == []


@pytest.mark.parametrize(
    "code",
    sorted(
        PORT_ERROR_CODES["ApplicationQueryPort.open_artifact"],
        key=lambda item: item.value,
    ),
)
def test_artifact_download_preserves_every_frozen_query_error(
    code: ErrorCode,
) -> None:
    error = ApplicationError(
        code=code,
        message=f"Safe {code.value} artifact failure.",
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )
    query = FakeQuery()
    query.queue("open_artifact", ApplicationPortError(error))
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        return await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/content",
            params={"case_id": CASE_ID},
        )

    response = _run(app, operation)
    assert response.status_code == ERROR_SPECS[code].http_status
    assert response.json() == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }


def test_mcp_is_mounted_on_the_same_asgi_app_in_stateless_json_mode() -> None:
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def scenario() -> tuple[list[str], str | None]:
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
                        tools = await session.list_tools()
                        return [tool.name for tool in tools.tools], get_session_id()

    names, session_id = asyncio.run(scenario())
    assert names == [
        "problem_locator_create_case",
        "problem_locator_prepare_attachment",
        "problem_locator_submit_supplement",
        "problem_locator_get_case",
        "problem_locator_resume_case",
        "problem_locator_cancel_case",
        "problem_locator_list_artifacts",
    ]
    assert session_id is None


def test_put_passes_raw_content_type_and_streams_to_application_port() -> None:
    payload = b"a" * (HTTP_STREAM_CHUNK_BYTES * 2 + 17)
    body = StreamingUploadFixture(
        [payload[:100_000], payload[100_000:]],
    )
    observed_chunks: list[bytes] = []

    def consume(command: UploadAttachmentContent):
        assert isinstance(command, UploadAttachmentContent)
        assert command.idempotency_key == ATTACHMENT_ID
        assert command.attachment_id == ATTACHMENT_ID
        assert command.expected_content_type == "application/zip"
        assert command.expected_size == len(payload)
        assert command.expected_sha256 == hashlib.sha256(payload).hexdigest()
        while True:
            chunk = command.byte_stream.read(1024 * 1024)
            if chunk == b"":
                break
            observed_chunks.append(chunk)
        return application_response(
            operation="UploadAttachmentContent",
            primary_resource_id=ATTACHMENT_ID,
            revision=3,
            with_case_view=False,
        )

    command = FakeApplicationService([consume])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers={
                "Idempotency-Key": ATTACHMENT_ID,
                "Content-Type": "application/zip",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
            },
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "attachment_id": ATTACHMENT_ID,
            "case_id": CASE_ID,
            "status": "READY",
            "case_revision": 3,
        },
        "error": None,
    }
    assert b"".join(observed_chunks) == payload
    assert max(map(len, observed_chunks)) <= HTTP_STREAM_CHUNK_BYTES
    assert body.read_calls == 2
    assert len(command.calls) == 1
    assert command.calls[0].byte_stream.closed


def test_put_maps_prepared_content_type_mismatch_before_reading_body() -> None:
    body = StreamingUploadFixture([b"must not be read"], fail_on_chunk=1)
    error = ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message="Attachment Content-Type does not match prepare.",
        details=[],
        retryable=False,
    )

    def reject_before_read(command: UploadAttachmentContent):
        assert command.expected_content_type == "application/json"
        assert command.byte_stream.read_requests == []
        raise ApplicationPortError(error)

    command = FakeApplicationService([reject_before_read])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers={
                "Idempotency-Key": ATTACHMENT_ID,
                "Content-Type": "application/json",
                "Content-Length": "16",
                "X-Content-SHA256": SHA256_A,
            },
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "data": None,
        "error": error.model_dump(mode="json"),
    }
    assert body.read_calls == 0
    assert command.calls[0].byte_stream.closed


def test_put_maps_application_error_after_body_and_closes_stream() -> None:
    payload = b"wrong bytes"
    body = StreamingUploadFixture([payload])
    error = ApplicationError(
        code=ErrorCode.RESOURCE_HASH_MISMATCH,
        message="Resource publication validation failed.",
        details=[],
        retryable=False,
    )

    def reject_after_read(command: UploadAttachmentContent):
        assert command.byte_stream.read(HTTP_STREAM_CHUNK_BYTES) == payload
        assert command.byte_stream.read(HTTP_STREAM_CHUNK_BYTES) == b""
        raise ApplicationPortError(error)

    command = FakeApplicationService([reject_after_read])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def operation(client: httpx.AsyncClient):
        request = client.build_request(
            "PUT",
            f"/api/v1/attachments/{ATTACHMENT_ID}/content",
            headers={
                "Idempotency-Key": ATTACHMENT_ID,
                "Content-Type": "application/zip",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": SHA256_A,
            },
            content=body,
        )
        return await client.send(request)

    response = _run(app, operation)
    assert response.status_code == 422
    assert response.json()["error"] == error.model_dump(mode="json")
    assert body.read_calls == 1
    assert command.calls[0].byte_stream.closed


def test_cancelled_put_aborts_worker_before_closing_async_request_source() -> None:
    command_entered = threading.Event()

    def consume_until_cancelled(command: UploadAttachmentContent):
        command_entered.set()
        command.byte_stream.read(HTTP_STREAM_CHUNK_BYTES)
        raise AssertionError("aborted request read unexpectedly returned")

    command = FakeApplicationService([consume_until_cancelled])
    app = create_http_app(
        command_port=command,
        query_port=FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:8000",
    )

    async def scenario() -> None:
        receive_entered = asyncio.Event()
        never = asyncio.Event()

        async def receive():
            receive_entered.set()
            await never.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            raise AssertionError("cancelled PUT must not start a response")

        request_task = asyncio.create_task(
            app(
                _upload_scope(content_length=1, sha256=SHA256_A),
                receive,
                send,
            )
        )
        assert await asyncio.to_thread(command_entered.wait, 5)
        await asyncio.wait_for(receive_entered.wait(), timeout=5)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request_task, timeout=5)

    asyncio.run(scenario())
    assert len(command.calls) == 1
    assert command.calls[0].byte_stream.closed
