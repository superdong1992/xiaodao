from __future__ import annotations

from problem_locator.contracts import JobStatus, JobType
from problem_locator.dispatch import (
    InProcessDispatcher,
    JobWorker,
    RecoveryCoordinator,
    RuntimeEpochContext,
)
from tests.deterministic.contracts.fakes import InMemoryExecutionRecordStore

from ._support import (
    CURRENT_EPOCH,
    clone_job,
    clone_outcome,
    load_dispatch_fixture,
    load_job,
    load_outcome,
    runtime_receipt,
)
from .fakes import (
    DeterministicEpochFactory,
    FakeApplicationService,
    FakeRecoveryView,
    FakeRuntime,
    RecordingDispatcher,
)
from .test_recovery import _complete_interrupt, _state_with_job_status


def test_review_resume_dispatches_one_new_same_stage_replacement() -> None:
    fixture = load_dispatch_fixture("replacement-chain.json")
    source = clone_job(
        load_job("review"),
        job_id=fixture["source_job_id"],
        status=JobStatus.INTERRUPTED,
    )
    replacement = clone_job(
        load_job("review"),
        job_id=fixture["replacement_job_id"],
        replacement_for_job_id=source.job_id,
    )
    outcome = clone_outcome(
        load_outcome("review"),
        replacement,
        outcome_id="00000000-0000-0000-0000-000000000103",
    )
    application = FakeApplicationService([replacement])
    runtime = FakeRuntime([runtime_receipt(outcome)])
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    dispatcher = InProcessDispatcher(JobWorker(application, runtime, epoch))
    dispatcher.start()

    first = dispatcher.submit(replacement.job_id)
    duplicate = dispatcher.submit(replacement.job_id)
    dispatcher.enable_claiming()
    assert dispatcher.wait_until_idle(1.0)

    assert first.accepted and not first.duplicate
    assert not duplicate.accepted and duplicate.duplicate
    assert len(runtime.calls) == 1
    executed = runtime.calls[0][0]
    assert executed.job_id != source.job_id
    assert executed.replacement_for_job_id == source.job_id
    assert executed.job_type is source.job_type is JobType.REVIEW
    assert dispatcher.shutdown(1.0)


def test_pending_resume_redispatches_the_original_job_id() -> None:
    job = load_job("route")
    application = FakeApplicationService([job])
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    dispatcher = InProcessDispatcher(JobWorker(application, runtime, epoch))
    dispatcher.start()

    assert dispatcher.submit(job.job_id).accepted
    assert dispatcher.submit(job.job_id).duplicate
    dispatcher.enable_claiming()
    assert dispatcher.wait_until_idle(1.0)

    assert application.claim_calls == [(job.job_id, CURRENT_EPOCH)]
    assert runtime.calls[0][0].job_id == job.job_id
    assert dispatcher.shutdown(1.0)


def test_startup_recovery_never_auto_dispatches_interrupted_job() -> None:
    state = _state_with_job_status(JobStatus.INTERRUPTED)
    view = FakeRecoveryView(state)
    job = next(iter(next(iter(state.cases.values())).jobs.values()))
    application = FakeApplicationService([job])
    dispatcher = RecordingDispatcher()
    application.on_interrupt = lambda epoch, recovery_id: _complete_interrupt(
        view, epoch, recovery_id
    )
    coordinator = RecoveryCoordinator(
        view,
        InMemoryExecutionRecordStore(),
        application,
        dispatcher,  # type: ignore[arg-type]
        DeterministicEpochFactory(CURRENT_EPOCH),  # type: ignore[arg-type]
        RuntimeEpochContext(),
    )

    result = coordinator.recover()

    assert result.completed
    assert result.pending_job_ids == ()
    assert dispatcher.submitted == []
