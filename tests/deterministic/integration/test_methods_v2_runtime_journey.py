from __future__ import annotations

import io
from pathlib import Path

from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import (
    CaseAggregate,
    Job,
    ResourceRef,
    ResourceKind,
    StateFile,
    validate_outcome_for_job,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.journey import configure_journey
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.methods_records_v2 import read_method_evidence_graph_v2
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
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
from tests.deterministic.integration.test_methods_v2_terminal_submission import (
    _public_protocol_projections,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    _claimed_logparse_job_state_and_resources,
    _json,
    _logparse_catalog,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime_methods_v2 import (
    _EvidenceV2ReviewerBackend,
    _EvidenceV2SpecialistBackend,
)


def _state_with_aggregate(aggregate) -> StateFile:
    value = _json("state.json")
    value["cases"] = {
        aggregate.case.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(value)


def _aligned_aggregate(job: Job, raw: CaseAggregate) -> CaseAggregate:
    snapshot = job.context_snapshot
    assert snapshot is not None and job.skill_ref is not None
    value = raw.model_dump(mode="json")
    value["case"].update(
        {
            "active_job_id": job.job_id,
            "status": "RUNNING",
            "selected_skill_ref": job.skill_ref.model_dump(mode="json"),
            "diagnosis_state": {
                "revision": snapshot.diagnosis_state_revision,
                "problem_spec": snapshot.problem_spec.model_dump(mode="json"),
                "user_facts": [item.model_dump(mode="json") for item in snapshot.user_facts],
                "confirmed_facts": [
                    item.model_dump(mode="json") for item in snapshot.confirmed_facts
                ],
                "active_hypotheses": [
                    item.model_dump(mode="json") for item in snapshot.active_hypotheses
                ],
                "rejected_hypotheses": [
                    item.model_dump(mode="json") for item in snapshot.rejected_hypotheses
                ],
                "open_questions": [
                    item.model_dump(mode="json") for item in snapshot.open_questions
                ],
                "pending_requirements": [
                    item.model_dump(mode="json") for item in snapshot.pending_requirements
                ],
                "evidence_refs": list(snapshot.evidence_refs),
                "candidate_conclusion": None,
            },
        }
    )
    value["jobs"] = {job.job_id: job.model_dump(mode="json")}
    return CaseAggregate.model_validate(value)


def _claim_active_review(repository: InMemoryStateRepository) -> Job:
    state = repository.read_snapshot()
    value = state.model_dump(mode="json")
    aggregate = next(iter(value["cases"].values()))
    review_job_id = aggregate["case"]["active_job_id"]
    review = aggregate["jobs"][review_job_id]
    review.update(
        {
            "status": "RUNNING",
            "started_at": "2026-07-31T08:04:30.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-0000-0000-000000000097",
        }
    )
    repository.seed(StateFile.model_validate(value))
    return repository.read_job(review_job_id)


def test_runtime_submission_reviewer_and_public_projection_are_one_v2_journey(
    tmp_path: Path,
    request,
) -> None:
    journey_stream = io.StringIO()
    configure_journey(stream=journey_stream)
    request.addfinalizer(configure_journey)
    broker_factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(tmp_path / "catalog", broker_factory)
    source_job, raw_aggregate, _ = _claimed_logparse_job_state_and_resources(catalog)
    aggregate = _aligned_aggregate(source_job, raw_aggregate)
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    attachment = next(iter(aggregate.attachments.values()))
    attachment_bytes = b"request timed out while calling inventory\n"
    resources.seed_formal_resource(
        ResourceRef(
            resource_kind=ResourceKind.FILE,
            storage_key=attachment.storage_key,
            size=attachment.size,
            sha256=attachment.sha256,
        ),
        state_reference_count=1,
        payload=attachment_bytes,
    )
    repository = InMemoryStateRepository(_state_with_aggregate(aggregate))
    records = InMemoryExecutionRecordStore()
    pending_source_value = source_job.model_dump(mode="json")
    pending_source_value.update(
        {
            "status": "PENDING",
            "started_at": None,
            "finished_at": None,
            "runtime_epoch": None,
        }
    )
    records.publish_job(Job.model_validate(pending_source_value))
    specialist_backend = _EvidenceV2SpecialistBackend(
        broker_factory,
        source_job,
        ("VALID",),
    )
    specialist_backend.target_contents = {
        "client": b"RPC DEADLINE EXCEEDED request_id=42\n",
        "server": b"CONNECTION POOL WAIT request_id=42\n",
    }

    def execute_preprocessing(
        session: object,
        operation: str,
        request_path: str,
        result_path: str,
    ) -> None:
        assert operation == "parse-targets"
        assert request_path == "output/proposals/methods-preprocess/request.json"
        assert result_path == "output/proposals/methods-preprocess/target_logs.json"
        specialist_backend._run_preprocessing(  # noqa: SLF001 - production-port test fixture
            {"workspace_root": getattr(session, "workspace_root")}
        )
        return None

    broker_factory.preprocessing_executor = execute_preprocessing
    specialist_runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=broker_factory,
        execution_records=records,
        clock=FakeClock("2026-07-31T08:03:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="runtime-journey-specialist"),
        workspace_manager=WorkspaceManager(tmp_path / "specialist-runtime"),
        backend=specialist_backend,
    )

    specialist_receipt = specialist_runtime.execute(
        source_job,
        InMemoryCancellationSignal(),
    )
    assert specialist_receipt.job_outcome.methods_review_target is not None, (
        specialist_receipt.job_outcome.error
    )
    assert specialist_receipt.job_outcome.result_type.value == "COMPLETED"
    assert specialist_receipt.job_outcome.error is None
    graph = read_method_evidence_graph_v2(records, job_id=source_job.job_id)
    assert graph is not None
    assert any(
        hit.marker == "connection pool wait"
        and hit.line == "CONNECTION POOL WAIT request_id=42"
        for hit in graph.hits
    )
    validate_outcome_for_job(
        source_job,
        specialist_receipt.job_outcome,
        aggregate,
    )
    notifier = InMemoryStateChangeNotifier()
    dispatcher = RecordingDispatcher()
    submission = OutcomeSubmissionService(
        repository,
        resources,
        guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        catalog,
        dispatcher,
        notifier,
        FakeClock("2026-07-31T08:04:00.000Z"),
        DeterministicIdGenerator(seed="runtime-journey-submission"),
    )
    handoff = submission.submit_outcome(
        specialist_receipt.job_outcome,
        specialist_receipt.outcome_file_ref,
    )
    assert handoff.disposition.value == "APPLIED"
    assert handoff.case_view.active_job is not None
    assert handoff.case_view.active_job.job_type.value == "REVIEW"
    review_job = _claim_active_review(repository)
    review_events: list[str] = []
    reviewer_backend = _EvidenceV2ReviewerBackend(
        ("VALID_CONFIRMED",),
        review_events,
    )
    reviewer_runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=FakeClock("2026-07-31T08:05:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="runtime-journey-reviewer"),
        workspace_manager=WorkspaceManager(tmp_path / "reviewer-runtime"),
        backend=reviewer_backend,
    )

    reviewer_receipt = reviewer_runtime.execute(
        review_job,
        InMemoryCancellationSignal(),
    )
    terminal = submission.submit_outcome(
        reviewer_receipt.job_outcome,
        reviewer_receipt.outcome_file_ref,
    )

    query = ApplicationQueryService(repository, resources, notifier)
    mcp_result, rest_result, mcp_view, rest_view = _public_protocol_projections(
        query,
        source_job.case_id,
    )
    assert terminal.case_view.status.value == "RESOLVED"
    assert mcp_result == rest_result
    assert mcp_result["status"] == "RESOLVED"
    assert mcp_result["confirmed_method_ids"]
    assert mcp_result["limitations"] == []
    assert "specialist_evaluation" not in mcp_view
    assert "specialist_evaluation" not in rest_view
    assert "private specialist reason" not in str(mcp_view)
    assert "private specialist reason" not in str(rest_view)
    journey = journey_stream.getvalue()
    assert "methods_reviewer_result" not in journey
    assert "Independent blind review of the frozen plan." not in journey
    assert mcp_result["diagnostic_id"] in journey
