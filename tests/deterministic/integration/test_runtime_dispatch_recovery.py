from __future__ import annotations

import collections
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from problem_locator.application import build_application_service
from problem_locator.application.preparation import runtime_bindings_from_job
from problem_locator.contracts import (
    APPLICATION_ERROR_RETRYABLE_CODES,
    ApplicationError,
    ApplicationPortError,
    CancelCase,
    ErrorCode,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    RuntimeBindings,
    RuntimeExecutionReceipt,
    StateFile,
    StateMutation,
    canonical_json_bytes,
)
from problem_locator.dispatch import (
    CancellationController,
    JobWorker,
    RecoveryCoordinator,
    RuntimeEpochContext,
    RuntimeEpochFactory,
    SchedulerService,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.outcome_publisher import OutcomePublisher
from problem_locator.runtime.workspace import WorkspaceManager
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
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryStateChangeNotifier,
    RecordingDispatcher,
)
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings, bindings_from_job
from tests.deterministic.unit.dispatch.fakes import (
    ManualSubmissionBackoff,
    RecordingDispatcher as RecoveryDispatcher,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
EPOCH = "00000000-0000-0000-0000-000000000880"
FIXED_TIME = "2026-07-31T08:08:00.000Z"
RUNTIME_CATALOG = ROOT / "tests/fixtures/components/runtime-catalog"


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _port_error(code: ErrorCode) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=f"S08 injected {code.value}.",
            details=[],
            retryable=code in APPLICATION_ERROR_RETRYABLE_CODES,
        )
    )


class _FaultingStateRepository(JsonFileStateRepository):
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.outcome_commit_faults: collections.deque[ErrorCode] = (
            collections.deque()
        )
        self.outcome_commit_attempts = 0
        super().__init__(*args, **kwargs)

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ):
        if mutation.insert_outcomes:
            self.outcome_commit_attempts += 1
            if self.outcome_commit_faults:
                raise _port_error(self.outcome_commit_faults.popleft())
        return super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )


class _ReadCaseFaultRepository(JsonFileStateRepository):
    def __init__(self, *args: object, read_case_code: ErrorCode, **kwargs: object) -> None:
        self.read_case_code = read_case_code
        self.read_case_calls: list[str] = []
        super().__init__(*args, **kwargs)

    def read_case(self, case_id: str):
        self.read_case_calls.append(case_id)
        raise _port_error(self.read_case_code)


class _RecordingJobControl:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.submit_calls: list[tuple[JobOutcome, object]] = []
        self.operation_log: list[tuple[str, str]] = []

    def claim_job(self, job_id: str, runtime_epoch: str):
        self.operation_log.append(("claim", job_id))
        return self.delegate.claim_job(job_id, runtime_epoch)

    def submit_outcome(self, outcome: JobOutcome, outcome_file_ref):
        self.submit_calls.append((outcome, outcome_file_ref))
        self.operation_log.append(("submit", outcome.job_id))
        return self.delegate.submit_outcome(outcome, outcome_file_ref)

    def report_execution_infrastructure_failure(
        self,
        job_id: str,
        runtime_epoch: str,
        failure_id: str,
        execution_failure,
    ):
        self.operation_log.append(("report", job_id))
        return self.delegate.report_execution_infrastructure_failure(
            job_id,
            runtime_epoch,
            failure_id,
            execution_failure,
        )

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ):
        self.operation_log.append(("interrupt", recovery_id))
        return self.delegate.interrupt_previous_epoch(
            current_runtime_epoch,
            recovery_id,
        )


class _PublishingRuntime:
    def __init__(
        self,
        records: FileExecutionRecordStore,
        outcome: JobOutcome,
        resource_context: object | None = None,
    ) -> None:
        self.publisher = OutcomePublisher(
            records,
            FakeClock(outcome.produced_at),
            DeterministicIdGenerator(seed="s08-failure-runtime"),
        )
        self.outcome = outcome
        self.resource_context = resource_context
        self.calls: list[Job] = []
        self.receipts: list[RuntimeExecutionReceipt] = []

    def execute(self, job, cancellation) -> RuntimeExecutionReceipt:
        self.calls.append(job)
        resource_context = self.resource_context
        if isinstance(resource_context, JsonFileStateRepository):
            resource_context = resource_context.read_case(job.case_id)
        receipt = self.publisher.publish_success(
            job,
            self.outcome,
            resource_context,
        )
        self.receipts.append(receipt)
        return receipt


class _NeverRuntime:
    def __init__(self) -> None:
        self.calls: list[Job] = []

    def execute(self, job, cancellation):
        self.calls.append(job)
        raise AssertionError("recovery must not re-run Runtime")


class _NeverBackend:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *args: object, **kwargs: object):
        self.calls += 1
        raise AssertionError("typed state failure must precede Agent execution")


class _LateDispatcher:
    def __init__(self) -> None:
        self.target: SchedulerService | None = None

    def bind(self, target: SchedulerService) -> None:
        assert self.target is None
        self.target = target

    def submit(self, job_id: str):
        assert self.target is not None
        return self.target.submit(job_id)

    def cancel(self, job_id: str):
        assert self.target is not None
        return self.target.cancel(job_id)


class _BlockingPublishingRuntime(_PublishingRuntime):
    def __init__(self, records: FileExecutionRecordStore, outcome: JobOutcome) -> None:
        super().__init__(records, outcome)
        self.finalized = threading.Event()
        self.release = threading.Event()
        self.observed_cancelled: list[bool] = []

    def execute(self, job, cancellation) -> RuntimeExecutionReceipt:
        self.calls.append(job)
        receipt = self.publisher.publish_success(job, self.outcome)
        self.receipts.append(receipt)
        self.finalized.set()
        if not self.release.wait(2.0):
            raise TimeoutError("S08 cancellation barrier was not released")
        self.observed_cancelled.append(cancellation.is_cancelled())
        return receipt


def _route_catalog(bindings: RuntimeBindings | None = None) -> FakeAssetCatalog:
    route = Job.model_validate(_json("job-route.json"))
    diagnose = Job.model_validate(_json("job-diagnose.json"))
    selected = diagnose.skill_ref
    assert selected is not None
    return FakeAssetCatalog(
        assets=[
            *assets_for_bindings(bindings_from_job(route)),
            *assets_for_bindings(bindings_from_job(diagnose)),
        ],
        diagnose={
            (selected.id, selected.version, selected.content_hash): (
                bindings or runtime_bindings_from_job(diagnose)
            )
        }
    )


def _seed_route_files(
    data_root: Path,
    lock: StorageCoordinationLock,
    guard: InProcessPublicationCommitGuard,
) -> tuple[FileExecutionRecordStore, StateFile, Job]:
    layout = StorageLayout.at(data_root)
    layout.initialize_v2_data_root()
    state = StateFile.model_validate(_json("state.json"))
    route = state.cases[CASE_ID].jobs[ROUTE_JOB_ID]
    records = FileExecutionRecordStore(data_root, lock)
    with guard.acquire():
        records.publish_job(route)
    layout.state.write_bytes(canonical_json_bytes(state))
    return records, state, route


def _route_application(
    data_root: Path,
    repository: JsonFileStateRepository,
    records: FileExecutionRecordStore,
    resources: FileResourceStore,
    guard: InProcessPublicationCommitGuard,
    upload_guard: InProcessAttachmentUploadGuard,
    catalog: FakeAssetCatalog,
    *,
    dispatcher: object | None = None,
    id_seed: str | None = None,
):
    return build_application_service(
        repository=repository,
        resource_store=resources,
        publication_guard=guard,
        upload_guard=upload_guard,
        execution_records=records,
        coordinator=DomainCoordinator(),
        projector=PureContextSnapshotProjector(),
        asset_catalog=catalog,
        dispatcher=dispatcher or RecordingDispatcher(),
        notifier=InMemoryStateChangeNotifier(),
        clock=FakeClock(FIXED_TIME),
        ids=DeterministicIdGenerator(
            seed=id_seed or f"s08-failure-app-{data_root.name}"
        ),
    )


@pytest.mark.parametrize(
    ("faults", "expected_attempts"),
    [
        ([ErrorCode.STATE_WRITE_FAILED], 2),
        ([ErrorCode.REVISION_CONFLICT] * 3, 4),
    ],
    ids=["state-write", "exhausted-revision-conflict"],
)
def test_finalized_outcome_retries_only_submission_with_real_file_records(
    tmp_path: Path,
    faults: list[ErrorCode],
    expected_attempts: int,
) -> None:
    data_root = tmp_path / "data"
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)
    registry = AttachmentUploadRegistry()
    upload_guard = InProcessAttachmentUploadGuard(registry)
    records, _, _ = _seed_route_files(data_root, lock, guard)
    repository = _FaultingStateRepository(
        data_root,
        lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-failure-state"),
        execution_record_store=records,
    )
    repository.outcome_commit_faults.extend(faults)
    resources = FileResourceStore(
        StorageLayout.at(data_root),
        lock,
        registry,
        DeterministicIdGenerator(seed="s08-failure-resources"),
    )
    application = _route_application(
        data_root,
        repository,
        records,
        resources,
        guard,
        upload_guard,
        _route_catalog(),
    )
    observed = _RecordingJobControl(application)
    outcome = JobOutcome.model_validate(_json("job-outcome-route.json"))
    runtime = _PublishingRuntime(records, outcome)
    epoch = RuntimeEpochContext()
    epoch.install(EPOCH)
    backoff = ManualSubmissionBackoff()
    worker = JobWorker(
        observed,
        runtime,
        epoch,
        submission_backoff=backoff,
    )

    result = worker.execute_one(ROUTE_JOB_ID, CancellationController())

    assert result.delivery_completed is True
    assert result.outcome_disposition is OutcomeDisposition.APPLIED
    assert len(runtime.calls) == 1
    assert len(runtime.receipts) == 1
    assert len(observed.submit_calls) == 2
    first_outcome, first_ref = observed.submit_calls[0]
    assert all(
        replayed_outcome is first_outcome and replayed_ref is first_ref
        for replayed_outcome, replayed_ref in observed.submit_calls
    )
    assert repository.outcome_commit_attempts == expected_attempts
    assert backoff.delays == [0.1]
    durable = records.read_published_outcome(ROUTE_JOB_ID)
    assert durable is not None
    assert canonical_json_bytes(durable.job_outcome) == canonical_json_bytes(outcome)
    assert durable.outcome_file_ref == runtime.receipts[0].outcome_file_ref
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.jobs[ROUTE_JOB_ID].status is JobStatus.SUCCEEDED
    assert aggregate.outcome_processing_records[outcome.outcome_id].disposition is (
        OutcomeDisposition.APPLIED
    )
    assert aggregate.case.active_job_id is not None
    next_job = aggregate.jobs[aggregate.case.active_job_id]
    assert canonical_json_bytes(next_job) == (
        data_root / "jobs" / next_job.job_id / "job.json"
    ).read_bytes()


def test_asset_error_parks_scheduler_then_recovery_replays_before_interrupt(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    first_lock = StorageCoordinationLock()
    first_guard = InProcessPublicationCommitGuard(first_lock)
    first_registry = AttachmentUploadRegistry()
    first_upload_guard = InProcessAttachmentUploadGuard(first_registry)
    first_records, _, route = _seed_route_files(
        data_root,
        first_lock,
        first_guard,
    )
    first_repository = JsonFileStateRepository(
        data_root,
        first_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-asset-park-state"),
        execution_record_store=first_records,
    )
    first_resources = FileResourceStore(
        StorageLayout.at(data_root),
        first_lock,
        first_registry,
        DeterministicIdGenerator(seed="s08-asset-park-resources"),
    )
    # Claim has every asset fixed by the ROUTE Job, but the selected skill's
    # DIAGNOSE role is deliberately unavailable during Outcome submission.
    missing_catalog = FakeAssetCatalog(
        assets=assets_for_bindings(bindings_from_job(route)),
    )
    first_application = _route_application(
        data_root,
        first_repository,
        first_records,
        first_resources,
        first_guard,
        first_upload_guard,
        missing_catalog,
    )
    first_control = _RecordingJobControl(first_application)
    route_outcome = JobOutcome.model_validate(_json("job-outcome-route.json"))
    first_runtime = _PublishingRuntime(first_records, route_outcome)
    first_scheduler = SchedulerService(
        first_repository,
        first_records,
        first_control,
        first_runtime,
        DeterministicIdGenerator(seed="s08-asset-park-epoch"),
        submission_backoff=ManualSubmissionBackoff(),
    )

    started = first_scheduler.start()
    assert started.completed is True
    assert first_scheduler.wait_until_idle(2.0) is True
    assert first_scheduler.ready is False
    assert first_scheduler.fatal_worker_error_code is (
        ErrorCode.ASSET_VERSION_UNAVAILABLE
    )
    assert len(first_runtime.calls) == 1
    parked = first_records.read_published_outcome(ROUTE_JOB_ID)
    assert parked is not None
    parked_bytes = canonical_json_bytes(parked.job_outcome)
    parked_state = first_repository.read_snapshot().cases[CASE_ID]
    assert parked_state.jobs[ROUTE_JOB_ID].status is JobStatus.RUNNING
    assert route_outcome.outcome_id not in parked_state.outcome_processing_records
    assert first_scheduler.shutdown(2.0) is True

    restart_lock = StorageCoordinationLock()
    restart_guard = InProcessPublicationCommitGuard(restart_lock)
    restart_registry = AttachmentUploadRegistry()
    restart_records = FileExecutionRecordStore(data_root, restart_lock)
    restart_repository = JsonFileStateRepository(
        data_root,
        restart_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-asset-repair-state"),
        execution_record_store=restart_records,
    )
    restart_resources = FileResourceStore(
        StorageLayout.at(data_root),
        restart_lock,
        restart_registry,
        DeterministicIdGenerator(seed="s08-asset-repair-resources"),
    )
    recovery_dispatcher = RecoveryDispatcher()
    repaired_catalog = _route_catalog()
    restarted_application = _route_application(
        data_root,
        restart_repository,
        restart_records,
        restart_resources,
        restart_guard,
        InProcessAttachmentUploadGuard(restart_registry),
        repaired_catalog,
        dispatcher=recovery_dispatcher,
    )
    restarted_control = _RecordingJobControl(restarted_application)
    recovery = RecoveryCoordinator(
        restart_repository,
        restart_records,
        restarted_control,
        recovery_dispatcher,  # type: ignore[arg-type]
        RuntimeEpochFactory(
            DeterministicIdGenerator(seed="s08-asset-repair-epoch")
        ),
        RuntimeEpochContext(),
        submission_backoff=ManualSubmissionBackoff(),
    )

    recovered = recovery.recover()

    assert recovered.completed is True
    assert recovered.replayed_job_ids == (ROUTE_JOB_ID,)
    assert [name for name, _ in restarted_control.operation_log] == [
        "submit",
        "interrupt",
    ]
    assert recovery_dispatcher.claiming_enabled is True
    assert len(first_runtime.calls) == 1
    replayed = restart_records.read_published_outcome(ROUTE_JOB_ID)
    assert replayed is not None
    assert canonical_json_bytes(replayed.job_outcome) == parked_bytes
    aggregate = restart_repository.read_snapshot().cases[CASE_ID]
    assert aggregate.jobs[ROUTE_JOB_ID].status is JobStatus.SUCCEEDED
    assert aggregate.outcome_processing_records[route_outcome.outcome_id].disposition is (
        OutcomeDisposition.APPLIED
    )
    assert aggregate.case.active_job_id is not None
    assert aggregate.jobs[aggregate.case.active_job_id].status is JobStatus.PENDING


def _alternate_diagnose_bindings(source: RuntimeBindings) -> RuntimeBindings:
    payload = source.model_dump(mode="python")
    profile = payload["agent_profile_ref"]
    profile.update(
        id="specialist-profile-catalog-b",
        version="9.0.0",
        content_hash="9" * 64,
    )
    return RuntimeBindings.model_validate(payload)


def test_recovery_adopts_prepublished_catalog_a_job_without_catalog_b_substitution(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    first_lock = StorageCoordinationLock()
    first_guard = InProcessPublicationCommitGuard(first_lock)
    first_registry = AttachmentUploadRegistry()
    first_records, _, _ = _seed_route_files(data_root, first_lock, first_guard)
    first_repository = _FaultingStateRepository(
        data_root,
        first_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-catalog-a-state"),
        execution_record_store=first_records,
    )
    first_repository.outcome_commit_faults.append(ErrorCode.STATE_WRITE_FAILED)
    first_resources = FileResourceStore(
        StorageLayout.at(data_root),
        first_lock,
        first_registry,
        DeterministicIdGenerator(seed="s08-catalog-a-resources"),
    )
    catalog_a = _route_catalog()
    first_application = _route_application(
        data_root,
        first_repository,
        first_records,
        first_resources,
        first_guard,
        InProcessAttachmentUploadGuard(first_registry),
        catalog_a,
    )
    claimed = first_application.claim_job(ROUTE_JOB_ID, EPOCH)
    assert claimed.claimed is True and claimed.job is not None
    route_outcome = JobOutcome.model_validate(_json("job-outcome-route.json"))
    finalized = OutcomePublisher(
        first_records,
        FakeClock(route_outcome.produced_at),
        DeterministicIdGenerator(seed="s08-catalog-a-runtime"),
    ).publish_success(claimed.job, route_outcome)

    with pytest.raises(ApplicationPortError) as failed_commit:
        first_application.submit_outcome(
            finalized.job_outcome,
            finalized.outcome_file_ref,
        )
    assert failed_commit.value.error.code is ErrorCode.STATE_WRITE_FAILED
    prospective_job_id = DeterministicIdGenerator(
        seed=f"s08-failure-app-{data_root.name}"
    ).derive(
        "job",
        [
            first_repository.read_snapshot().installation_id,
            CASE_ID,
            route_outcome.outcome_id,
            "next_job",
        ],
    )
    prepublished = first_records.read_published_job(prospective_job_id)
    assert prepublished is not None
    catalog_a_bytes = canonical_json_bytes(prepublished.job)
    catalog_a_bindings = runtime_bindings_from_job(prepublished.job)

    restart_lock = StorageCoordinationLock()
    restart_guard = InProcessPublicationCommitGuard(restart_lock)
    restart_registry = AttachmentUploadRegistry()
    restart_records = FileExecutionRecordStore(data_root, restart_lock)
    restart_repository = JsonFileStateRepository(
        data_root,
        restart_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-catalog-b-state"),
        execution_record_store=restart_records,
    )
    restart_resources = FileResourceStore(
        StorageLayout.at(data_root),
        restart_lock,
        restart_registry,
        DeterministicIdGenerator(seed="s08-catalog-b-resources"),
    )
    selected = route_outcome.payload.skill_ref
    assert selected is not None
    catalog_b_bindings = _alternate_diagnose_bindings(catalog_a_bindings)
    catalog_b = FakeAssetCatalog(
        diagnose={
            (selected.id, selected.version, selected.content_hash): (
                catalog_b_bindings
            )
        }
    )
    recovery_dispatcher = RecoveryDispatcher()
    restarted_application = _route_application(
        data_root,
        restart_repository,
        restart_records,
        restart_resources,
        restart_guard,
        InProcessAttachmentUploadGuard(restart_registry),
        catalog_b,
        dispatcher=recovery_dispatcher,
    )
    restarted_control = _RecordingJobControl(restarted_application)
    recovered = RecoveryCoordinator(
        restart_repository,
        restart_records,
        restarted_control,
        recovery_dispatcher,  # type: ignore[arg-type]
        RuntimeEpochFactory(
            DeterministicIdGenerator(seed="s08-catalog-b-epoch")
        ),
        RuntimeEpochContext(),
        submission_backoff=ManualSubmissionBackoff(),
    ).recover()

    assert recovered.completed is True
    assert recovered.replayed_job_ids == (ROUTE_JOB_ID,)
    assert catalog_b.diagnose_calls == []
    adopted = restart_records.read_published_job(prospective_job_id)
    assert adopted is not None
    assert canonical_json_bytes(adopted.job) == catalog_a_bytes
    assert runtime_bindings_from_job(adopted.job) == catalog_a_bindings
    assert runtime_bindings_from_job(adopted.job) != catalog_b_bindings
    aggregate = restart_repository.read_snapshot().cases[CASE_ID]
    assert aggregate.jobs[prospective_job_id] == adopted.job
    assert [name for name, _ in restarted_control.operation_log] == [
        "submit",
        "interrupt",
    ]


def _real_route_catalog(tmp_path: Path) -> VersionedAssetCatalog:
    skill_dir = tmp_path / "skill-dir"
    skill_dir.mkdir()
    shutil.copytree(
        RUNTIME_CATALOG / "skill-dir/manual-triage",
        skill_dir / "manual-triage",
    )
    return VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
    )


def _state_with_catalog_route_job(catalog: VersionedAssetCatalog) -> tuple[StateFile, Job]:
    job_payload = _json("job-route.json")
    job_payload.update(catalog.route_bindings().model_dump(mode="python"))
    job = Job.model_validate(job_payload)
    state_payload = _json("state.json")
    aggregate = state_payload["cases"][CASE_ID]
    aggregate["jobs"] = {job.job_id: job.model_dump(mode="python")}
    aggregate["case"].update(
        active_job_id=job.job_id,
        selected_skill_ref=None,
        status="RUNNING",
    )
    return StateFile.model_validate(state_payload), job


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_real_runtime_state_fault_fails_scheduler_and_next_start_only_interrupts(
    tmp_path: Path,
    code: ErrorCode,
) -> None:
    catalog = _real_route_catalog(tmp_path)
    state, route = _state_with_catalog_route_job(catalog)
    data_root = tmp_path / "data"
    layout = StorageLayout.at(data_root)
    layout.initialize_v2_data_root()
    first_lock = StorageCoordinationLock()
    first_guard = InProcessPublicationCommitGuard(first_lock)
    first_registry = AttachmentUploadRegistry()
    first_records = FileExecutionRecordStore(data_root, first_lock)
    with first_guard.acquire():
        first_records.publish_job(route)
    layout.state.write_bytes(canonical_json_bytes(state))
    first_repository = _ReadCaseFaultRepository(
        data_root,
        first_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed=f"s08-{code.value}-state"),
        execution_record_store=first_records,
        read_case_code=code,
    )
    first_resources = FileResourceStore(
        layout,
        first_lock,
        first_registry,
        DeterministicIdGenerator(seed=f"s08-{code.value}-resources"),
    )
    first_application = _route_application(
        data_root,
        first_repository,
        first_records,
        first_resources,
        first_guard,
        InProcessAttachmentUploadGuard(first_registry),
        catalog,  # type: ignore[arg-type]
    )
    backend = _NeverBackend()
    runtime = DiagnosisRuntime(
        state_repository=first_repository,
        resource_store=first_resources,
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=first_records,
        clock=FakeClock(FIXED_TIME),
        id_generator=DeterministicIdGenerator(seed=f"s08-{code.value}-runtime"),
        workspace_manager=WorkspaceManager(data_root),
        backend=backend,  # type: ignore[arg-type]
    )
    first_scheduler = SchedulerService(
        first_repository,
        first_records,
        first_application,
        runtime,
        DeterministicIdGenerator(seed=f"s08-{code.value}-epoch"),
        submission_backoff=ManualSubmissionBackoff(),
    )

    started = first_scheduler.start()
    assert started.completed is True
    assert first_scheduler.wait_until_idle(2.0) is True
    assert first_scheduler.ready is False
    assert first_scheduler.fatal_worker_error_type == "ApplicationPortError"
    assert first_scheduler.fatal_worker_error_code is code
    assert first_repository.read_case_calls == [CASE_ID]
    assert backend.calls == 0
    assert first_records.read_published_outcome(route.job_id) is None
    failed_state = first_repository.read_snapshot().cases[CASE_ID]
    assert failed_state.jobs[route.job_id].status is JobStatus.RUNNING
    assert first_scheduler.shutdown(2.0) is True

    restart_lock = StorageCoordinationLock()
    restart_guard = InProcessPublicationCommitGuard(restart_lock)
    restart_registry = AttachmentUploadRegistry()
    restart_records = FileExecutionRecordStore(data_root, restart_lock)
    restart_repository = JsonFileStateRepository(
        data_root,
        restart_lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed=f"s08-{code.value}-restart-state"),
        execution_record_store=restart_records,
    )
    restart_resources = FileResourceStore(
        StorageLayout.at(data_root),
        restart_lock,
        restart_registry,
        DeterministicIdGenerator(seed=f"s08-{code.value}-restart-resources"),
    )
    restarted_application = _route_application(
        data_root,
        restart_repository,
        restart_records,
        restart_resources,
        restart_guard,
        InProcessAttachmentUploadGuard(restart_registry),
        catalog,  # type: ignore[arg-type]
    )
    restart_runtime = _NeverRuntime()
    restarted_scheduler = SchedulerService(
        restart_repository,
        restart_records,
        restarted_application,
        restart_runtime,
        DeterministicIdGenerator(seed=f"s08-{code.value}-restart-epoch"),
        submission_backoff=ManualSubmissionBackoff(),
    )

    recovered = restarted_scheduler.start()

    assert recovered.completed is True
    assert recovered.replayed_job_ids == ()
    assert recovered.interrupted_job_ids == (route.job_id,)
    assert recovered.pending_job_ids == ()
    assert restarted_scheduler.ready is True
    assert restart_runtime.calls == []
    recovered_state = restart_repository.read_snapshot().cases[CASE_ID]
    assert recovered_state.jobs[route.job_id].status is JobStatus.INTERRUPTED
    assert recovered_state.outcomes == {}
    assert restarted_scheduler.shutdown(2.0) is True


def test_cancel_commit_wins_barrier_and_late_finalized_outcome_is_stale_once(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    lock = StorageCoordinationLock()
    guard = InProcessPublicationCommitGuard(lock)
    registry = AttachmentUploadRegistry()
    records, _, _ = _seed_route_files(data_root, lock, guard)
    repository = JsonFileStateRepository(
        data_root,
        lock,
        FakeClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-cancel-race-state"),
        execution_record_store=records,
    )
    resources = FileResourceStore(
        StorageLayout.at(data_root),
        lock,
        registry,
        DeterministicIdGenerator(seed="s08-cancel-race-resources"),
    )
    late_dispatcher = _LateDispatcher()
    application = _route_application(
        data_root,
        repository,
        records,
        resources,
        guard,
        InProcessAttachmentUploadGuard(registry),
        _route_catalog(),
        dispatcher=late_dispatcher,
    )
    route_outcome = JobOutcome.model_validate(_json("job-outcome-route.json"))
    runtime = _BlockingPublishingRuntime(records, route_outcome)
    scheduler = SchedulerService(
        repository,
        records,
        application,
        runtime,
        DeterministicIdGenerator(seed="s08-cancel-race-epoch"),
        submission_backoff=ManualSubmissionBackoff(),
    )
    late_dispatcher.bind(scheduler)

    started = scheduler.start()
    assert started.completed is True
    assert runtime.finalized.wait(2.0) is True
    running = repository.read_snapshot().cases[CASE_ID]
    assert running.jobs[ROUTE_JOB_ID].status is JobStatus.RUNNING
    case_revision_before_cancel = running.case.case_revision
    diagnosis_revision = running.case.diagnosis_state.revision

    cancelled = application.execute(
        CancelCase(
            idempotency_key="s08-cancel-finalized-race",
            case_id=CASE_ID,
            expected_case_revision=case_revision_before_cancel,
        )
    )
    assert cancelled.business_receipt.status == "CANCELLED"
    after_cancel = repository.read_snapshot().cases[CASE_ID]
    assert after_cancel.case.case_revision == case_revision_before_cancel + 1
    assert after_cancel.case.diagnosis_state.revision == diagnosis_revision
    assert after_cancel.jobs[ROUTE_JOB_ID].status is JobStatus.CANCELLED

    runtime.release.set()
    assert scheduler.wait_until_idle(2.0) is True

    stale_state = repository.read_snapshot().cases[CASE_ID]
    processing = stale_state.outcome_processing_records[route_outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.STALE
    assert stale_state.case.status.value == "CANCELLED"
    assert stale_state.case.case_revision == after_cancel.case.case_revision + 1
    assert stale_state.case.diagnosis_state.revision == diagnosis_revision
    assert stale_state.jobs[ROUTE_JOB_ID].status is JobStatus.CANCELLED
    assert len(runtime.calls) == 1
    assert runtime.observed_cancelled == [True]

    durable = records.read_published_outcome(ROUTE_JOB_ID)
    assert durable is not None
    before_duplicate = repository.read_snapshot()
    duplicate = application.submit_outcome(
        durable.job_outcome,
        durable.outcome_file_ref,
    )
    after_duplicate = repository.read_snapshot()
    assert duplicate.disposition is OutcomeDisposition.DUPLICATE
    assert after_duplicate == before_duplicate
    assert scheduler.ready is True
    assert scheduler.shutdown(2.0) is True
