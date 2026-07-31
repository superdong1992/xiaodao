from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from problem_locator.application.job_control import JobControlService
from problem_locator.application.mutations import build_state_mutation
from problem_locator.application.preparation import fixed_asset_refs
from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    AssetKind,
    CaseFailure,
    CaseFailureUpdate,
    CaseStatus,
    DiagnosisStateDelta,
    ErrorCode,
    ERROR_SPECS,
    ExecutionFailure,
    ExecutionStage,
    FailureReportDisposition,
    FieldUpdateAction,
    JobLifecycleUpdate,
    JobStatus,
    ResolvedAsset,
    StateFile,
    TransitionPlan,
    TriggerType,
)
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryPublicationCommitGuard,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    ScriptedCoordinator,
)


ROOT = Path(__file__).resolve().parents[3]
STATE_FIXTURE = ROOT / "tests/fixtures/contracts/positive/state.json"

CASE_ID = "00000000-0000-0000-0000-000000000001"
SECOND_CASE_ID = "00000000-0000-0000-0000-000000000002"
JOB_ID = "00000000-0000-0000-0000-000000000010"
SECOND_JOB_ID = "00000000-0000-0000-0000-000000000020"
FAILURE_ID = "00000000-0000-0000-0000-000000000030"
RECOVERY_ID = "00000000-0000-0000-0000-000000000040"
OTHER_RECOVERY_ID = "00000000-0000-0000-0000-000000000041"
OLD_EPOCH = "00000000-0000-0000-0000-000000000050"
CURRENT_EPOCH = "00000000-0000-0000-0000-000000000051"
NOW = "2026-07-31T00:10:00.000Z"


def _state() -> StateFile:
    return StateFile.model_validate(
        json.loads(STATE_FIXTURE.read_text(encoding="utf-8"))
    )


def _running_state(*, runtime_epoch: str = OLD_EPOCH) -> StateFile:
    payload = _state().model_dump(mode="python")
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        updated_at="2026-07-31T00:05:00.000Z",
    )
    aggregate["jobs"][JOB_ID].update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:05:00.000Z",
        runtime_epoch=runtime_epoch,
    )
    return StateFile.model_validate(payload)


def _recovery_state() -> StateFile:
    payload = _state().model_dump(mode="python")
    first = payload["cases"][CASE_ID]
    second = copy.deepcopy(first)
    second["case"].update(
        case_id=SECOND_CASE_ID,
        active_job_id=SECOND_JOB_ID,
    )
    job = second["jobs"].pop(JOB_ID)
    job.update(job_id=SECOND_JOB_ID, case_id=SECOND_CASE_ID)
    second["jobs"][SECOND_JOB_ID] = job
    payload["cases"][SECOND_CASE_ID] = second

    first["case"].update(
        case_revision=2,
        updated_at="2026-07-31T00:05:00.000Z",
    )
    first["jobs"][JOB_ID].update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:05:00.000Z",
        runtime_epoch=OLD_EPOCH,
    )
    return StateFile.model_validate(payload)


def _empty_delta() -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def _control_plan(snapshot, trigger, target: JobStatus) -> TransitionPlan:
    job = snapshot.active_job
    assert job is not None
    failure_update = None
    if target is JobStatus.FAILED:
        if trigger.trigger_type is TriggerType.ASSET_VERSION_UNAVAILABLE:
            code = ErrorCode.ASSET_VERSION_UNAVAILABLE
            message = "A fixed runtime asset is unavailable."
        else:
            code = trigger.payload.execution_failure.code
            message = trigger.payload.execution_failure.message
        failure_update = CaseFailureUpdate(
            action=FieldUpdateAction.SET,
            value=CaseFailure(
                code=code,
                message=message,
                source_job_id=job.job_id,
                source_outcome_id=None,
                occurred_at=trigger.occurred_at,
            ),
        )
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=(
            CaseStatus.FAILED
            if target is JobStatus.FAILED
            else CaseStatus.INTERRUPTED
        ),
        job_updates=[
            JobLifecycleUpdate(
                job_id=job.job_id,
                expected_status=job.status,
                target_status=target,
                started_at=None,
                finished_at=trigger.occurred_at,
                runtime_epoch=None,
            )
        ],
        outcome_disposition=None,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=failure_update,
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Apply the validated Job control transition.",
    )


def _coordinator_for(target: JobStatus) -> ScriptedCoordinator:
    return ScriptedCoordinator(
        [lambda snapshot, trigger: _control_plan(snapshot, trigger, target)]
    )


def _available_catalog(state: StateFile) -> FakeAssetCatalog:
    job = state.cases[CASE_ID].jobs[JOB_ID]
    return FakeAssetCatalog(
        assets=[
            ResolvedAsset(
                ref=ref,
                asset_kind=AssetKind.AGENT_PROFILE,
                root_path=f"/virtual/assets/{index}",
            )
            for index, ref in enumerate(fixed_asset_refs(job))
        ]
    )


def _execution_failure(*, message: str = "Outcome publication failed.") -> ExecutionFailure:
    return ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message=message,
        retryable=True,
        details=[],
    )


def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


class _ReadFailsAfterCommitRepository(InMemoryStateRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.fail_next_read = False

    def read_snapshot(self) -> StateFile:
        if self.fail_next_read:
            self.fail_next_read = False
            raise _port_error(ErrorCode.STATE_CORRUPT, "injected post-commit failure")
        return super().read_snapshot()

    def commit(self, expected_generation, expected_case_revision, mutation):
        receipt = super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )
        self.fail_next_read = True
        return receipt


class _CountingRepository(InMemoryStateRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.read_snapshot_calls = 0

    def read_snapshot(self) -> StateFile:
        self.read_snapshot_calls += 1
        return super().read_snapshot()


def _service(
    state: StateFile,
    coordinator: ScriptedCoordinator,
    *,
    catalog: FakeAssetCatalog | None = None,
    repository: InMemoryStateRepository | None = None,
):
    repository = repository or InMemoryStateRepository(state)
    guard = InMemoryPublicationCommitGuard()
    notifier = InMemoryStateChangeNotifier()
    clock = FakeClock(NOW)
    ids = DeterministicIdGenerator()
    service = JobControlService(
        repository=repository,
        publication_guard=guard,
        coordinator=coordinator,
        asset_catalog=catalog or _available_catalog(state),
        notifier=notifier,
        clock=clock,
        ids=ids,
    )
    return service, repository, guard, notifier, clock, ids


def test_claim_checks_every_fixed_asset_then_commits_running_job_under_short_lease() -> None:
    state = _state()
    coordinator = ScriptedCoordinator()
    catalog = _available_catalog(state)
    service, repository, guard, notifier, clock, _ = _service(
        state,
        coordinator,
        catalog=catalog,
    )

    receipt = service.claim_job(JOB_ID, CURRENT_EPOCH)

    assert receipt.claimed is True
    assert receipt.job is not None
    assert receipt.job.status is JobStatus.RUNNING
    assert receipt.job.runtime_epoch == CURRENT_EPOCH
    assert receipt.job.started_at == NOW
    assert receipt.failure_applied is False
    assert list(catalog.check_calls[0]) == fixed_asset_refs(state.cases[CASE_ID].jobs[JOB_ID])
    assert coordinator.calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert not guard.held_by_current_thread()
    committed = repository.read_snapshot()
    aggregate = committed.cases[CASE_ID]
    assert aggregate.case.case_revision == 2
    assert aggregate.case.diagnosis_state.revision == 1
    assert aggregate.jobs[JOB_ID] == receipt.job
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert clock.calls == 1


def test_claim_missing_asset_uses_coordinator_failure_plan_without_claiming() -> None:
    state = _state()
    coordinator = _coordinator_for(JobStatus.FAILED)
    service, repository, guard, notifier, _, _ = _service(
        state,
        coordinator,
        catalog=FakeAssetCatalog(),
    )

    receipt = service.claim_job(JOB_ID, CURRENT_EPOCH)

    assert receipt.claimed is False
    assert receipt.failure_applied is True
    assert receipt.failure_code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert len(coordinator.calls) == 1
    trigger = coordinator.calls[0][1]
    assert trigger.trigger_type is TriggerType.ASSET_VERSION_UNAVAILABLE
    assert trigger.payload.source_job_id == JOB_ID
    assert trigger.payload.missing_refs == fixed_asset_refs(
        state.cases[CASE_ID].jobs[JOB_ID]
    )
    committed = repository.read_snapshot().cases[CASE_ID]
    assert committed.jobs[JOB_ID].status is JobStatus.FAILED
    assert committed.case.status is CaseStatus.FAILED
    assert committed.case.failure.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    assert committed.case.diagnosis_state.revision == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]


def test_claim_returns_the_normal_rejection_receipt_for_a_non_pending_job() -> None:
    state = _running_state(runtime_epoch=CURRENT_EPOCH)
    coordinator = ScriptedCoordinator()
    catalog = _available_catalog(state)
    service, repository, guard, _, clock, ids = _service(
        state,
        coordinator,
        catalog=catalog,
    )

    receipt = service.claim_job(JOB_ID, CURRENT_EPOCH)

    assert receipt.claimed is False
    assert receipt.job is None
    assert receipt.failure_applied is False
    assert receipt.failure_code is None
    assert repository.commit_calls == []
    assert catalog.check_calls == []
    assert coordinator.calls == []
    assert guard.acquire_calls == 0
    assert clock.calls == 0
    assert ids.new_calls == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.INVALID_CASE_STATE, ErrorCode.VALIDATION_ERROR],
)
def test_claim_coordinator_rejection_uses_the_same_normal_receipt_channel(
    code: ErrorCode,
) -> None:
    state = _state()
    decision = ApplicationError(
        code=code,
        message="The asset-unavailable transition is not applicable.",
        details=[],
        retryable=False,
    )
    coordinator = ScriptedCoordinator([decision])
    service, repository, guard, notifier, _, _ = _service(
        state,
        coordinator,
        catalog=FakeAssetCatalog(),
    )

    receipt = service.claim_job(JOB_ID, CURRENT_EPOCH)

    assert receipt.claimed is False
    assert receipt.job is None
    assert receipt.failure_applied is False
    assert receipt.failure_code is None
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1].trigger_type is TriggerType.ASSET_VERSION_UNAVAILABLE
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert notifier.notify_calls == []


@pytest.mark.parametrize(
    ("operation", "target_status"),
    [
        ("claim", JobStatus.FAILED),
        ("report", JobStatus.FAILED),
        ("recovery", JobStatus.INTERRUPTED),
    ],
)
@pytest.mark.parametrize("invalid_output", ["decision", "plan"])
def test_invalid_job_control_coordinator_output_uses_typed_validation_error(
    operation: str,
    target_status: JobStatus,
    invalid_output: str,
) -> None:
    state = _recovery_state() if operation == "recovery" else (
        _running_state() if operation == "report" else _state()
    )

    def invalid_result(snapshot, trigger):
        if invalid_output == "decision":
            return ApplicationError(
                code=ErrorCode.ACTIVE_JOB_EXISTS,
                message="This error code is not legal for the control Trigger.",
                details=[],
                retryable=False,
            )
        return _control_plan(snapshot, trigger, target_status).model_copy(
            update={"clear_active_job": False}
        )

    coordinator = ScriptedCoordinator([invalid_result])
    catalog = FakeAssetCatalog() if operation == "claim" else _available_catalog(state)
    service, repository, guard, notifier, _, _ = _service(
        state,
        coordinator,
        catalog=catalog,
    )

    with pytest.raises(ApplicationPortError) as captured:
        if operation == "claim":
            service.claim_job(JOB_ID, CURRENT_EPOCH)
        elif operation == "report":
            service.report_execution_infrastructure_failure(
                JOB_ID,
                OLD_EPOCH,
                FAILURE_ID,
                _execution_failure(),
            )
        else:
            service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    assert captured.value.error.code is ErrorCode.VALIDATION_ERROR
    expected_audit_commits = 1 if operation == "recovery" else 0
    assert len(repository.commit_calls) == expected_audit_commits
    assert guard.acquire_calls == guard.release_calls == expected_audit_commits
    assert notifier.notify_calls == []
    assert len(coordinator.calls) == 1
    if operation == "recovery":
        record = repository.read_snapshot().recovery_processing_records[RECOVERY_ID]
        assert record.completed_at is None


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("claim_job", ("not-a-job-id", CURRENT_EPOCH)),
        ("claim_job", (JOB_ID, "not-a-runtime-epoch")),
        ("claim_job", (True, CURRENT_EPOCH)),
        ("interrupt_previous_epoch", ("not-a-runtime-epoch", RECOVERY_ID)),
        ("interrupt_previous_epoch", (CURRENT_EPOCH, "not-a-recovery-id")),
        ("interrupt_previous_epoch", (CURRENT_EPOCH, 1.5)),
    ],
)
def test_raw_job_control_input_is_rebuilt_before_any_dependency_call(
    operation: str,
    args: tuple[object, ...],
) -> None:
    state = _state()
    repository = _CountingRepository(state)
    coordinator = ScriptedCoordinator()
    catalog = _available_catalog(state)
    service, _, guard, notifier, clock, ids = _service(
        state,
        coordinator,
        catalog=catalog,
        repository=repository,
    )

    with pytest.raises(ApplicationPortError) as captured:
        getattr(service, operation)(*args)

    assert captured.value.error.code is ErrorCode.VALIDATION_ERROR
    assert captured.value.error.retryable is False
    assert repository.read_snapshot_calls == 0
    assert repository.commit_calls == []
    assert catalog.check_calls == []
    assert coordinator.calls == []
    assert guard.acquire_calls == 0
    assert notifier.notify_calls == []
    assert clock.calls == 0
    assert ids.new_calls == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("claim_job", (JOB_ID, CURRENT_EPOCH)),
        (
            "report_execution_infrastructure_failure",
            (JOB_ID, OLD_EPOCH, FAILURE_ID, _execution_failure()),
        ),
        ("interrupt_previous_epoch", (CURRENT_EPOCH, RECOVERY_ID)),
    ],
)
def test_job_control_preserves_the_exact_precommit_state_read_fault(
    code: ErrorCode,
    operation: str,
    args: tuple[object, ...],
) -> None:
    state = _running_state()
    repository = InMemoryStateRepository(state)
    failure = _port_error(code, "injected state read failure")
    repository.inject_read_failure("read_snapshot", failure)
    coordinator = ScriptedCoordinator()
    catalog = _available_catalog(state)
    service, _, guard, notifier, clock, ids = _service(
        state,
        coordinator,
        catalog=catalog,
        repository=repository,
    )

    with pytest.raises(ApplicationPortError) as captured:
        getattr(service, operation)(*args)

    assert captured.value is failure
    assert repository.commit_calls == []
    assert catalog.check_calls == []
    assert coordinator.calls == []
    assert guard.acquire_calls == 0
    assert notifier.notify_calls == []
    assert clock.calls == 0
    assert ids.new_calls == []


def test_claim_propagates_the_exact_repository_port_error_and_releases_lease() -> None:
    state = _state()
    service, repository, guard, _, _, _ = _service(
        state,
        ScriptedCoordinator(),
    )
    failure = _port_error(ErrorCode.STATE_WRITE_FAILED, "State write failed.")
    repository.fail_next_commit(failure)

    with pytest.raises(ApplicationPortError) as captured:
        service.claim_job(JOB_ID, CURRENT_EPOCH)

    assert captured.value is failure
    assert guard.acquire_calls == guard.release_calls == 1
    assert not guard.held_by_current_thread()


def test_execution_failure_is_applied_once_then_exactly_replayed_as_duplicate() -> None:
    state = _running_state()
    coordinator = _coordinator_for(JobStatus.INTERRUPTED)
    service, repository, guard, notifier, _, _ = _service(state, coordinator)
    failure = _execution_failure()

    applied = service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        failure,
    )
    generation_after_apply = repository.read_snapshot().generation
    duplicate = service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        failure,
    )

    assert applied.disposition is FailureReportDisposition.APPLIED
    assert applied.case_view.status is CaseStatus.INTERRUPTED
    assert duplicate.disposition is FailureReportDisposition.DUPLICATE
    assert duplicate.case_view.case_revision == applied.case_view.case_revision
    assert repository.read_snapshot().generation == generation_after_apply
    record = repository.read_snapshot().cases[CASE_ID].execution_failure_records[FAILURE_ID]
    assert record.failure == failure
    assert record.recorded_at == NOW
    assert len(coordinator.calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, generation_after_apply)]


def test_post_commit_state_read_failure_cannot_replace_applied_failure_receipt() -> None:
    state = _running_state()
    repository = _ReadFailsAfterCommitRepository(state)
    service, _, guard, notifier, _, _ = _service(
        state,
        _coordinator_for(JobStatus.INTERRUPTED),
        repository=repository,
    )

    receipt = service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        _execution_failure(),
    )

    assert receipt.disposition is FailureReportDisposition.APPLIED
    assert receipt.case_view.status is CaseStatus.INTERRUPTED
    assert repository.fail_next_read is True
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls
    repository.fail_next_read = False
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert FAILURE_ID in aggregate.execution_failure_records


def test_execution_failure_conflict_and_stale_report_are_zero_write_results() -> None:
    running = _running_state()
    coordinator = _coordinator_for(JobStatus.INTERRUPTED)
    service, repository, _, _, _, _ = _service(running, coordinator)
    first = _execution_failure()
    service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        first,
    )
    generation = repository.read_snapshot().generation

    with pytest.raises(ApplicationPortError) as captured:
        service.report_execution_infrastructure_failure(
            JOB_ID,
            OLD_EPOCH,
            FAILURE_ID,
            _execution_failure(message="Different canonical content."),
        )
    assert captured.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert repository.read_snapshot().generation == generation

    stale_service, stale_repository, stale_guard, _, _, _ = _service(
        _state(),
        ScriptedCoordinator(),
    )
    stale = stale_service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        first,
    )
    assert stale.disposition is FailureReportDisposition.STALE
    assert stale_repository.commit_calls == []
    assert stale_guard.acquire_calls == 0


def test_execution_failure_consumes_coordinator_application_error_union() -> None:
    state = _running_state()
    decision = ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message="The validated Trigger was rejected.",
        details=[],
        retryable=False,
    )
    service, repository, guard, _, _, _ = _service(
        state,
        ScriptedCoordinator([decision]),
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.report_execution_infrastructure_failure(
            JOB_ID,
            OLD_EPOCH,
            FAILURE_ID,
            _execution_failure(),
        )

    assert captured.value.error == decision
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0


def test_execution_failure_coordinator_state_race_returns_fresh_stale_receipt() -> None:
    state = _running_state()
    repository = _CountingRepository(state)
    decision = ApplicationError(
        code=ErrorCode.INVALID_CASE_STATE,
        message="The Case state advanced before the failure was applied.",
        details=[],
        retryable=False,
    )
    coordinator = ScriptedCoordinator([decision])
    service, _, guard, notifier, _, _ = _service(
        state,
        coordinator,
        repository=repository,
    )

    receipt = service.report_execution_infrastructure_failure(
        JOB_ID,
        OLD_EPOCH,
        FAILURE_ID,
        _execution_failure(),
    )

    assert receipt.failure_id == FAILURE_ID
    assert receipt.disposition is FailureReportDisposition.STALE
    assert receipt.case_view.case_id == CASE_ID
    assert repository.read_snapshot_calls == 2
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert notifier.notify_calls == []


def test_recovery_persists_exact_lists_interrupts_each_case_and_replays_receipt() -> None:
    state = _recovery_state()
    coordinator = _coordinator_for(JobStatus.INTERRUPTED)
    service, repository, guard, notifier, clock, _ = _service(state, coordinator)

    receipt = service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    assert receipt.recovery_id == RECOVERY_ID
    assert receipt.interrupted_job_ids == [JOB_ID]
    assert receipt.pending_job_ids == [SECOND_JOB_ID]
    committed = repository.read_snapshot()
    processing = committed.recovery_processing_records[RECOVERY_ID]
    runtime = next(item for item in committed.runtime_epochs if item.recovery_id == RECOVERY_ID)
    assert processing.completed_at == NOW
    assert runtime.recovery_completed_at == NOW
    assert processing.interrupted_job_ids == receipt.interrupted_job_ids
    assert processing.pending_job_ids == receipt.pending_job_ids
    assert committed.cases[CASE_ID].jobs[JOB_ID].status is JobStatus.INTERRUPTED
    assert committed.cases[SECOND_CASE_ID].jobs[SECOND_JOB_ID].status is JobStatus.PENDING
    assert committed.cases[CASE_ID].case.case_revision == 3
    assert committed.cases[CASE_ID].case.diagnosis_state.revision == 1
    assert len(repository.commit_calls) == 3
    assert guard.acquire_calls == guard.release_calls == 3
    assert notifier.notify_calls == [(CASE_ID, 3)]
    assert len(coordinator.calls) == 1

    commit_count = len(repository.commit_calls)
    coordinator_count = len(coordinator.calls)
    clock_count = clock.calls
    replay = service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)
    assert replay == receipt
    assert len(repository.commit_calls) == commit_count
    assert len(coordinator.calls) == coordinator_count
    assert clock.calls == clock_count


def test_incomplete_recovery_resumes_from_persisted_first_receipt() -> None:
    state = _recovery_state()
    decision = ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message="Pause after writing the recovery audit.",
        details=[],
        retryable=False,
    )
    first_service, repository, guard, notifier, clock, ids = _service(
        state,
        ScriptedCoordinator([decision]),
    )

    with pytest.raises(ApplicationPortError):
        first_service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    incomplete = repository.read_snapshot().recovery_processing_records[RECOVERY_ID]
    assert incomplete.completed_at is None
    assert incomplete.interrupted_job_ids == [JOB_ID]
    assert incomplete.pending_job_ids == [SECOND_JOB_ID]
    assert len(repository.read_snapshot().runtime_epochs) == 1
    assert len(repository.commit_calls) == 1

    resumed_coordinator = _coordinator_for(JobStatus.INTERRUPTED)
    resumed = JobControlService(
        repository=repository,
        publication_guard=guard,
        coordinator=resumed_coordinator,
        asset_catalog=_available_catalog(state),
        notifier=notifier,
        clock=clock,
        ids=ids,
    ).interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    assert resumed.interrupted_job_ids == incomplete.interrupted_job_ids
    assert resumed.pending_job_ids == incomplete.pending_job_ids
    assert len(repository.read_snapshot().runtime_epochs) == 1
    assert repository.read_snapshot().recovery_processing_records[RECOVERY_ID].completed_at == NOW

    with pytest.raises(ApplicationPortError) as captured:
        first_service.interrupt_previous_epoch(CURRENT_EPOCH, OTHER_RECOVERY_ID)
    assert captured.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_recovery_state_rejection_rereads_then_keeps_the_audit_incomplete() -> None:
    state = _recovery_state()
    detail = ApplicationErrorDetail(
        field="case_status",
        resource_type="CASE",
        resource_id=CASE_ID,
        resource_ref=None,
        expected="RUNNING",
        actual="INTERRUPTED",
        limit=None,
        observed=None,
    )
    decision = ApplicationError(
        code=ErrorCode.INVALID_CASE_STATE,
        message="The old-epoch Job cannot be interrupted in this Case state.",
        details=[detail],
        retryable=False,
    )
    coordinator = ScriptedCoordinator([decision])
    service, repository, guard, notifier, _, _ = _service(state, coordinator)

    with pytest.raises(ApplicationPortError) as captured:
        service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    assert captured.value.error.code is ErrorCode.VALIDATION_ERROR
    assert captured.value.error.message == decision.message
    assert captured.value.error.details == [detail]
    persisted = repository.read_snapshot()
    assert persisted.recovery_processing_records[RECOVERY_ID].completed_at is None
    assert persisted.cases[CASE_ID].jobs[JOB_ID].status is JobStatus.RUNNING
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == []
    assert len(coordinator.calls) == 1


def test_recovery_relational_state_contradiction_is_typed_state_corrupt() -> None:
    payload = _recovery_state().model_dump(mode="python")
    payload["cases"][CASE_ID]["case"].update(
        status=CaseStatus.INTERRUPTED,
        active_job_id=None,
    )
    contradictory = StateFile.model_validate(payload)
    coordinator = ScriptedCoordinator()
    service, repository, guard, notifier, _, _ = _service(
        contradictory,
        coordinator,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    assert captured.value.error.code is ErrorCode.STATE_CORRUPT
    record = repository.read_snapshot().recovery_processing_records[RECOVERY_ID]
    assert record.completed_at is None
    assert record.interrupted_job_ids == [JOB_ID]
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert coordinator.calls == []
    assert notifier.notify_calls == []


def test_recovery_state_rejection_accepts_a_freshly_completed_race() -> None:
    state = _recovery_state()
    repository = InMemoryStateRepository(state)
    decision = ApplicationError(
        code=ErrorCode.INVALID_CASE_STATE,
        message="Another writer already interrupted the old-epoch Job.",
        details=[],
        retryable=False,
    )

    def race_then_reject(snapshot, trigger):
        current = repository.read_snapshot()
        aggregate = current.cases[CASE_ID]
        case_payload = aggregate.case.model_dump(mode="python")
        case_payload.update(
            status=CaseStatus.INTERRUPTED,
            active_job_id=None,
            case_revision=aggregate.case.case_revision + 1,
            updated_at=NOW,
        )
        repository.commit(
            current.generation,
            aggregate.case.case_revision,
            build_state_mutation(
                upsert_case=aggregate.case.model_validate(case_payload),
                job_lifecycle_updates=[
                    JobLifecycleUpdate(
                        job_id=JOB_ID,
                        expected_status=JobStatus.RUNNING,
                        target_status=JobStatus.INTERRUPTED,
                        started_at=None,
                        finished_at=NOW,
                        runtime_epoch=None,
                    )
                ],
            ),
        )
        return decision

    coordinator = ScriptedCoordinator([race_then_reject])
    service, _, guard, notifier, _, _ = _service(
        state,
        coordinator,
        repository=repository,
    )

    receipt = service.interrupt_previous_epoch(CURRENT_EPOCH, RECOVERY_ID)

    persisted = repository.read_snapshot()
    assert receipt.interrupted_job_ids == [JOB_ID]
    assert receipt.pending_job_ids == [SECOND_JOB_ID]
    assert persisted.cases[CASE_ID].jobs[JOB_ID].status is JobStatus.INTERRUPTED
    assert persisted.recovery_processing_records[RECOVERY_ID].completed_at == NOW
    assert len(repository.commit_calls) == 3
    assert guard.acquire_calls == guard.release_calls == 2
    assert notifier.notify_calls == []
    assert len(coordinator.calls) == 1
