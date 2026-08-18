from __future__ import annotations

import asyncio
import hashlib

import httpx

from problem_locator.contracts.commands import (
    ArtifactListResponse,
    CaseQueryResponse,
    CreateCase,
    SubmitSupplement,
)
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from tests.deterministic.contracts._support import REPOSITORY_ROOT
from tests.deterministic.unit.interfaces.fakes import (
    FakeApplicationService,
    FakeQuery,
    FakeStateAdmin,
)
from tests.deterministic.unit.interfaces.helpers import (
    ARTIFACT_ID,
    ATTACHMENT_ID,
    CASE_ID,
    JOB_ID,
    SHA256_A,
    application_response,
    artifact_summary,
    case_view,
    problem_spec_input,
    readiness,
)


REQUEST_ID = "10000000-0000-0000-0000-000000000001"
REQUEST_ID_2 = "10000000-0000-0000-0000-000000000002"


def _run(app, operation):
    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client:
                return await operation(client)

    return asyncio.run(scenario())


def _app(command=None, query=None, *, public_base_url="http://127.0.0.1:8000"):
    return create_http_app(
        command_port=command or FakeApplicationService(),
        query_port=query or FakeQuery(),
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url=public_base_url,
    )


def _create_payload(*, request_id: str = REQUEST_ID, wait_seconds: int = 0):
    return {
        "request_id": request_id,
        "raw_problem_text": "RPC request times out.",
        "problem_spec": problem_spec_input(),
        "initial_user_facts": [
            {"name": "problem_time", "value": "2026-08-18 10:30"}
        ],
        "wait_seconds": wait_seconds,
    }


def test_create_case_maps_nested_rest_body_to_existing_command() -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase", with_case_view=False)]
    )
    app = _app(command=command)

    async def operation(client: httpx.AsyncClient):
        return await client.post("/api/v1/cases", json=_create_payload())

    response = _run(app, operation)

    assert response.status_code == 200
    assert response.json()["data"]["case_view"] is None
    assert response.json()["data"]["business_receipt"]["case_id"] == CASE_ID
    assert len(command.calls) == 1
    recorded = command.calls[0]
    assert isinstance(recorded, CreateCase)
    assert recorded.idempotency_key == REQUEST_ID
    assert recorded.problem_spec.statement == "RPC request times out."
    assert [(item.name, item.value) for item in recorded.initial_user_facts] == [
        ("problem_time", "2026-08-18 10:30")
    ]
    assert recorded.wait_seconds == 0


def test_create_case_replays_same_business_request_and_rejects_changed_content() -> None:
    command = FakeApplicationService(
        [application_response(operation="CreateCase")],
        replay_idempotent=True,
    )
    app = _app(command=command)

    async def operation(client: httpx.AsyncClient):
        first = await client.post("/api/v1/cases", json=_create_payload())
        replay = await client.post(
            "/api/v1/cases",
            json=_create_payload(wait_seconds=30),
        )
        changed_payload = _create_payload()
        changed_payload["raw_problem_text"] = "Different problem text."
        conflict = await client.post("/api/v1/cases", json=changed_payload)
        return first, replay, conflict

    first, replay, conflict = _run(app, operation)

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["business_receipt"] == replay.json()["data"][
        "business_receipt"
    ]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


def test_rest_and_mcp_map_create_and_supplement_to_identical_commands() -> None:
    command = FakeApplicationService(
        [
            application_response(operation="CreateCase"),
            application_response(operation="SubmitSupplement", revision=2),
            application_response(operation="CreateCase"),
            application_response(operation="SubmitSupplement", revision=2),
        ]
    )
    query = FakeQuery()
    app = _app(command=command, query=query)

    supplement_payload = {
        "request_id": REQUEST_ID_2,
        "expected_case_revision": 1,
        "inputs": [{"name": "problem_time", "value": "10:30"}],
        "attachment_ids": [ATTACHMENT_ID],
        "wait_seconds": 0,
    }

    async def rest_operation(client: httpx.AsyncClient):
        create = await client.post("/api/v1/cases", json=_create_payload())
        supplement = await client.post(
            f"/api/v1/cases/{CASE_ID}/supplements",
            json=supplement_payload,
        )
        return create, supplement

    rest_create, rest_supplement = _run(app, rest_operation)
    assert rest_create.status_code == rest_supplement.status_code == 200

    adapter = McpAdapter(command, query, public_base_url="http://127.0.0.1:8000")
    problem_spec = problem_spec_input()
    mcp_create = asyncio.run(
        adapter.call(
            "problem_locator_create_case",
            {
                "request_id": REQUEST_ID,
                "raw_problem_text": "RPC request times out.",
                **problem_spec,
                "initial_user_fact_names": ["problem_time"],
                "initial_user_fact_values": ["2026-08-18 10:30"],
                "wait_seconds": 0,
            },
        )
    )
    mcp_supplement = asyncio.run(
        adapter.call(
            "problem_locator_submit_supplement",
            {
                "request_id": REQUEST_ID_2,
                "case_id": CASE_ID,
                "expected_case_revision": 1,
                "input_names": ["problem_time"],
                "input_values": ["10:30"],
                "attachment_ids": [ATTACHMENT_ID],
                "wait_seconds": 0,
            },
        )
    )

    assert mcp_create["ok"] is True
    assert mcp_supplement["ok"] is True
    assert command.calls[0] == command.calls[2]
    assert command.calls[1] == command.calls[3]


def test_create_case_rejects_duplicate_nested_names_and_unknown_fields() -> None:
    command = FakeApplicationService()
    app = _app(command=command)
    payload = _create_payload()
    payload["initial_user_facts"] = [
        {"name": "problem_time", "value": "first"},
        {"name": "problem_time", "value": "second"},
    ]
    payload["problem_spec"]["unexpected"] = "forbidden"

    async def operation(client: httpx.AsyncClient):
        return await client.post("/api/v1/cases", json=payload)

    response = _run(app, operation)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert "body.problem_spec.unexpected" in fields
    assert command.calls == []


def test_get_case_maps_long_poll_query_and_rejects_unknown_parameters() -> None:
    query = FakeQuery()
    query.queue(
        "get_case",
        CaseQueryResponse(case_view=case_view(revision=3), wait_timed_out=True),
    )
    app = _app(query=query)

    async def operation(client: httpx.AsyncClient):
        valid = await client.get(
            f"/api/v1/cases/{CASE_ID}",
            params={"wait_for_job_id": JOB_ID, "wait_seconds": 30},
        )
        invalid = await client.get(
            f"/api/v1/cases/{CASE_ID}",
            params={"unexpected": "value"},
        )
        return valid, invalid

    valid, invalid = _run(app, operation)

    assert valid.status_code == 200
    assert valid.json()["data"]["case_view"]["case_revision"] == 3
    assert valid.json()["data"]["wait_timed_out"] is True
    assert query.calls == [("get_case", (CASE_ID, JOB_ID, 30))]
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


def test_submit_supplement_maps_named_values_and_ready_attachments() -> None:
    command = FakeApplicationService(
        [application_response(operation="SubmitSupplement", revision=4)]
    )
    app = _app(command=command)

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/supplements",
            json={
                "request_id": REQUEST_ID_2,
                "expected_case_revision": 3,
                "inputs": [{"name": "problem_time", "value": "10:30"}],
                "attachment_ids": [ATTACHMENT_ID],
                "wait_seconds": 0,
            },
        )

    response = _run(app, operation)

    assert response.status_code == 200
    assert len(command.calls) == 1
    recorded = command.calls[0]
    assert isinstance(recorded, SubmitSupplement)
    assert recorded.case_id == CASE_ID
    assert recorded.expected_case_revision == 3
    assert recorded.inputs == {"problem_time": "10:30"}
    assert recorded.attachment_ids == [ATTACHMENT_ID]


def test_submit_supplement_requires_content_and_unique_input_names() -> None:
    command = FakeApplicationService()
    app = _app(command=command)

    async def operation(client: httpx.AsyncClient):
        empty = await client.post(
            f"/api/v1/cases/{CASE_ID}/supplements",
            json={
                "request_id": REQUEST_ID_2,
                "expected_case_revision": 3,
                "inputs": [],
                "attachment_ids": [],
                "wait_seconds": 0,
            },
        )
        duplicate = await client.post(
            f"/api/v1/cases/{CASE_ID}/supplements",
            json={
                "request_id": REQUEST_ID_2,
                "expected_case_revision": 3,
                "inputs": [
                    {"name": "problem_time", "value": "first"},
                    {"name": "problem_time", "value": "second"},
                ],
                "attachment_ids": [],
                "wait_seconds": 0,
            },
        )
        return empty, duplicate

    empty, duplicate = _run(app, operation)

    assert empty.status_code == duplicate.status_code == 400
    assert command.calls == []


def test_prepare_attachment_requires_browser_metadata() -> None:
    command = FakeApplicationService()
    app = _app(command=command)

    async def operation(client: httpx.AsyncClient):
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/attachments",
            json={
                "request_id": REQUEST_ID,
                "expected_case_revision": 1,
                "name": "logs.zip",
                "content_type": "application/zip",
            },
        )

    response = _run(app, operation)

    assert response.status_code == 400
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert fields == {"body.declared_sha256", "body.declared_size"}
    assert command.calls == []


def test_list_artifacts_exposes_only_public_views_and_download_urls() -> None:
    query = FakeQuery()
    query.queue("list_artifacts", ArtifactListResponse(artifacts=[artifact_summary()]))
    app = _app(query=query, public_base_url="https://locator.example/root")

    async def operation(client: httpx.AsyncClient):
        return await client.get(f"/api/v1/cases/{CASE_ID}/artifacts")

    response = _run(app, operation)

    assert response.status_code == 200
    assert query.calls == [("list_artifacts", (CASE_ID, False))]
    artifact = response.json()["data"]["artifacts"][0]
    assert artifact["artifact_id"] == ARTIFACT_ID
    assert artifact["download_url"] == (
        f"https://locator.example/root/api/v1/artifacts/{ARTIFACT_ID}/content"
        f"?case_id={CASE_ID}"
    )
    assert "created_by_job_id" not in artifact
    assert "downloadable" not in artifact


def test_openapi_and_swagger_publish_the_browser_contract() -> None:
    app = _app()

    async def operation(client: httpx.AsyncClient):
        return await client.get("/openapi.json"), await client.get("/docs")

    openapi_response, docs_response = _run(app, operation)

    assert openapi_response.status_code == 200
    schema = openapi_response.json()
    expected_paths = {
        "/api/v1/cases",
        "/api/v1/cases/{case_id}",
        "/api/v1/cases/{case_id}/attachments",
        "/api/v1/attachments/{attachment_id}/content",
        "/api/v1/cases/{case_id}/supplements",
        "/api/v1/cases/{case_id}/artifacts",
        "/api/v1/artifacts/{artifact_id}/content",
    }
    assert expected_paths <= set(schema["paths"])
    assert "/mcp" not in schema["paths"]

    create_schema = schema["components"]["schemas"]["CreateCaseBody"]
    assert "problem_spec" in create_schema["required"]
    assert "initial_user_facts" in create_schema["properties"]
    prepare_schema = schema["components"]["schemas"]["PrepareAttachmentBody"]
    assert {"declared_size", "declared_sha256"} <= set(prepare_schema["required"])

    upload = schema["paths"][
        "/api/v1/attachments/{attachment_id}/content"
    ]["put"]
    binary_content = upload["requestBody"]["content"]
    assert set(binary_content) == {
        "application/gzip",
        "application/zip",
        "application/x-tar",
    }
    assert all(
        item["schema"] == {"type": "string", "format": "binary"}
        for item in binary_content.values()
    )
    content_length = next(
        item for item in upload["parameters"] if item["name"] == "Content-Length"
    )
    assert "Chrome" in content_length["description"]

    download = schema["paths"][
        "/api/v1/artifacts/{artifact_id}/content"
    ]["get"]["responses"]["200"]
    assert {
        "Content-Length",
        "Content-Type",
        "X-Content-SHA256",
        "X-Problem-Locator-Correlation-ID",
    } <= set(download["headers"])

    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text


def test_openapi_contract_matches_versioned_snapshot() -> None:
    schema = _app().openapi()
    document_bytes = canonical_json_bytes(schema)
    snapshot = {
        "schema_version": 1,
        "openapi": schema["openapi"],
        "info": schema["info"],
        "document_sha256": hashlib.sha256(document_bytes).hexdigest(),
        "operations": {
            path: sorted(
                method
                for method in item
                if method in {"get", "post", "put", "delete", "patch"}
            )
            for path, item in sorted(schema["paths"].items())
        },
        "component_schemas": sorted(schema["components"]["schemas"]),
    }
    expected = canonical_json_bytes(snapshot)
    snapshot_path = REPOSITORY_ROOT / "schemas/v2/web-api.openapi.snapshot.json"
    actual = snapshot_path.read_bytes()
    assert actual == expected, (
        f"regenerate {snapshot_path.relative_to(REPOSITORY_ROOT)} as "
        f"{expected.decode('utf-8')}"
    )


def test_wildcard_cors_allows_browser_preflight_without_credentials() -> None:
    app = _app()

    async def operation(client: httpx.AsyncClient):
        preflight = await client.options(
            "/api/v1/cases",
            headers={
                "Origin": "https://arbitrary-ui.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,idempotency-key,x-content-sha256",
            },
        )
        actual = await client.get(
            "/live",
            headers={"Origin": "https://another-ui.example"},
        )
        return preflight, actual

    preflight, actual = _run(app, operation)

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in preflight.headers
    assert preflight.headers["x-problem-locator-correlation-id"]
    allow_headers = preflight.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allow_headers
    assert "x-content-sha256" in allow_headers
    assert actual.headers["access-control-allow-origin"] == "*"
    assert "x-problem-locator-correlation-id" in actual.headers[
        "access-control-expose-headers"
    ].lower()
