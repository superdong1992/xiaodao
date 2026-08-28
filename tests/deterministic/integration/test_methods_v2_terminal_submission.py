from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from problem_locator.application.mutations import apply_transition_plan_to_case
from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import (
    CaseAggregate,
    CaseStatus,
    JobStatus,
    OutcomeDisposition,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.mcp_server import McpAdapter
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.deterministic.unit.domain._builders import rebuild, running, snapshot_with_active
from tests.deterministic.unit.domain.test_methods_v2_terminal_bridge import (
    _review_terminal,
    _specialist_terminal,
)
from tests.deterministic.unit.interfaces.fakes import (
    FakeApplicationService,
    FakeStateAdmin,
)
from tests.deterministic.unit.interfaces.helpers import readiness


ROOT = Path(__file__).resolve().parents[3]
STATE_FIXTURE = ROOT / "tests/fixtures/contracts/positive/state.json"
PROCESSED_AT = "2026-07-31T08:04:00.000Z"


def _running_review_state(
    tmp_path,
    specialist_verdicts: tuple[str, ...],
    reviewer_verdicts: tuple[str, ...],
):
    flow = _review_terminal(
        tmp_path,
        specialist_verdicts=specialist_verdicts,
        reviewer_verdicts=reviewer_verdicts,
    )
    reviewing_case = apply_transition_plan_to_case(
        flow.handoff_snapshot.case,
        flow.handoff_transition,
        flow.diagnosis_state,
        created_job=flow.review_job,
        processed_at=flow.handoff_outcome.produced_at,
    )
    assert reviewing_case.status is CaseStatus.REVIEWING
    assert flow.handoff_snapshot.active_job is not None
    lifecycle = flow.handoff_transition.job_updates[0]
    lifecycle_values = {"status": lifecycle.target_status}
    for field in ("started_at", "finished_at", "runtime_epoch"):
        value = getattr(lifecycle, field)
        if value is not None:
            lifecycle_values[field] = value
    completed_source = rebuild(
        flow.handoff_snapshot.active_job,
        **lifecycle_values,
    )
    active_review = running(flow.review_job)
    aggregate = CaseAggregate(
        case=reviewing_case,
        jobs={
            completed_source.job_id: completed_source,
            active_review.job_id: active_review,
        },
        outcomes={},
        outcome_processing_records={},
        execution_failure_records={},
        attachments={},
        evidence={},
        artifacts={},
    )
    state_payload = json.loads(STATE_FIXTURE.read_text(encoding="utf-8"))
    state_payload["cases"] = {
        flow.source.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(state_payload), flow.outcome, flow.projection


def _running_specialist_failure_state(tmp_path, reason_code: str):
    source, _, projection, outcome = _specialist_terminal(tmp_path, reason_code)
    assert source.context_snapshot is not None
    source = rebuild(
        source,
        context_snapshot=rebuild(source.context_snapshot, evidence_refs=[]),
        evidence_refs=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_refs=[],
    )
    active = snapshot_with_active(source)
    assert active.active_job is not None
    aggregate = CaseAggregate(
        case=active.case,
        jobs={active.active_job.job_id: active.active_job},
        outcomes={},
        outcome_processing_records={},
        execution_failure_records={},
        attachments={},
        evidence={},
        artifacts={},
    )
    state_payload = json.loads(STATE_FIXTURE.read_text(encoding="utf-8"))
    state_payload["cases"] = {
        source.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(state_payload), outcome, projection


def _submit(state, outcome):
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    repository = InMemoryStateRepository(state)
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
        DeterministicIdGenerator(seed="methods-v2-terminal-submission"),
    )
    receipt = submission.submit_outcome(outcome, outcome_ref)
    query = ApplicationQueryService(repository, resources, notifier)
    return receipt, repository, query


def _public_protocol_projections(query, case_id: str):
    mcp = McpAdapter(
        FakeApplicationService(),
        query,
        public_base_url="http://127.0.0.1:18080",
    )
    mcp_result = asyncio.run(
        mcp.call(
            "problem_locator_get_case",
            {
                "case_id": case_id,
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
        rest_result = client.get(f"/api/v1/cases/{case_id}")
    assert mcp_result["ok"] is True
    assert rest_result.status_code == 200
    return (
        mcp_result["data"]["case_view"]["methods_result"],
        rest_result.json()["data"]["case_view"]["methods_result"],
    )


@pytest.mark.parametrize(
    (
        "specialist_verdicts",
        "reviewer_verdicts",
        "expected_status",
        "expected_reason",
    ),
    [
        (
            ("CONFIRMED", "REJECTED"),
            ("CONFIRMED", "REJECTED"),
            CaseStatus.RESOLVED,
            None,
        ),
        (
            ("CONFIRMED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            CaseStatus.UNRESOLVED,
            "SPECIALIST_REVIEWER_DISAGREEMENT",
        ),
        (
            ("UNKNOWN", "REJECTED"),
            ("UNKNOWN", "REJECTED"),
            CaseStatus.UNRESOLVED,
            "INCOMPLETE_EVALUATION",
        ),
        (
            ("REJECTED", "REJECTED"),
            ("REJECTED", "REJECTED"),
            CaseStatus.UNRESOLVED,
            "NO_CONFIRMED_METHOD",
        ),
    ],
)
def test_consensus_terminal_projection_survives_submission_mcp_and_rest(
    tmp_path: Path,
    specialist_verdicts: tuple[str, ...],
    reviewer_verdicts: tuple[str, ...],
    expected_status: CaseStatus,
    expected_reason: str | None,
) -> None:
    state, outcome, projection = _running_review_state(
        tmp_path / "flow",
        specialist_verdicts,
        reviewer_verdicts,
    )
    receipt, repository, query = _submit(state, outcome)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    direct_view = query.get_case(outcome.case_id).case_view
    assert direct_view.status is expected_status
    assert direct_view.methods_result == projection
    assert direct_view.final_result is None
    assert direct_view.unresolved_result is None
    assert direct_view.artifacts == []
    public_json = direct_view.model_dump_json()
    assert "private specialist reason" not in public_json
    assert "private reviewer reason" not in public_json

    aggregate = repository.read_snapshot().cases[outcome.case_id]
    assert aggregate.case.diagnosis_state.candidate_conclusion is None
    assert aggregate.artifacts == {}
    assert aggregate.jobs[outcome.job_id].status is JobStatus.SUCCEEDED
    assert aggregate.outcomes[outcome.outcome_id].methods_reviewer_result is not None

    mcp_projection, rest_projection = _public_protocol_projections(
        query,
        outcome.case_id,
    )
    assert mcp_projection == rest_projection
    assert mcp_projection["result_ref"] == projection.result_ref
    assert mcp_projection["reason_code"] == expected_reason
    assert mcp_projection["diagnostic_id"] == projection.diagnostic_id
    assert mcp_projection["confirmed_method_ids"] == list(
        projection.confirmed_method_ids
    )
    assert "evaluations" not in mcp_projection
    assert "private" not in json.dumps(mcp_projection)


@pytest.mark.parametrize(
    "reason_code",
    [
        "RESOURCE_SNAPSHOT_DRIFT",
        "SERVER_INVARIANT_VIOLATION",
        "AUDIT_ARCHIVE_FAILED",
    ],
)
def test_each_failed_terminal_reason_reaches_case_mcp_and_rest(
    tmp_path: Path,
    reason_code: str,
) -> None:
    state, outcome, projection = _running_specialist_failure_state(
        tmp_path / reason_code.lower(),
        reason_code,
    )
    receipt, repository, query = _submit(state, outcome)
    assert receipt.disposition is OutcomeDisposition.APPLIED
    view = query.get_case(outcome.case_id).case_view
    assert view.status is CaseStatus.FAILED
    assert view.methods_result == projection
    assert view.methods_result.reason_code == reason_code
    assert view.failure is not None
    assert view.artifacts == []
    assert repository.read_snapshot().cases[outcome.case_id].artifacts == {}

    mcp_projection, rest_projection = _public_protocol_projections(
        query,
        outcome.case_id,
    )
    assert mcp_projection == rest_projection
    assert mcp_projection["reason_code"] == reason_code
    assert mcp_projection["diagnostic_id"] == projection.diagnostic_id
    assert mcp_projection["reasons"] == list(projection.reasons)
    assert "evaluations" not in mcp_projection
