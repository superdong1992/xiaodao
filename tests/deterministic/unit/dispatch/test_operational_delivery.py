"""Bound delivery attempts without repeating Runtime or inventing persisted state."""
from __future__ import annotations

import threading

import pytest

from problem_locator.contracts import ErrorCode, ExecutionFailure, ExecutionStage, RuntimeInfrastructureError
from problem_locator.dispatch import CancellationController, JobWorker, RuntimeEpochContext, SchedulerService
from problem_locator.dispatch.worker import DeliverySubmissionExpired
from problem_locator.operational import OperationalState
from tests.deterministic.contracts.fakes import DeterministicIdGenerator, InMemoryExecutionRecordStore
from ._support import CURRENT_EPOCH, application_port_error, load_job, load_outcome, runtime_receipt
from .fakes import FakeApplicationService, FakeRuntime


class _ClockBackoff:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def monotonic(self):
        return self.now

    def wait(self, delay):
        self.delays.append(delay)
        self.now += delay
        return True

    def wake_for_shutdown(self):
        pass


@pytest.mark.parametrize("phase", ["RESULT_DELIVERY", "FAILURE_REPORT"])
def test_persistent_submission_failure_stops_at_window_without_second_runtime(phase):
    job = load_job("route")
    application = FakeApplicationService([job])
    receipt = runtime_receipt(load_outcome("route"))
    failure = ExecutionFailure(stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED, message="record write failed", retryable=True, details=[])
    failure_id = "00000000-0000-0000-0000-000000000120"
    result = receipt if phase == "RESULT_DELIVERY" else RuntimeInfrastructureError(failure_id, failure)
    failures = application.submit_failures if phase == "RESULT_DELIVERY" else application.report_failures
    failures.extend(application_port_error(ErrorCode.STATE_WRITE_FAILED) for _ in range(100))
    runtime, clock = FakeRuntime([result]), _ClockBackoff()
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    worker = JobWorker(application, runtime, epoch, submission_backoff=clock, monotonic=clock.monotonic)

    with pytest.raises(DeliverySubmissionExpired) as caught:
        worker.execute_one(job.job_id, CancellationController())

    assert (caught.value.phase, caught.value.case_id, caught.value.job_id) == (phase, job.case_id, job.job_id)
    assert caught.value.error_code is ErrorCode.STATE_WRITE_FAILED
    assert caught.value.secondary_error_code is (ErrorCode.EXECUTION_RECORD_FAILED if phase == "FAILURE_REPORT" else None)
    assert clock.now == 30.0
    assert len(runtime.calls) == len(application.claim_calls) == 1
    if phase == "RESULT_DELIVERY":
        assert all(outcome is receipt.job_outcome and ref is receipt.outcome_file_ref
            for outcome, ref in application.submit_calls)
        assert 1 < len(application.submit_calls) < 100
    else:
        assert all(args == (job.job_id, CURRENT_EPOCH, failure_id, failure) for args in application.report_calls)
        assert 1 < len(application.report_calls) < 100


def test_late_successful_synchronous_submission_is_not_reclassified_unknown():
    job, clock = load_job("route"), _ClockBackoff()
    application = FakeApplicationService([job])
    application.on_submit = lambda *_: setattr(clock, "now", 31.0)
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    worker = JobWorker(application, runtime, epoch, submission_backoff=clock, monotonic=clock.monotonic)
    assert worker.execute_one(job.job_id, CancellationController()).delivery_completed
    assert clock.now == 31.0 and not clock.delays
    assert len(runtime.calls) == len(application.submit_calls) == 1


@pytest.mark.parametrize("queued_before_start", [False, True])
def test_expired_delivery_releases_worker_closes_admission_and_preserves_unknown(queued_before_start, monkeypatch):
    job, clock = load_job("route"), _ClockBackoff()
    application = FakeApplicationService([job])
    application.submit_failures.extend(application_port_error(ErrorCode.STATE_WRITE_FAILED) for _ in range(100))
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    operational = OperationalState(lambda: "2026-09-15T00:00:00Z")

    class Repository:
        def read_job(self, key):
            return application.jobs[key]

    service = SchedulerService(Repository(), InMemoryExecutionRecordStore(), application, runtime,
        DeterministicIdGenerator(scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}),
        submission_backoff=clock, monotonic=clock.monotonic, operational_state=operational)
    try:
        if queued_before_start:
            assert service.submit(job.job_id).accepted
            recover = service._recovery.recover

            def recover_after_worker_finishes():
                result = recover()
                assert service.wait_until_idle(1.0)
                return result

            monkeypatch.setattr(service._recovery, "recover", recover_after_worker_finishes)
        service.start()
        if not queued_before_start:
            assert service.submit(job.job_id).accepted
        assert service.wait_until_idle(1.0)
        assert not service.ready
        rejected = service.submit("00000000-0000-0000-0000-000000000999")
        assert not rejected.accepted and not rejected.duplicate
        fault, = operational.faults
        assert (fault.runtime_epoch, fault.case_id, fault.job_id, fault.phase, fault.persistence) == (
            CURRENT_EPOCH, job.case_id, job.job_id, "RESULT_DELIVERY", "UNKNOWN")
        assert application.jobs[job.job_id].status.value == "RUNNING"
        assert operational.error_for_case(job.case_id, "RUNNING") is not None
        assert operational.error_for_case(job.case_id, "RESOLVED") is None
        assert len(runtime.calls) == 1
    finally:
        assert service.shutdown(1.0)


def test_fatal_pause_exposes_each_previously_accepted_queued_case(monkeypatch):
    first, clock = load_job("route"), _ClockBackoff()
    queued = first.model_copy(update={"job_id": "00000000-0000-0000-0000-000000000998",
        "case_id": "00000000-0000-0000-0000-000000000999"})
    application = FakeApplicationService([first, queued])
    application.submit_failures.extend(application_port_error(ErrorCode.STATE_WRITE_FAILED) for _ in range(100))
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])

    class Repository:
        def read_job(self, key):
            return application.jobs[key]

    service = SchedulerService(Repository(), InMemoryExecutionRecordStore(), application, runtime,
        DeterministicIdGenerator(scripted_ids={"runtime_epoch": [CURRENT_EPOCH]}),
        submission_backoff=clock, monotonic=clock.monotonic)
    finished = threading.Event()
    record_fatal = service._dispatcher._on_fatal_worker_error

    def signal_fatal(job_id, error):
        record_fatal(job_id, error)
        finished.set()

    monkeypatch.setattr(service._dispatcher, "_on_fatal_worker_error", signal_fatal)
    try:
        assert service.submit(first.job_id).accepted
        assert service.submit(queued.job_id).accepted
        service.start()
        assert finished.wait(1.0)
        assert not service.ready
        assert application.jobs[queued.job_id].status.value == "PENDING"
        error = service.operational_state.error_for_case(queued.case_id, "RUNNING")
        details = {item.field: item.actual for item in error.details}
        assert details["phase"] == "DISPATCH_PAUSED"
        assert details["case_id"] == queued.case_id and details["job_id"] == queued.job_id
        assert details["runtime_epoch"] == CURRENT_EPOCH
        assert len(runtime.calls) == 1
        assert service.operational_state.error_for_case(queued.case_id, "CANCELLED") is None
    finally:
        assert service.shutdown(1.0)
