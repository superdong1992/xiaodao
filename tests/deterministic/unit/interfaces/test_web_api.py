from __future__ import annotations

import asyncio
import json
import re

import httpx

from problem_locator.contracts.commands import (
    ArtifactListResponse,
    CaseQueryResponse,
    CreateCase,
    SubmitSupplement,
)
from problem_locator.contracts.enums import ErrorCode
from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_DESCRIPTION_UTF8_BYTES,
    MAX_USER_TEXT_UTF8_BYTES,
)
from problem_locator.contracts.models import CaseFailure
from problem_locator.contracts.serialization import canonical_json_bytes
from problem_locator.interfaces.error_mapping import http_status_for
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from problem_locator.interfaces.rest_models import (
    ApplicationSuccessEnvelope,
    ArtifactListSuccessEnvelope,
    CaseQuerySuccessEnvelope,
    CreateCaseBody,
    ErrorEnvelope,
    LiveSuccessEnvelope,
    PrepareAttachmentBody,
    PrepareAttachmentSuccessEnvelope,
    ReadinessSuccessEnvelope,
    SubmitSupplementBody,
    UploadReadySuccessEnvelope,
)
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


def _guide_json_examples() -> list[object]:
    guide = (REPOSITORY_ROOT / "docs/browser-rest-api.md").read_text(encoding="utf-8")
    return [
        json.loads(match)
        for match in re.findall(r"```json\s*\n([\s\S]*?)```", guide)
    ]


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


def test_openapi_uuid_metadata_preserves_application_validation_errors() -> None:
    app = _app()

    async def operation(client: httpx.AsyncClient):
        invalid_path = await client.get("/api/v1/cases/NOT-A-UUID")
        invalid_query = await client.get(
            f"/api/v1/cases/{CASE_ID}",
            params={"wait_for_job_id": "NOT-A-UUID"},
        )
        return invalid_path, invalid_query

    invalid_path, invalid_query = _run(app, operation)

    for response in (invalid_path, invalid_query):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
        assert response.json()["error"]["details"][0]["field"] == "$"


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
    expected_operations = {
        ("/live", "get"): "get_liveness",
        ("/ready", "get"): "get_readiness",
        ("/api/v1/cases", "post"): "create_case",
        ("/api/v1/cases/{case_id}", "get"): "get_case",
        (
            "/api/v1/cases/{case_id}/attachments",
            "post",
        ): "prepare_attachment",
        (
            "/api/v1/attachments/{attachment_id}/content",
            "put",
        ): "upload_attachment",
        (
            "/api/v1/cases/{case_id}/supplements",
            "post",
        ): "submit_supplement",
        ("/api/v1/cases/{case_id}/artifacts", "get"): "list_artifacts",
        (
            "/api/v1/artifacts/{artifact_id}/content",
            "get",
        ): "download_artifact",
    }
    assert set(schema["paths"]) == {path for path, _method in expected_operations}
    assert "/mcp" not in schema["paths"]
    assert {
        (path, method): schema["paths"][path][method]["operationId"]
        for path, method in expected_operations
    } == expected_operations
    for path, method in expected_operations:
        operation = schema["paths"][path][method]
        assert operation["summary"]
        assert operation["description"]
        for response in operation["responses"].values():
            assert response["description"]
            assert "X-Problem-Locator-Correlation-ID" in response["headers"]

    create_schema = schema["components"]["schemas"]["CreateCaseBody"]
    assert "problem_spec" in create_schema["required"]
    assert "initial_user_facts" in create_schema["properties"]
    assert create_schema["properties"]["initial_user_facts"]["default"] == []
    assert create_schema["examples"]
    prepare_schema = schema["components"]["schemas"]["PrepareAttachmentBody"]
    assert {"declared_size", "declared_sha256"} <= set(prepare_schema["required"])
    assert (
        prepare_schema["properties"]["declared_size"]["maximum"]
        == MAX_ATTACHMENT_BYTES
    )

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
    assert {item["name"] for item in upload["parameters"] if item["in"] == "header"} == {
        "Idempotency-Key",
        "Content-Type",
        "Content-Length",
        "X-Content-SHA256",
    }
    content_length = next(
        item for item in upload["parameters"] if item["name"] == "Content-Length"
    )
    assert "Chrome" in content_length["description"]

    download_operation = schema["paths"][
        "/api/v1/artifacts/{artifact_id}/content"
    ]["get"]
    case_query = next(
        item
        for item in download_operation["parameters"]
        if item["in"] == "query" and item["name"] == "case_id"
    )
    assert case_query["required"] is True
    assert case_query["schema"]["format"] == "uuid"
    wait_for_job = next(
        item
        for item in schema["paths"]["/api/v1/cases/{case_id}"]["get"]["parameters"]
        if item["name"] == "wait_for_job_id"
    )
    assert "active Job in the initial snapshot" in wait_for_job["description"]
    download = download_operation["responses"]["200"]
    assert {
        "Content-Length",
        "Content-Type",
        "X-Content-SHA256",
        "X-Problem-Locator-Correlation-ID",
    } <= set(download["headers"])

    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text


def test_openapi_describes_every_parameter_and_reachable_model_field() -> None:
    schema = _app().openapi()
    uuid_names = {
        "case_id",
        "wait_for_job_id",
        "attachment_id",
        "artifact_id",
    }

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put"}:
                continue
            for parameter in operation.get("parameters", []):
                assert parameter["description"]
                assert parameter["schema"]
                if parameter["name"] in uuid_names:
                    outer_schema = parameter["schema"]
                    parameter_schema = outer_schema
                    if "anyOf" in outer_schema:
                        parameter_schema = next(
                            item
                            for item in outer_schema["anyOf"]
                            if item.get("type") == "string"
                        )
                    assert (
                        parameter_schema.get("format")
                        or outer_schema.get("format")
                    ) == "uuid"
                    assert parameter_schema["pattern"].startswith("^")
            if "requestBody" in operation:
                assert operation["requestBody"]["description"]

    missing = [
        f"{schema_name}.{field_name}"
        for schema_name, component in schema["components"]["schemas"].items()
        for field_name, field_schema in component.get("properties", {}).items()
        if not field_schema.get("description")
    ]
    assert missing == []
    assert all(
        component.get("description")
        for component in schema["components"]["schemas"].values()
    )

    utf8_limited_fields = []

    def collect_utf8_limits(value: object) -> set[int]:
        limits: set[int] = set()
        if isinstance(value, dict):
            if "x-max-utf8-bytes" in value:
                limits.add(value["x-max-utf8-bytes"])
            for nested in value.values():
                limits.update(collect_utf8_limits(nested))
        elif isinstance(value, list):
            for nested in value:
                limits.update(collect_utf8_limits(nested))
        return limits

    for schema_name, component in schema["components"]["schemas"].items():
        for field_name, field_schema in component.get("properties", {}).items():
            limits = collect_utf8_limits(field_schema)
            if not limits:
                continue
            utf8_limited_fields.append(f"{schema_name}.{field_name}")
            assert "UTF-8 bytes" in field_schema["description"]
            assert limits <= {
                MAX_USER_TEXT_UTF8_BYTES,
                MAX_DESCRIPTION_UTF8_BYTES,
            }
    assert utf8_limited_fields

    response_refs: set[str] = set()

    def collect_refs(value: object) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                response_refs.add(ref.rsplit("/", 1)[-1])
            for nested in value.values():
                collect_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_refs(nested)

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put"}:
                continue
            collect_refs(operation.get("responses", {}))

    visited: set[str] = set()
    while response_refs - visited:
        schema_name = next(iter(response_refs - visited))
        visited.add(schema_name)
        component = schema["components"]["schemas"][schema_name]
        collect_refs(component)
        properties = component.get("properties", {})
        if component.get("type") == "object" and properties:
            expected_required = list(properties)
            if schema_name == "CaseFailure":
                expected_required = [
                    field_name
                    for field_name in expected_required
                    if field_name not in {"reason_code", "diagnostic_id"}
                ]
            if schema_name == "CaseView":
                expected_required = [
                    field_name
                    for field_name in expected_required
                    if field_name != "methods_result"
                ]
            assert component["required"] == expected_required


def test_methods_failure_diagnostics_are_optional_in_rest_and_serialization() -> None:
    case_failure_schema = _app().openapi()["components"]["schemas"]["CaseFailure"]
    required = set(case_failure_schema["required"])
    assert "reason_code" not in required
    assert "diagnostic_id" not in required

    legacy_failure = CaseFailure(
        code=ErrorCode.OUTCOME_INVALID,
        message="Outcome validation failed.",
        source_job_id=JOB_ID,
        source_outcome_id=None,
        occurred_at="2026-07-31T00:00:00.000Z",
    )
    encoded = canonical_json_bytes(legacy_failure)
    assert b'"reason_code"' not in encoded
    assert b'"diagnostic_id"' not in encoded


def test_nonterminal_methods_result_is_optional_in_rest_and_serialization() -> None:
    case_view_schema = _app().openapi()["components"]["schemas"]["CaseView"]
    assert "methods_result" not in set(case_view_schema["required"])

    encoded = canonical_json_bytes(case_view())
    assert b'"methods_result"' not in encoded


def test_openapi_examples_validate_against_the_real_rest_dtos() -> None:
    schema = _app().openapi()
    components = schema["components"]["schemas"]
    operations_with_error_examples: set[str] = set()

    for model, schema_name in (
        (CreateCaseBody, "CreateCaseBody"),
        (PrepareAttachmentBody, "PrepareAttachmentBody"),
        (SubmitSupplementBody, "SubmitSupplementBody"),
    ):
        examples = components[schema_name]["examples"]
        assert examples
        for example in examples:
            model.model_validate(example)

    success_models = {
        "get_liveness": LiveSuccessEnvelope,
        "get_readiness": ReadinessSuccessEnvelope,
        "create_case": ApplicationSuccessEnvelope,
        "get_case": CaseQuerySuccessEnvelope,
        "submit_supplement": ApplicationSuccessEnvelope,
        "prepare_attachment": PrepareAttachmentSuccessEnvelope,
        "upload_attachment": UploadReadySuccessEnvelope,
        "list_artifacts": ArtifactListSuccessEnvelope,
    }
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put"}:
                continue
            operation_id = operation["operationId"]
            if operation_id in success_models:
                value = operation["responses"]["200"]["content"][
                    "application/json"
                ]["examples"]["success"]["value"]
                success_models[operation_id].model_validate(value)
            for status, response in operation.get("responses", {}).items():
                for media in response.get("content", {}).values():
                    for example in media.get("examples", {}).values():
                        value = example["value"]
                        if value.get("ok") is not False:
                            continue
                        parsed = ErrorEnvelope.model_validate(value)
                        assert http_status_for(parsed.error) == int(status)
                        operations_with_error_examples.add(operation_id)

    assert operations_with_error_examples == {
        "get_readiness",
        "create_case",
        "get_case",
        "submit_supplement",
        "prepare_attachment",
        "upload_attachment",
        "list_artifacts",
        "download_artifact",
    }

    input_constraints = components["InputRequirementConstraints"]["properties"]
    assert "empty array" in input_constraints["allowed_values"]["description"]
    assert "Python fullmatch" in input_constraints["pattern"]["description"]
    assert "version string" in components["VersionedRef"]["properties"][
        "version"
    ]["description"]

    operations = {
        operation["operationId"]: operation
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put"}
    }
    created = operations["create_case"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["success"]["value"]["data"]
    assert created["business_receipt"]["status"] == "RUNNING"
    assert created["business_receipt"]["job_id"] == created["case_view"][
        "active_job"
    ]["job_id"]
    assert created["case_view"]["status"] == "RUNNING"

    submit = operations["submit_supplement"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["success"]["value"]["data"]
    assert submit["business_receipt"]["case_revision"] == 2
    assert submit["case_view"]["case_revision"] == 2
    assert submit["business_receipt"]["status"] == "RUNNING"

    prepared = operations["prepare_attachment"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["success"]["value"]["data"]["application_response"]
    assert prepared["business_receipt"]["case_revision"] == 2
    assert prepared["case_view"]["case_revision"] == 2
    assert prepared["business_receipt"]["status"] == "UPLOADING"

    uploaded = operations["upload_attachment"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["success"]["value"]["data"]
    assert uploaded["case_revision"] == 3

    revision_conflict_operations = {
        operation_id
        for operation_id, operation in operations.items()
        for response in operation["responses"].values()
        for media in response.get("content", {}).values()
        for example in media.get("examples", {}).values()
        if isinstance(example["value"].get("error"), dict)
        and example["value"]["error"].get("code") == "REVISION_CONFLICT"
    }
    assert revision_conflict_operations == {
        "submit_supplement",
        "prepare_attachment",
    }


def test_guide_json_examples_validate_against_the_real_rest_dtos() -> None:
    examples = _guide_json_examples()
    assert len(examples) == 13
    assert examples[0] == {"ok": True, "data": {}, "error": None}

    for index, model in (
        (4, CreateCaseBody),
        (7, PrepareAttachmentBody),
        (10, SubmitSupplementBody),
    ):
        model.model_validate(examples[index])

    for index, model in (
        (1, ErrorEnvelope),
        (2, LiveSuccessEnvelope),
        (3, ReadinessSuccessEnvelope),
        (5, ApplicationSuccessEnvelope),
        (6, CaseQuerySuccessEnvelope),
        (8, PrepareAttachmentSuccessEnvelope),
        (9, UploadReadySuccessEnvelope),
        (11, ApplicationSuccessEnvelope),
        (12, ArtifactListSuccessEnvelope),
    ):
        parsed = model.model_validate(examples[index])
        assert parsed.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            exclude_unset=False,
        ) == examples[index]


def test_create_case_openapi_and_guide_examples_execute_through_asgi() -> None:
    command = FakeApplicationService(
        [
            application_response(operation="CreateCase", with_case_view=False),
            application_response(operation="CreateCase", with_case_view=False),
        ]
    )
    app = _app(command=command)
    openapi_payload = app.openapi()["components"]["schemas"]["CreateCaseBody"][
        "examples"
    ][0]
    guide_payload = _guide_json_examples()[4]

    async def operation(client: httpx.AsyncClient):
        return (
            await client.post("/api/v1/cases", json=openapi_payload),
            await client.post("/api/v1/cases", json=guide_payload),
        )

    responses = _run(app, operation)

    assert [response.status_code for response in responses] == [200, 200]
    assert [call.idempotency_key for call in command.calls] == [
        openapi_payload["request_id"],
        guide_payload["request_id"],
    ]


def test_openapi_contains_no_cross_protocol_surface() -> None:
    document = canonical_json_bytes(_app().openapi()).lower()
    assert b'"/mcp"' not in document
    assert b"problem_locator_create_case" not in document
    assert b"claude" not in document


def test_openapi_contract_matches_versioned_snapshot() -> None:
    schema = _app().openapi()
    expected = canonical_json_bytes(schema)
    snapshot_path = REPOSITORY_ROOT / "schemas/v2/web-api.openapi.snapshot.json"
    actual = snapshot_path.read_bytes()
    assert actual == expected, (
        f"regenerate the complete canonical document at "
        f"{snapshot_path.relative_to(REPOSITORY_ROOT)}"
    )

    app = _app()

    async def operation(client: httpx.AsyncClient):
        return await client.get("/openapi.json")

    response = _run(app, operation)
    assert response.content == actual


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
