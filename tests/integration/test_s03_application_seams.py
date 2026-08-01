from __future__ import annotations

from pathlib import Path
import json

from problem_locator.application import build_application_service
from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    ApplicationCommandPort,
    CaseStatus,
    CreateCase,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    RuntimeBindings,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.resource_store import FileResourceStore
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.contracts.fakes import (
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


ROOT = Path(__file__).resolve().parents[2]
CASE_ID = "00000000-0000-0000-0000-000000000801"
TRIGGER_ID = "00000000-0000-0000-0000-000000000802"
JOB_ID = "00000000-0000-0000-0000-000000000803"
FIXED_TIME = "2026-07-31T08:03:00.000Z"
CONTRACT_CASE_ID = "00000000-0000-0000-0000-000000000001"
CONTRACT_JOB_ID = "00000000-0000-0000-0000-000000000010"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000090"


def _route_bindings() -> RuntimeBindings:
    route = Job.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/job-route.json").read_text()
    )
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


class _ObservedExecutionRecords(FileExecutionRecordStore):
    def __init__(self, *args, publication_guard, **kwargs) -> None:
        self.publication_guard = publication_guard
        self.publication_observations: list[bool] = []
        super().__init__(*args, **kwargs)

    def publish_job(self, job):
        self.publication_observations.append(
            self.publication_guard.held_by_current_thread()
        )
        return super().publish_job(job)


class _ObservedStateRepository(JsonFileStateRepository):
    def __init__(self, *args, publication_guard, **kwargs) -> None:
        self.publication_guard = publication_guard
        self.publication_observations: list[bool] = []
        super().__init__(*args, **kwargs)

    def commit(self, expected_generation, expected_case_revision, mutation):
        self.publication_observations.append(
            self.publication_guard.held_by_current_thread()
        )
        return super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )


class _LeaseCheckingMemoryRecords(InMemoryExecutionRecordStore):
    def __init__(self, guard: InMemoryPublicationCommitGuard) -> None:
        super().__init__()
        self.guard = guard
        self.publication_observations: list[bool] = []

    def publish_job(self, job):
        self.publication_observations.append(self.guard.held_by_current_thread())
        return super().publish_job(job)


def _create_command() -> CreateCase:
    return CreateCase(
        idempotency_key="s08-create-rpc-timeout",
        problem_spec={
            "statement": "Payment to inventory RPC times out",
            "expected_behavior": "The inventory RPC returns successfully",
            "actual_behavior": "The payment service observes a timeout",
            "scope": "payment-service to inventory-service",
            "goals": ["Locate the evidence-backed cause"],
            "non_goals": [],
            "constraints": ["Use the frozen runtime bindings"],
            "completion_criteria": ["A reviewed conclusion is available"],
        },
        initial_user_facts=[],
        wait_seconds=0,
    )


def _running_route_state() -> StateFile:
    payload = json.loads(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text()
    )
    aggregate = payload["cases"][CONTRACT_CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["jobs"][CONTRACT_JOB_ID].update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:01:00.000Z",
        runtime_epoch=RUNTIME_EPOCH,
    )
    return StateFile.model_validate(payload)


def test_domain_plan_is_fully_committed_through_real_file_adapters(
    tmp_path,
) -> None:
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.ensure_directories()
    coordination_lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(coordination_lock)
    attachment_registry = AttachmentUploadRegistry()
    attachment_guard = InProcessAttachmentUploadGuard(attachment_registry)
    storage_ids = DeterministicIdGenerator(seed="s08-s03-storage")
    execution_records = _ObservedExecutionRecords(
        data_root,
        coordination_lock,
        publication_guard=publication_guard,
        temp_token_factory=lambda: "s08-record-temp",
    )
    repository = _ObservedStateRepository(
        data_root,
        coordination_lock,
        FakeClock(FIXED_TIME),
        storage_ids,
        execution_record_store=execution_records,
        publication_guard=publication_guard,
    )
    resources = FileResourceStore(
        layout,
        coordination_lock,
        attachment_registry,
        storage_ids,
    )
    dispatcher = RecordingDispatcher()
    app_ids = DeterministicIdGenerator(
        scripted_ids={
            "case": [CASE_ID],
            "trigger": [TRIGGER_ID],
            "job": [JOB_ID],
        }
    )
    service = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=publication_guard,
        upload_guard=attachment_guard,
        execution_records=execution_records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=FakeAssetCatalog(route=_route_bindings()),
        dispatcher=dispatcher,
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(FIXED_TIME),
        ids=app_ids,
    )

    first = service.execute(_create_command())
    replay = service.execute(_create_command())

    assert isinstance(service, ApplicationCommandPort)
    assert replay.business_receipt == first.business_receipt
    state = repository.read_snapshot()
    aggregate = state.cases[CASE_ID]
    job = aggregate.jobs[JOB_ID]
    assert aggregate.case.active_job_id == JOB_ID
    assert aggregate.case.case_revision == 1
    assert aggregate.case.diagnosis_state.revision == 1
    assert job.status is JobStatus.PENDING
    assert job.context_snapshot == PureContextSnapshotProjector().project(
        aggregate.case.diagnosis_state
    )
    assert execution_records.publication_observations == [True]
    assert repository.publication_observations == [True]
    assert dispatcher.submit_calls == [JOB_ID, JOB_ID]
    assert canonical_json_bytes(job) == (
        data_root / "jobs" / JOB_ID / "job.json"
    ).read_bytes()

    restarted = JsonFileStateRepository(
        data_root,
        coordination_lock,
        FakeClock(FIXED_TIME),
        storage_ids,
        execution_record_store=execution_records,
    )
    assert restarted.read_snapshot() == state
    assert canonical_json_bytes(restarted.read_job(JOB_ID)) == canonical_json_bytes(job)


def test_route_outcome_and_projected_next_job_share_one_commit() -> None:
    state = _running_route_state()
    route_outcome = JobOutcome.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/job-outcome-route.json").read_text()
    )
    diagnose_template = Job.model_validate_json(
        (ROOT / "tests/fixtures/contracts/positive/job-diagnose.json").read_text()
    )
    diagnose_bindings = runtime_bindings_from_job(diagnose_template)
    skill_ref = route_outcome.payload.skill_ref
    assert skill_ref is not None
    catalog = FakeAssetCatalog(
        diagnose={
            (skill_ref.id, skill_ref.version, skill_ref.content_hash): diagnose_bindings
        }
    )
    guard = InMemoryPublicationCommitGuard()
    records = _LeaseCheckingMemoryRecords(guard)
    outcome_ref = records.publish_outcome_bytes(
        route_outcome.job_id,
        canonical_json_bytes(route_outcome),
    )
    repository = InMemoryStateRepository(state)
    resources = InMemoryResourceStore(publication_guard=guard)
    dispatcher = RecordingDispatcher()
    service = OutcomeSubmissionService(
        repository,
        resources,
        guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        catalog,
        dispatcher,
        InMemoryStateChangeNotifier(),
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-s03-outcome"),
    )

    receipt = service.submit_outcome(route_outcome, outcome_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    assert len(repository.commit_calls) == 1
    mutation = repository.commit_calls[0][2]
    assert mutation.insert_outcomes == [route_outcome]
    assert len(mutation.insert_jobs) == 1
    next_job = mutation.insert_jobs[0]
    committed = repository.read_snapshot().cases[CONTRACT_CASE_ID]
    assert committed.case.status is CaseStatus.RUNNING
    assert committed.case.active_job_id == next_job.job_id
    assert committed.jobs[CONTRACT_JOB_ID].status is JobStatus.SUCCEEDED
    assert next_job.previous_outcome_refs == [route_outcome.outcome_id]
    assert next_job.context_snapshot == PureContextSnapshotProjector().project(
        committed.case.diagnosis_state
    )
    assert records.publication_observations == [True]
    assert dispatcher.submit_calls == [next_job.job_id]
