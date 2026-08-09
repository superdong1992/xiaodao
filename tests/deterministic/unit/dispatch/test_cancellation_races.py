from __future__ import annotations

import threading

from problem_locator.contracts import (
    CancellationReason,
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    OutcomeDisposition,
    RuntimeInfrastructureError,
)
from problem_locator.dispatch import (
    CancellationController,
    InProcessDispatcher,
    JobWorker,
    RuntimeEpochContext,
)

from ._support import CURRENT_EPOCH, load_job, load_outcome, runtime_receipt
from .fakes import FakeApplicationService, FakeRuntime, ManualGate


def _dispatcher(runtime: FakeRuntime):
    job = load_job("route")
    application = FakeApplicationService([job])
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    dispatcher = InProcessDispatcher(JobWorker(application, runtime, epoch))
    dispatcher.start()
    return job, application, dispatcher


def test_cancellation_signal_is_first_reason_wins_and_wait_wakes() -> None:
    signal = CancellationController()
    observed: list[bool] = []
    waiter = threading.Thread(target=lambda: observed.append(signal.wait(None)))
    waiter.start()

    assert signal.cancel(CancellationReason.USER_CANCEL)
    assert not signal.cancel(CancellationReason.SERVICE_SHUTDOWN)
    waiter.join(1.0)
    assert not waiter.is_alive()
    assert observed == [True]
    assert signal.is_cancelled()
    assert signal.reason is CancellationReason.USER_CANCEL


def test_running_user_cancel_reaches_runtime_and_duplicate_does_not_overwrite() -> None:
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    runtime.wait_for_cancellation = True
    entered = ManualGate()
    runtime.execution_gate = entered
    job, application, dispatcher = _dispatcher(runtime)
    application.outcome_dispositions.append(OutcomeDisposition.STALE)
    dispatcher.submit(job.job_id)
    dispatcher.enable_claiming()
    assert entered.entered.wait(1.0)

    first = dispatcher.cancel(job.job_id)
    duplicate = dispatcher.cancel(job.job_id)
    entered.release()

    assert first.signalled
    assert not duplicate.signalled
    assert dispatcher.wait_until_idle(1.0)
    assert runtime.observed_cancellation_reasons == [CancellationReason.USER_CANCEL]
    assert application.returned_outcome_dispositions == [OutcomeDisposition.STALE]
    assert dispatcher.shutdown(1.0)


def test_user_cancel_beats_shutdown_and_shutdown_wait_is_bounded() -> None:
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    runtime.wait_for_cancellation = True
    entered = ManualGate()
    cleanup = ManualGate()
    runtime.execution_gate = entered
    runtime.cleanup_gate = cleanup
    job, application, dispatcher = _dispatcher(runtime)
    dispatcher.submit(job.job_id)
    dispatcher.enable_claiming()
    assert entered.entered.wait(1.0)
    assert dispatcher.cancel(job.job_id).signalled
    entered.release()
    assert cleanup.entered.wait(1.0)

    assert not dispatcher.shutdown(0.01)
    assert runtime.observed_cancellation_reasons == [CancellationReason.USER_CANCEL]
    cleanup.release()
    assert dispatcher.shutdown(1.0)
    assert application.submit_calls == []


def test_shutdown_beats_late_user_cancel_without_overwriting_reason() -> None:
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    runtime.wait_for_cancellation = True
    entered = ManualGate()
    cleanup = ManualGate()
    runtime.execution_gate = entered
    runtime.cleanup_gate = cleanup
    job, application, dispatcher = _dispatcher(runtime)
    dispatcher.submit(job.job_id)
    dispatcher.enable_claiming()
    assert entered.entered.wait(1.0)
    entered.release()

    assert not dispatcher.shutdown(0.01)
    assert cleanup.entered.wait(1.0)
    assert not dispatcher.cancel(job.job_id).signalled
    assert runtime.observed_cancellation_reasons == [
        CancellationReason.SERVICE_SHUTDOWN
    ]
    cleanup.release()
    assert dispatcher.shutdown(1.0)
    assert application.submit_calls == []


def test_cancel_after_runtime_returns_does_not_signal_during_outcome_submission() -> None:
    runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    job, application, dispatcher = _dispatcher(runtime)
    submission = ManualGate()
    application.submit_gate = submission
    dispatcher.submit(job.job_id)
    dispatcher.enable_claiming()
    assert submission.entered.wait(1.0)

    assert not dispatcher.cancel(job.job_id).signalled
    submission.release()
    assert dispatcher.wait_until_idle(1.0)
    assert len(application.submit_calls) == 1
    assert dispatcher.shutdown(1.0)


def test_cancel_after_runtime_error_does_not_signal_during_failure_report() -> None:
    failure = ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution record validation failed.",
        retryable=True,
        details=[],
    )
    runtime = FakeRuntime(
        [
            RuntimeInfrastructureError(
                "00000000-0000-0000-0000-000000000150",
                failure,
            )
        ]
    )
    job, application, dispatcher = _dispatcher(runtime)
    report = ManualGate()
    application.report_gate = report
    dispatcher.submit(job.job_id)
    dispatcher.enable_claiming()
    assert report.entered.wait(1.0)

    assert not dispatcher.cancel(job.job_id).signalled
    report.release()
    assert dispatcher.wait_until_idle(1.0)
    assert len(application.report_calls) == 1
    assert dispatcher.shutdown(1.0)
