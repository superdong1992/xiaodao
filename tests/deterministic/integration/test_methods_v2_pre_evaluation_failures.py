from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import (
    CaseAggregate,
    CaseStatus,
    Job,
    OutcomeDisposition,
    StateFile,
    canonical_json_bytes,
    method_pre_evaluation_diagnostic_id_v2,
)
from problem_locator.contracts.methods_reason_v2 import METHOD_PUBLIC_REASON_TEXT_V2
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.methods_records_v2 import (
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
)
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    FakeLogparseBrokerFactory,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.deterministic.unit.interfaces.fakes import (
    FakeApplicationService,
    FakeStateAdmin,
)
from tests.deterministic.unit.interfaces.helpers import readiness
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _Clock,
    _StateView,
    _claimed_logparse_job_state_and_resources,
    _logparse_catalog,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime_methods_v2 import (
    _EvidenceV2SpecialistBackend,
)


ROOT = Path(__file__).resolve().parents[3]
STATE_FIXTURE = ROOT / "tests/fixtures/contracts/positive/state.json"
PROCESSED_AT = "2026-07-31T08:04:00.000Z"


class _RejectFirstLogOpenRecords(InMemoryExecutionRecordStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_log_open = True

    def open_log_sinks(self, job_id: str, combined_limit_bytes: int):
        if self.reject_log_open:
            self.reject_log_open = False
            raise OSError("injected execution-record log-open failure")
        return super().open_log_sinks(job_id, combined_limit_bytes)


def _state_with_aggregate(aggregate: CaseAggregate) -> StateFile:
    payload = json.loads(STATE_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"] = {
        aggregate.case.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(payload)


def _submit_and_query(
    aggregate: CaseAggregate,
    outcome: Any,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    repository = InMemoryStateRepository(_state_with_aggregate(aggregate))
    records = InMemoryExecutionRecordStore()
    outcome_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    notifier = InMemoryStateChangeNotifier()
    submission = OutcomeSubmissionService(
        repository,
        resources,
        guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        FakeAssetCatalog(),
        RecordingDispatcher(),
        notifier,
        FakeClock(PROCESSED_AT),
        DeterministicIdGenerator(seed="methods-v2-pre-evaluation-submission"),
    )
    receipt = submission.submit_outcome(outcome, outcome_ref)
    assert receipt.disposition is OutcomeDisposition.APPLIED
    query = ApplicationQueryService(repository, resources, notifier)
    direct = query.get_case(outcome.case_id).case_view

    mcp = McpAdapter(
        FakeApplicationService(),
        query,
        public_base_url="http://127.0.0.1:18080",
    )
    mcp_result = asyncio.run(
        mcp.call(
            "problem_locator_get_case",
            {
                "case_id": outcome.case_id,
                "wait_for_job_id": None,
                "wait_seconds": 0,
            },
        )
    )
    app = create_http_app(
        command_port=FakeApplicationService(),
        query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://127.0.0.1:18080",
    )
    with TestClient(app) as client:
        rest_result = client.get(f"/api/v1/cases/{outcome.case_id}")
    assert mcp_result["ok"] is True
    assert rest_result.status_code == 200
    return (
        direct,
        mcp_result["data"]["case_view"],
        rest_result.json()["data"]["case_view"],
    )


def _pre_evaluation_runtime(
    tmp_path: Path,
    failure_point: str,
) -> tuple[
    DiagnosisRuntime,
    Job,
    CaseAggregate,
    _EvidenceV2SpecialistBackend,
    InMemoryExecutionRecordStore,
]:
    factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(tmp_path, factory)
    job, aggregate, resources = _claimed_logparse_job_state_and_resources(catalog)
    backend = _EvidenceV2SpecialistBackend(factory, job, ("VALID",))
    records: InMemoryExecutionRecordStore
    records = (
        _RejectFirstLogOpenRecords()
        if failure_point == "execution-record"
        else InMemoryExecutionRecordStore()
    )
    workspace_root = tmp_path / "runtime-data"
    if failure_point == "workspace":
        workspace_root.write_text("not a directory", encoding="utf-8")
    runtime = DiagnosisRuntime(
        state_repository=_StateView(aggregate),
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=factory,
        execution_records=records,
        clock=_Clock(),
        id_generator=DeterministicIdGenerator(seed="methods-v2-pre-evaluation-runtime"),
        workspace_manager=WorkspaceManager(workspace_root),
        backend=backend,
    )
    if failure_point == "resource":
        skill_file = (
            tmp_path
            / "logparse-skills"
            / "rpc-log-analysis"
            / "package"
            / "rpc-log-analysis"
            / "SKILL.md"
        )
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\nresource drift\n",
            encoding="utf-8",
        )
    elif failure_point == "logparse":
        backend.accept_request = True
        backend.emit_claim = False
    return runtime, job, aggregate, backend, records


@pytest.mark.parametrize(
    ("failure_point", "reason_code"),
    [
        ("resource", "RESOURCE_SNAPSHOT_DRIFT"),
        ("workspace", "SERVER_INVARIANT_VIOLATION"),
        ("logparse", "SERVER_INVARIANT_VIOLATION"),
        ("execution-record", "AUDIT_ARCHIVE_FAILED"),
    ],
)
def test_pre_evaluation_failure_reaches_case_mcp_and_rest_without_fake_graph(
    tmp_path: Path,
    failure_point: str,
    reason_code: str,
) -> None:
    runtime, job, aggregate, backend, records = _pre_evaluation_runtime(
        tmp_path / failure_point,
        failure_point,
    )

    runtime_receipt = runtime.execute(job, InMemoryCancellationSignal())

    outcome = runtime_receipt.job_outcome
    assert outcome.methods_terminal_projection is None
    assert read_method_evidence_graph_v2(records, job_id=job.job_id) is None
    assert read_method_evaluation_plan_v2(records, job_id=job.job_id) is None
    assert outcome.error is not None
    assert outcome.error.reason_code == reason_code
    expected_diagnostic_id = method_pre_evaluation_diagnostic_id_v2(
        case_id=job.case_id,
        source_job_id=job.job_id,
        reason_code=reason_code,
        source_stage=outcome.error.stage.value,
        source_error_code=outcome.error.code.value,
    )
    assert outcome.error.diagnostic_id == expected_diagnostic_id
    assert outcome.error.message == METHOD_PUBLIC_REASON_TEXT_V2[reason_code]
    assert len(backend.calls) == (1 if failure_point == "logparse" else 0)

    direct, mcp_view, rest_view = _submit_and_query(aggregate, outcome)

    assert direct.status is CaseStatus.FAILED
    assert direct.methods_result is None
    assert direct.failure is not None
    assert direct.failure.reason_code == reason_code
    assert direct.failure.diagnostic_id == expected_diagnostic_id
    assert direct.failure.message == METHOD_PUBLIC_REASON_TEXT_V2[reason_code]
    assert "methods_result" not in mcp_view
    assert "methods_result" not in rest_view
    assert mcp_view["failure"]["reason_code"] == reason_code
    assert rest_view["failure"]["reason_code"] == reason_code
    assert mcp_view["failure"]["diagnostic_id"] == expected_diagnostic_id
    assert rest_view["failure"]["diagnostic_id"] == expected_diagnostic_id
