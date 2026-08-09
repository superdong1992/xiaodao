from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from problem_locator.application import build_application_service
from problem_locator.contracts import (
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    RuntimeExecutionReceipt,
    StateFile,
)
from problem_locator.dispatch import CancellationController, JobWorker, RuntimeEpochContext
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.journey import configure_journey
from problem_locator.runtime.outcome_publisher import OutcomePublisher
from tests.deterministic.contracts.fakes import (
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
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings, bindings_from_job


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000805"
FIXED_TIME = "2026-07-31T08:05:00.000Z"


@pytest.fixture
def journey_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_journey(stream=stream)
    yield stream
    configure_journey()


class _PublishingRuntime:
    def __init__(
        self,
        records: InMemoryExecutionRecordStore,
        outcome: JobOutcome,
    ) -> None:
        self.publisher = OutcomePublisher(
            records,
            FakeClock(FIXED_TIME),
            DeterministicIdGenerator(seed="s08-s05-runtime"),
        )
        self.outcome = outcome
        self.calls: list[tuple[Job, object]] = []

    def execute(self, job, cancellation) -> RuntimeExecutionReceipt:
        self.calls.append((job, cancellation))
        assert cancellation.is_cancelled() is False
        return self.publisher.publish_success(job, self.outcome)


def _state() -> StateFile:
    return StateFile.model_validate(
        json.loads((FIXTURES / "state.json").read_text())
    )


def _route_outcome() -> JobOutcome:
    payload = json.loads((FIXTURES / "job-outcome-route.json").read_text())
    payload["produced_at"] = FIXED_TIME
    return JobOutcome.model_validate(payload)


def test_worker_claims_executes_once_and_commits_the_finalized_outcome(
    journey_stream: io.StringIO,
) -> None:
    state = _state()
    route_job = state.cases[CASE_ID].jobs[ROUTE_JOB_ID]
    diagnose_template = Job.model_validate_json(
        (FIXTURES / "job-diagnose.json").read_text()
    )
    route_bindings = bindings_from_job(route_job)
    diagnose_bindings = bindings_from_job(diagnose_template)
    skill_ref = diagnose_bindings.skill_ref
    assert skill_ref is not None
    catalog = FakeAssetCatalog(
        assets=[
            *assets_for_bindings(route_bindings),
            *assets_for_bindings(diagnose_bindings),
        ],
        diagnose={
            (skill_ref.id, skill_ref.version, skill_ref.content_hash): diagnose_bindings
        },
    )
    repository = InMemoryStateRepository(state)
    guard = InMemoryPublicationCommitGuard()
    upload_guard = InMemoryAttachmentUploadGuard()
    resources = InMemoryResourceStore(
        upload_guard=upload_guard,
        publication_guard=guard,
    )
    records = InMemoryExecutionRecordStore()
    dispatcher = RecordingDispatcher()
    application = build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,
        dispatcher=dispatcher,
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(FIXED_TIME),
        ids=DeterministicIdGenerator(seed="s08-s05-application"),
    )
    runtime = _PublishingRuntime(records, _route_outcome())
    epoch = RuntimeEpochContext()
    epoch.install(RUNTIME_EPOCH)
    worker = JobWorker(application, runtime, epoch)

    result = worker.execute_one(ROUTE_JOB_ID, CancellationController())

    journey_events = [
        json.loads(line) for line in journey_stream.getvalue().splitlines()
    ]
    assert [event["event"] for event in journey_events] == [
        "job.claimed",
        "job.outcome.applied",
        "job.pending_persisted",
        "job.queued",
    ]
    assert all(event["case_id"] == CASE_ID for event in journey_events)
    assert journey_events[0]["job_id"] == ROUTE_JOB_ID
    assert journey_events[1]["outcome_id"] == runtime.outcome.outcome_id
    assert journey_events[2]["job_id"] == journey_events[3]["job_id"]
    assert journey_events[2]["job_type"] == "DIAGNOSE"

    assert result.claimed is True
    assert result.runtime_called is True
    assert result.delivery_completed is True
    assert result.outcome_disposition is OutcomeDisposition.APPLIED
    assert len(runtime.calls) == 1
    claimed_job = runtime.calls[0][0]
    assert claimed_job.status is JobStatus.RUNNING
    assert claimed_job.runtime_epoch == RUNTIME_EPOCH
    assert len(records.publish_outcome_calls) == 1

    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.jobs[ROUTE_JOB_ID].status is JobStatus.SUCCEEDED
    assert aggregate.outcomes[runtime.outcome.outcome_id] == runtime.outcome
    assert aggregate.case.active_job_id is not None
    next_job = aggregate.jobs[aggregate.case.active_job_id]
    assert next_job.status is JobStatus.PENDING
    assert next_job.previous_outcome_refs == [runtime.outcome.outcome_id]
    assert next_job.context_snapshot == PureContextSnapshotProjector().project(
        aggregate.case.diagnosis_state
    )
    assert dispatcher.submit_calls == [next_job.job_id]
