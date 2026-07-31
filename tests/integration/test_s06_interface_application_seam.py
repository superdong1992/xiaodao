from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from problem_locator.application import build_application_service
from problem_locator.contracts import Job, RuntimeBindings, StateFile
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryAttachmentUploadGuard,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.unit.interfaces.fakes import FakeStateAdmin
from tests.unit.interfaces.helpers import readiness


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000806"
TRIGGER_ID = "00000000-0000-0000-0000-000000000807"
JOB_ID = "00000000-0000-0000-0000-000000000808"
FIXED_TIME = "2026-07-31T08:06:00.000Z"


def _route_bindings() -> RuntimeBindings:
    route = Job.model_validate_json((FIXTURES / "job-route.json").read_text())
    return RuntimeBindings(
        agent_profile_ref=route.agent_profile_ref,
        available_skill_refs=route.available_skill_refs,
        skill_ref=route.skill_ref,
        tool_bundle_ref=route.tool_bundle_ref,
        context_policy_ref=route.context_policy_ref,
        output_contract_ref=route.output_contract_ref,
        logparse_tool_ref=route.logparse_tool_ref,
        logparse_product=route.logparse_product,
        resource_limits=route.resource_limits,
    )


def _application():
    payload = json.loads((FIXTURES / "state.json").read_text())
    state = StateFile.model_validate({**payload, "generation": 0, "cases": {}})
    repository = InMemoryStateRepository(state)
    guard = InMemoryPublicationCommitGuard()
    upload_guard = InMemoryAttachmentUploadGuard()
    resources = InMemoryResourceStore(
        upload_guard=upload_guard,
        publication_guard=guard,
    )
    service = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=guard,
        upload_guard=upload_guard,
        execution_records=InMemoryExecutionRecordStore(),
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=FakeAssetCatalog(route=_route_bindings()),
        dispatcher=RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(FIXED_TIME),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "case": [CASE_ID],
                "trigger": [TRIGGER_ID],
                "job": [JOB_ID],
            }
        ),
    )
    return service, repository


def _create_arguments(statement: str = "Payment RPC timeout") -> dict[str, object]:
    return {
        "request_id": "s08-mcp-create",
        "problem_spec": {
            "statement": statement,
            "expected_behavior": "Inventory responds before the deadline",
            "actual_behavior": "Payment observes an RPC timeout",
            "scope": "payment-service to inventory-service",
            "goals": ["Locate the cause"],
            "non_goals": [],
            "constraints": ["Use supplied evidence"],
            "completion_criteria": ["Produce an evidenced diagnosis"],
        },
        "initial_user_facts": [],
        "wait_seconds": 0,
    }


def test_mcp_dto_idempotency_and_query_use_the_real_application() -> None:
    service, repository = _application()
    adapter = McpAdapter(
        service,
        service,
        public_base_url="http://127.0.0.1:18080",
    )

    first = asyncio.run(
        adapter.call("problem_locator_create_case", _create_arguments())
    )
    replay = asyncio.run(
        adapter.call("problem_locator_create_case", _create_arguments())
    )
    conflict = asyncio.run(
        adapter.call(
            "problem_locator_create_case",
            _create_arguments("A different RPC target"),
        )
    )
    queried = asyncio.run(
        adapter.call(
            "problem_locator_get_case",
            {"case_id": CASE_ID, "wait_for_job_id": None, "wait_seconds": 0},
        )
    )

    assert first == replay
    assert first["ok"] is True
    assert first["data"]["business_receipt"]["primary_resource_id"] == CASE_ID
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert queried["ok"] is True
    assert queried["data"]["case_view"]["case_id"] == CASE_ID
    assert repository.read_snapshot().generation == 1


def test_shared_http_application_exposes_live_and_typed_readiness() -> None:
    service, _ = _application()
    app = create_http_app(
        command_port=service,
        query_port=service,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:18080",
    )

    with TestClient(app) as client:
        live = client.get("/live")
        ready = client.get("/ready")

    assert live.status_code == 200
    assert live.json()["data"] == {"status": "live"}
    assert ready.status_code == 200
    assert ready.json()["data"]["ready"] is True
    assert [check["name"] for check in ready.json()["data"]["checks"]] == [
        "CONFIG",
        "INSTANCE_LOCK",
        "STATE",
        "DATA_DIRECTORIES",
        "RECOVERY",
    ]
