from __future__ import annotations

import threading

from problem_locator.dispatch import InProcessDispatcher, JobWorker, RuntimeEpochContext

from ._support import (
    CURRENT_EPOCH,
    clone_job,
    clone_outcome,
    load_job,
    load_outcome,
    runtime_receipt,
)
from .fakes import FakeApplicationService, FakeRuntime, ManualGate


class _ConcurrencyGate(ManualGate):
    def __init__(self, count):
        super().__init__()
        self.barrier = threading.Barrier(count + 1)

    def arrive_and_wait(self, timeout_seconds=2):
        self.barrier.wait(timeout_seconds)
        super().arrive_and_wait(timeout_seconds)


def _make_jobs_and_receipts():
    definitions = [
        ("route", "00000000-0000-0000-0000-000000000130", "00000000-0000-0000-0000-000000000230"),
        (
            "diagnose",
            "00000000-0000-0000-0000-000000000131",
            "00000000-0000-0000-0000-000000000231",
        ),
        ("review", "00000000-0000-0000-0000-000000000132", "00000000-0000-0000-0000-000000000232"),
    ]
    jobs = []
    receipts = []
    for index, (kind, job_id, case_id) in enumerate(definitions):
        job = clone_job(load_job(kind), job_id=job_id, case_id=case_id)
        outcome = clone_outcome(
            load_outcome(kind),
            job,
            outcome_id=f"00000000-0000-0000-0000-00000000033{index}",
        )
        jobs.append(job)
        receipts.append(runtime_receipt(outcome))
    return jobs, receipts


def _dispatcher(application, runtime):
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    dispatcher = InProcessDispatcher(JobWorker(application, runtime, epoch),
        job_identity=lambda key: (application.jobs[key].case_id, application.jobs[key].job_type))
    dispatcher.start()
    return dispatcher


def test_route_and_two_diagnoses_execute_across_cases() -> None:
    jobs, receipts = _make_jobs_and_receipts()
    application = FakeApplicationService(jobs)
    by_job = {receipt.job_outcome.job_id: receipt for receipt in receipts}
    runtime = FakeRuntime([lambda job, _: by_job[job.job_id]] * 3)
    first_execution = _ConcurrencyGate(3)
    runtime.execution_gate = first_execution
    dispatcher = _dispatcher(application, runtime)

    for job in jobs:
        assert dispatcher.submit(job.job_id).accepted
    dispatcher.enable_claiming()
    first_execution.barrier.wait(2)
    assert len(runtime.calls) == 3
    assert dispatcher.queued_job_ids == ()

    first_execution.release()
    assert dispatcher.wait_until_idle(1.0)
    assert runtime.max_active == 3
    assert {job.job_id for job, _ in runtime.calls} == {job.job_id for job in jobs}
    assert dispatcher.shutdown(1.0)


def test_concurrent_duplicate_submit_produces_one_claim_and_runtime_call() -> None:
    job = load_job("route")
    application = FakeApplicationService([job])
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    dispatcher = _dispatcher(application, runtime)
    barrier = threading.Barrier(9)
    receipts = []

    def submit() -> None:
        barrier.wait()
        receipts.append(dispatcher.submit(job.job_id))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()

    assert sum(receipt.accepted for receipt in receipts) == 1
    assert sum(receipt.duplicate for receipt in receipts) == 7
    dispatcher.enable_claiming()
    assert dispatcher.wait_until_idle(1.0)
    assert len(application.claim_calls) == 1
    assert len(runtime.calls) == 1
    assert dispatcher.shutdown(1.0)


def test_worker_has_no_global_execution_permit_between_direct_callers() -> None:
    jobs, receipts = _make_jobs_and_receipts()
    application = FakeApplicationService(jobs[:2])
    receipts_by_job_id = {
        receipt.job_outcome.job_id: receipt for receipt in receipts[:2]
    }

    def receipt_for_claimed_job(job, _cancellation):
        return receipts_by_job_id[job.job_id]

    runtime = FakeRuntime([receipt_for_claimed_job, receipt_for_claimed_job])
    gate = _ConcurrencyGate(2)
    runtime.execution_gate = gate
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    worker = JobWorker(application, runtime, epoch)
    barrier = threading.Barrier(3)
    results = []

    def execute(job_id: str) -> None:
        barrier.wait()
        from problem_locator.dispatch import CancellationController

        results.append(worker.execute_one(job_id, CancellationController()))

    threads = [
        threading.Thread(target=execute, args=(job.job_id,)) for job in jobs[:2]
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    gate.barrier.wait(2)
    assert len(application.claim_calls) == 2
    assert len(runtime.calls) == 2
    gate.release()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()

    assert len(results) == 2
    assert len(application.claim_calls) == 2
    assert len(runtime.calls) == 2
    assert runtime.max_active == 2
