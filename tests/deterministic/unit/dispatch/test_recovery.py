from __future__ import annotations

import copy
import threading

import pytest

from problem_locator.contracts import (
    ErrorCode,
    JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    Job,
    JobStatus,
    OutcomeDisposition,
    RecoveryReceipt,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.dispatch import (
    RecoveryCoordinator,
    RuntimeEpochContext,
    RuntimeEpochFactory,
    SchedulerService,
    SchedulerShutdownSignal,
)
from tests.deterministic.contracts.fakes import DeterministicIdGenerator, InMemoryExecutionRecordStore

from ._support import (
    CURRENT_EPOCH,
    FINISHED_AT,
    OLD_EPOCH,
    REPOSITORY_ROOT,
    STARTED_AT,
    application_port_error,
    load_dispatch_fixture,
    load_outcome,
    load_state,
    runtime_receipt,
)
from .fakes import (
    DeterministicEpochFactory,
    FakeApplicationService,
    FakeRecoveryView,
    FakeRuntime,
    ManualGate,
    RecordingDispatcher,
    ManualSubmissionBackoff,
)


OLD_STARTED_AT = "2026-07-31T00:00:00.000Z"
OLD_RECOVERED_AT = "2026-07-31T00:00:01.000Z"
CURRENT_RECOVERED_AT = "2026-07-31T00:12:00.000Z"


class RecordingExecutionRecordStore(InMemoryExecutionRecordStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_outcome_calls: list[str] = []

    def read_published_outcome(self, job_id: str):
        self.read_outcome_calls.append(job_id)
        return super().read_published_outcome(job_id)


def test_startup_outbox_fixture_covers_r3_recovery_audit_scenarios() -> None:
    scenarios = load_dispatch_fixture("startup-outbox.json")["scenarios"]
    expected = {
        "finalized_old_running": ("replay_then_interrupt", None),
        "replay_delivery_failure": (
            "readiness_false_no_interrupt",
            "JobControlPort.submit_outcome:STATE_WRITE_FAILED",
        ),
        "cancelled_then_finalized": ("stale_audit", None),
        "corrupt_finalized_record": (
            "readiness_false_no_interrupt",
            "ExecutionRecordStore.read_published_outcome:EXECUTION_RECORD_FAILED",
        ),
        "technical_rejected_without_trusted_outcome": (
            "skip_replay_from_rejected_audit",
            None,
        ),
        "completed_recovery_exact_receipt": (
            "receipt_matches_persisted_recovery_record",
            None,
        ),
        "first_next_job_publish_transient": (
            "retry_same_receipt_then_apply",
            "JobControlPort.submit_outcome:EXECUTION_RECORD_FAILED",
        ),
        "existing_next_job_record_corrupt": (
            "rejected_terminal_no_retry",
            None,
        ),
        "next_job_asset_unavailable_park": (
            "readiness_false_preserve_outbox_no_interrupt",
            "JobControlPort.submit_outcome:ASSET_VERSION_UNAVAILABLE",
        ),
        "next_job_config_invalid_park": (
            "readiness_false_preserve_outbox_no_interrupt",
            "JobControlPort.submit_outcome:CONFIG_INVALID",
        ),
        "parked_asset_repaired_replay": (
            "replay_same_canonical_receipt_then_complete",
            None,
        ),
        "replay_state_corrupt": (
            "readiness_false_no_interrupt",
            "JobControlPort.submit_outcome:STATE_CORRUPT",
        ),
    }
    assert {scenario["name"] for scenario in scenarios} == set(expected)
    for scenario in scenarios:
        assert set(scenario) == {
            "expected",
            "fault",
            "job_fixture",
            "name",
            "outcome_fixture",
        }
        assert (scenario["expected"], scenario["fault"]) == expected[
            scenario["name"]
        ]
        assert (REPOSITORY_ROOT / scenario["job_fixture"]).is_file()
        if scenario["outcome_fixture"] is not None:
            assert (REPOSITORY_ROOT / scenario["outcome_fixture"]).is_file()


def _state_with_job_status(status: JobStatus) -> StateFile:
    payload = load_state().model_dump(mode="json")
    case_id = next(iter(payload["cases"]))
    aggregate = payload["cases"][case_id]
    job_id = next(iter(aggregate["jobs"]))
    job = aggregate["jobs"][job_id]
    if status is JobStatus.PENDING:
        job.update(status="PENDING", started_at=None, finished_at=None, runtime_epoch=None)
        aggregate["case"].update(status="RUNNING", active_job_id=job_id)
        payload["runtime_epochs"] = []
        payload["recovery_processing_records"] = {}
    elif status is JobStatus.RUNNING:
        job.update(
            status="RUNNING",
            started_at=STARTED_AT,
            finished_at=None,
            runtime_epoch=OLD_EPOCH,
        )
        aggregate["case"].update(status="RUNNING", active_job_id=job_id)
        payload["runtime_epochs"] = [_epoch_record(OLD_EPOCH, OLD_RECOVERED_AT)]
        payload["recovery_processing_records"] = {
            OLD_EPOCH: _recovery_record(OLD_EPOCH, OLD_RECOVERED_AT)
        }
    elif status is JobStatus.INTERRUPTED:
        job.update(
            status="INTERRUPTED",
            started_at=STARTED_AT,
            finished_at=FINISHED_AT,
            runtime_epoch=OLD_EPOCH,
        )
        aggregate["case"].update(status="INTERRUPTED", active_job_id=None)
        payload["runtime_epochs"] = [_epoch_record(OLD_EPOCH, OLD_RECOVERED_AT)]
        payload["recovery_processing_records"] = {
            OLD_EPOCH: _recovery_record(OLD_EPOCH, OLD_RECOVERED_AT)
        }
    elif status is JobStatus.CANCELLED:
        job.update(
            status="CANCELLED",
            started_at=STARTED_AT,
            finished_at=FINISHED_AT,
            runtime_epoch=OLD_EPOCH,
        )
        aggregate["case"].update(status="CANCELLED", active_job_id=None)
        payload["runtime_epochs"] = [_epoch_record(OLD_EPOCH, OLD_RECOVERED_AT)]
        payload["recovery_processing_records"] = {
            OLD_EPOCH: _recovery_record(OLD_EPOCH, OLD_RECOVERED_AT)
        }
    else:
        raise AssertionError(status)
    return StateFile.model_validate(payload)


def _two_case_pending_state() -> tuple[StateFile, list[tuple[str, str]]]:
    payload = _state_with_job_status(JobStatus.PENDING).model_dump(mode="json")
    template = next(iter(payload["cases"].values()))
    rows = [
        (
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000090",
        ),
        (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000099",
        ),
    ]
    cases: dict[str, object] = {}
    for case_id, job_id in rows:
        aggregate = copy.deepcopy(template)
        old_job_id = next(iter(aggregate["jobs"]))
        job = aggregate["jobs"].pop(old_job_id)
        job["job_id"] = job_id
        job["case_id"] = case_id
        aggregate["jobs"][job_id] = job
        aggregate["case"]["case_id"] = case_id
        aggregate["case"]["active_job_id"] = job_id
        cases[case_id] = aggregate
    payload["cases"] = cases
    return StateFile.model_validate(payload), rows


def _epoch_record(epoch: str, completed_at: str | None) -> dict[str, object]:
    return {
        "runtime_epoch": epoch,
        "started_at": OLD_STARTED_AT,
        "recovery_id": epoch,
        "recovery_completed_at": completed_at,
    }


def _recovery_record(
    epoch: str,
    completed_at: str | None,
    *,
    interrupted: list[str] | None = None,
    pending: list[str] | None = None,
) -> dict[str, object]:
    return {
        "recovery_id": epoch,
        "current_runtime_epoch": epoch,
        "interrupted_job_ids": sorted(interrupted or []),
        "pending_job_ids": sorted(pending or []),
        "completed_at": completed_at,
    }


def _complete_interrupt(
    view: FakeRecoveryView,
    current_epoch: str,
    recovery_id: str,
) -> RecoveryReceipt:
    payload = view.read_snapshot().model_dump(mode="json")
    existing = payload["recovery_processing_records"].get(recovery_id)
    if existing is not None:
        if existing["current_runtime_epoch"] != current_epoch:
            raise AssertionError("recovery_id was reused for another runtime epoch")
        if existing["completed_at"] is None:
            runtime_record = next(
                record
                for record in payload["runtime_epochs"]
                if record["recovery_id"] == recovery_id
            )
            runtime_record["recovery_completed_at"] = CURRENT_RECOVERED_AT
            existing["completed_at"] = CURRENT_RECOVERED_AT
            payload["generation"] += 1
            payload["updated_at"] = CURRENT_RECOVERED_AT
            view.replace(StateFile.model_validate(payload))
        return RecoveryReceipt(
            recovery_id=recovery_id,
            interrupted_job_ids=existing["interrupted_job_ids"],
            pending_job_ids=existing["pending_job_ids"],
        )
    interrupted: list[str] = []
    pending: list[str] = []
    for aggregate in payload["cases"].values():
        for job_id, job in aggregate["jobs"].items():
            if job["status"] == "RUNNING" and job["runtime_epoch"] != current_epoch:
                interrupted.append(job_id)
                job.update(status="INTERRUPTED", finished_at=FINISHED_AT)
                aggregate["case"].update(status="INTERRUPTED", active_job_id=None)
            elif job["status"] == "PENDING":
                pending.append(job_id)
    payload["generation"] += 1
    payload["updated_at"] = CURRENT_RECOVERED_AT
    payload["runtime_epochs"] = [
        record
        for record in payload["runtime_epochs"]
        if record["runtime_epoch"] != current_epoch
    ]
    payload["runtime_epochs"].append(_epoch_record(current_epoch, CURRENT_RECOVERED_AT))
    payload["recovery_processing_records"][recovery_id] = _recovery_record(
        current_epoch,
        CURRENT_RECOVERED_AT,
        interrupted=interrupted,
        pending=pending,
    )
    view.replace(StateFile.model_validate(payload))
    return RecoveryReceipt(
        recovery_id=recovery_id,
        interrupted_job_ids=sorted(interrupted),
        pending_job_ids=sorted(pending),
    )


def _record_claimed_job(view: FakeRecoveryView, running_job: Job) -> None:
    payload = view.read_snapshot().model_dump(mode="json")
    aggregate = payload["cases"][running_job.case_id]
    aggregate["jobs"][running_job.job_id] = running_job.model_dump(mode="json")
    aggregate["case"].update(
        status="RUNNING",
        active_job_id=running_job.job_id,
    )
    payload["generation"] += 1
    payload["updated_at"] = STARTED_AT
    view.replace(StateFile.model_validate(payload))


def _record_processed_outcome(
    view: FakeRecoveryView,
    disposition: OutcomeDisposition,
    *,
    rejection_error_code: ErrorCode = ErrorCode.OUTCOME_INVALID,
) -> None:
    payload = view.read_snapshot().model_dump(mode="json")
    case_id = next(iter(payload["cases"]))
    aggregate = payload["cases"][case_id]
    job_id = next(iter(aggregate["jobs"]))
    outcome = load_outcome("route")
    receipt = runtime_receipt(outcome)
    job = aggregate["jobs"][job_id]
    if job["status"] != "CANCELLED":
        job.update(status="SUCCEEDED", finished_at=FINISHED_AT)
        aggregate["case"].update(status="WAITING_INPUT", active_job_id=None)
    persisted_disposition = (
        OutcomeDisposition.APPLIED
        if disposition is OutcomeDisposition.DUPLICATE
        else disposition
    )
    aggregate["outcomes"][outcome.outcome_id] = outcome.model_dump(mode="json")
    aggregate["outcome_processing_records"][outcome.outcome_id] = {
        "outcome_id": outcome.outcome_id,
        "job_id": job_id,
        "outcome_hash": receipt.outcome_file_ref.sha256,
        "outcome_file_ref": receipt.outcome_file_ref.model_dump(mode="json"),
        "disposition": persisted_disposition.value,
        "processed_at": FINISHED_AT,
        "error_code": (
            rejection_error_code.value
            if persisted_disposition is OutcomeDisposition.REJECTED
            else None
        ),
        "accepted_evidence_ids": [],
        "accepted_artifact_ids": [],
        "created_job_id": None,
        "reason": "S05 recovery fixture processing result.",
    }
    payload["generation"] += 1
    payload["updated_at"] = FINISHED_AT
    view.replace(StateFile.model_validate(payload))


def _coordinator(
    state: StateFile,
    records=None,
    application=None,
    dispatcher=None,
    backoff=None,
    shutdown_signal=None,
):
    view = FakeRecoveryView(state)
    records = records or InMemoryExecutionRecordStore()
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = application or FakeApplicationService([job])
    dispatcher = dispatcher or RecordingDispatcher()
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    epoch_context = RuntimeEpochContext()
    coordinator = RecoveryCoordinator(
        view,
        records,
        application,
        dispatcher,  # type: ignore[arg-type]
        DeterministicEpochFactory(CURRENT_EPOCH),  # type: ignore[arg-type]
        epoch_context,
        shutdown_signal,
        backoff,
    )
    return coordinator, view, records, application, dispatcher, epoch_context


def test_startup_does_not_redispatch_or_persist_old_pending_work() -> None:
    state = _state_with_job_status(JobStatus.PENDING)
    coordinator, view, _, application, dispatcher, epoch_context = _coordinator(state)
    job_id = next(iter(next(iter(state.cases.values())).jobs))
    original_job = view.read_job(job_id)

    result = coordinator.recover()

    assert result.completed
    assert result.runtime_epoch == CURRENT_EPOCH
    assert result.replayed_job_ids == ()
    assert result.pending_job_ids == ()
    assert application.operation_log == []
    assert dispatcher.submitted == []
    assert dispatcher.claiming_enabled
    assert epoch_context.require() == CURRENT_EPOCH
    assert view.read_snapshot_calls == 0
    assert view.read_job(job_id) == original_job


def test_startup_does_not_scan_history_or_order_old_jobs() -> None:
    state, rows = _two_case_pending_state()
    records = RecordingExecutionRecordStore()
    coordinator, _, _, _, dispatcher, _ = _coordinator(
        state,
        records=records,
    )

    result = coordinator.recover()

    assert result.completed
    assert records.read_outcome_calls == []
    assert result.pending_job_ids == ()
    assert dispatcher.submitted == []
    assert dispatcher.claiming_enabled


def test_startup_does_not_replay_finalized_old_running_outcome() -> None:
    scenario = next(
        item
        for item in load_dispatch_fixture("startup-outbox.json")["scenarios"]
        if item["name"] == "finalized_old_running"
    )
    assert scenario["expected"] == "replay_then_interrupt"
    assert scenario["name"] == "finalized_old_running"
    assert scenario["job_fixture"].endswith("/state.json")
    assert scenario["outcome_fixture"].endswith("/job-outcome-route.json")
    state = _state_with_job_status(JobStatus.RUNNING)
    records = InMemoryExecutionRecordStore()
    outcome = load_outcome("route")
    records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    coordinator, view, _, application, dispatcher, _ = _coordinator(
        state, records=records
    )
    application.on_submit = lambda _outcome, _ref, disposition: _record_processed_outcome(
        view, disposition
    )

    result = coordinator.recover()

    assert result.completed
    assert result.replayed_job_ids == ()
    assert result.interrupted_job_ids == ()
    assert application.operation_log == []
    assert dispatcher.submitted == []


# V10 deliberately retires startup replay and interruption. The replacement
# contract covers PENDING/RUNNING/CANCELLED and corrupt old state/outbox/assets
# in test_startup_v10.py. Terminal process-crash durability is exercised by
# integration/test_terminal_crash.py; delivery retries remain below.

def test_scheduler_service_shutdown_wakes_the_worker_delivery_backoff() -> None:
    state = _state_with_job_status(JobStatus.PENDING)
    view = FakeRecoveryView(state)
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = FakeApplicationService([job])
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    application.submit_failures.append(
        application_port_error(ErrorCode.STATE_WRITE_FAILED)
    )
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    backoff = ManualSubmissionBackoff()
    wait_gate = ManualGate()
    backoff.wait_gate = wait_gate
    service = SchedulerService(
        view,
        InMemoryExecutionRecordStore(),
        application,
        runtime,
        DeterministicIdGenerator(
            scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
        ),
        submission_backoff=backoff,
    )

    recovery_result = service.start()
    assert recovery_result.completed
    assert service.submit(job.job_id).accepted
    assert wait_gate.entered.wait(1.0)

    assert service.shutdown(1.0)

    assert len(runtime.calls) == 1
    assert len(application.submit_calls) == 1
    assert backoff.delays == [0.1]
    assert not service.ready


def test_invalid_shutdown_timeout_does_not_poison_scheduler_service() -> None:
    state = _state_with_job_status(JobStatus.PENDING)
    view = FakeRecoveryView(state)
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = FakeApplicationService([job])
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    service = SchedulerService(
        view,
        InMemoryExecutionRecordStore(),
        application,
        runtime,
        DeterministicIdGenerator(
            scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
        ),
        submission_backoff=ManualSubmissionBackoff(),
    )

    with pytest.raises(ValueError, match="non-negative"):
        service.shutdown(-1.0)

    assert service.start().completed
    assert service.submit(job.job_id).accepted
    assert service.wait_until_idle(1.0)
    assert len(runtime.calls) == 1
    assert service.shutdown(1.0)


def test_scheduler_service_starts_without_entering_old_replay_backoff() -> None:
    state = _state_with_job_status(JobStatus.RUNNING)
    view = FakeRecoveryView(state)
    outcome = load_outcome("route")
    records = InMemoryExecutionRecordStore()
    records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    application = FakeApplicationService(
        [next(iter(next(iter(state.cases.values())).jobs.values()))]
    )
    application.submit_failures.append(
        application_port_error(ErrorCode.STATE_WRITE_FAILED)
    )
    backoff = ManualSubmissionBackoff()
    wait_gate = ManualGate()
    backoff.wait_gate = wait_gate
    service = SchedulerService(
        view,
        records,
        application,
        FakeRuntime(),
        DeterministicIdGenerator(
            scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
        ),
        submission_backoff=backoff,
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(service.start()))
    thread.start()
    thread.join(1.0)
    assert service.shutdown(1.0)
    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].completed
    assert not wait_gate.entered.is_set()
    assert application.submit_calls == []
    assert application.interrupt_calls == []
    assert backoff.delays == []
    assert not service.ready


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.ASSET_VERSION_UNAVAILABLE,
        ErrorCode.CONFIG_INVALID,
    ],
)
def test_scheduler_service_keeps_safe_code_for_fatal_worker_port_error(
    code: ErrorCode,
) -> None:
    state = _state_with_job_status(JobStatus.PENDING)
    view = FakeRecoveryView(state)
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = FakeApplicationService([job])
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    application.submit_failures.append(
        application_port_error(code)
    )
    service = SchedulerService(
        view,
        InMemoryExecutionRecordStore(),
        application,
        FakeRuntime([runtime_receipt(load_outcome("route"))]),
        DeterministicIdGenerator(
            scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
        ),
        submission_backoff=ManualSubmissionBackoff(),
    )

    assert service.start().completed
    assert service.submit(job.job_id).accepted
    assert service.wait_until_idle(1.0)

    assert service.fatal_worker_error_type == "ApplicationPortError"
    assert service.fatal_worker_error_code is code
    assert not service.ready
    assert service.shutdown(1.0)


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_runtime_state_fault_stops_readiness_and_restart_does_not_replay(
    code: ErrorCode,
) -> None:
    state = _state_with_job_status(JobStatus.PENDING)
    view = FakeRecoveryView(state)
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = FakeApplicationService([job])
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    runtime_error = application_port_error(code)
    runtime = FakeRuntime([runtime_error])
    records = InMemoryExecutionRecordStore()
    backoff = ManualSubmissionBackoff()
    service = SchedulerService(
        view,
        records,
        application,
        runtime,
        DeterministicIdGenerator(
            scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
        ),
        submission_backoff=backoff,
    )

    assert service.start().completed
    assert service.submit(job.job_id).accepted
    assert service.wait_until_idle(1.0)

    assert service.fatal_worker_error_type == "ApplicationPortError"
    assert service.fatal_worker_error_code is code
    assert not service.ready
    assert not service._dispatcher.claiming_enabled
    assert len(application.claim_calls) == 1
    assert len(runtime.calls) == 1
    assert application.submit_calls == []
    assert application.report_calls == []
    assert application.jobs[job.job_id].status is JobStatus.RUNNING
    assert backoff.delays == []
    assert records.read_published_outcome(job.job_id) is None
    assert service.shutdown(1.0)

    repaired_state = _state_with_job_status(JobStatus.RUNNING)
    repaired, repaired_view, _, repaired_application, dispatcher, _ = _coordinator(
        repaired_state,
        records=records,
    )

    recovered = repaired.recover()

    assert recovered.completed
    assert recovered.interrupted_job_ids == ()
    assert repaired_view.read_snapshot_calls == 0
    assert repaired_application.submit_calls == []
    assert len(runtime.calls) == 1
    assert dispatcher.claiming_enabled


def test_each_scheduler_process_gets_a_new_non_reused_runtime_epoch() -> None:
    generator = DeterministicIdGenerator(seed="s05-runtime-epochs")
    first = RuntimeEpochFactory(generator).create([])
    second = RuntimeEpochFactory(generator).create([first])
    assert first != second


def test_runtime_epoch_factory_rejects_historical_reuse() -> None:
    generator = DeterministicIdGenerator(
        scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}
    )
    factory = RuntimeEpochFactory(generator)
    try:
        factory.create([CURRENT_EPOCH])
    except RuntimeError as exc:
        assert "reused" in str(exc)
    else:
        raise AssertionError("historical epoch reuse was accepted")
