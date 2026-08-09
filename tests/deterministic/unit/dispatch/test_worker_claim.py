from __future__ import annotations

import threading

import pytest

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    ExecutionFailure,
    ExecutionStage,
    FailureReportDisposition,
    JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    JobControlPort,
    JobStatus,
    OutcomeDisposition,
    Runtime,
    RuntimeInfrastructureError,
)
from problem_locator.dispatch import (
    CancellationController,
    JobWorker,
    RuntimeEpochContext,
)

from ._support import (
    CURRENT_EPOCH,
    application_port_error,
    clone_job,
    clone_outcome,
    load_dispatch_fixture,
    load_job,
    load_outcome,
    runtime_receipt,
)
from .fakes import (
    FakeApplicationService,
    FakeRuntime,
    ManualGate,
    ManualSubmissionBackoff,
)


def _worker(
    application: FakeApplicationService,
    runtime: FakeRuntime,
    *,
    backoff: ManualSubmissionBackoff | None = None,
) -> JobWorker:
    epoch = RuntimeEpochContext()
    epoch.install(CURRENT_EPOCH)
    return JobWorker(application, runtime, epoch, submission_backoff=backoff)


def test_worker_fakes_implement_the_frozen_ports() -> None:
    assert isinstance(FakeApplicationService(), JobControlPort)
    assert isinstance(FakeRuntime(), Runtime)


def test_component_stage_matrix_covers_all_typed_workers_and_mismatches() -> None:
    rows = load_dispatch_fixture("job-stage-matrix.json")["cases"]
    assert {(row["case_status"], row["job_type"], row["claim_allowed"]) for row in rows} == {
        ("RUNNING", "ROUTE", True),
        ("RUNNING", "DIAGNOSE", True),
        ("REVIEWING", "REVIEW", True),
        ("REVIEWING", "DIAGNOSE", False),
        ("RUNNING", "REVIEW", False),
    }
    for row in rows:
        fixture_kind = row["job_type"].lower()
        job = load_job(fixture_kind)
        application = FakeApplicationService([job])
        if row["claim_allowed"]:
            runtime = FakeRuntime([runtime_receipt(load_outcome(fixture_kind))])
        else:
            application.claim_rejections.add(job.job_id)
            runtime = FakeRuntime()
        result = _worker(application, runtime).execute_one(
            job.job_id,
            CancellationController(),
        )
        assert result.claimed is row["claim_allowed"]
        assert result.runtime_called is row["claim_allowed"]


@pytest.mark.parametrize("job_type", ["route", "diagnose", "review"])
def test_claimed_job_uses_typed_worker_and_submits_the_same_receipt(job_type: str) -> None:
    job = load_job(job_type)
    outcome = load_outcome(job_type)
    receipt = runtime_receipt(outcome)
    application = FakeApplicationService(
        [job],
        outcome_dispositions=[OutcomeDisposition.APPLIED],
    )
    runtime = FakeRuntime([receipt])

    result = _worker(application, runtime).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.claimed and result.runtime_called
    assert result.outcome_disposition is OutcomeDisposition.APPLIED
    assert len(runtime.calls) == 1
    claimed_job = runtime.calls[0][0]
    assert claimed_job.status is JobStatus.RUNNING
    assert claimed_job.runtime_epoch == CURRENT_EPOCH
    assert application.submit_calls == [
        (receipt.job_outcome, receipt.outcome_file_ref)
    ]


@pytest.mark.parametrize("asset_failure", [False, True])
def test_rejected_or_asset_failed_claim_never_calls_runtime(asset_failure: bool) -> None:
    job = load_job("route")
    application = FakeApplicationService([job])
    if asset_failure:
        application.asset_failures.add(job.job_id)
    else:
        application.claim_rejections.add(job.job_id)
    runtime = FakeRuntime()

    result = _worker(application, runtime).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert not result.claimed
    assert not result.runtime_called
    assert application.claim_calls == [(job.job_id, CURRENT_EPOCH)]
    assert runtime.calls == []
    assert application.submit_calls == []
    assert application.report_calls == []


def test_claim_competition_cannot_use_the_application_error_channel() -> None:
    job = load_job("route")
    application = FakeApplicationService([job])
    application.claim_failures.append(
        application_port_error(ErrorCode.CLAIM_REJECTED)
    )

    with pytest.raises(AssertionError, match="not allowed"):
        _worker(application, FakeRuntime()).execute_one(
            job.job_id,
            CancellationController(),
        )


def test_concurrent_claims_execute_runtime_exactly_once() -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    application = FakeApplicationService([job])
    runtime = FakeRuntime([receipt])
    worker = _worker(application, runtime)
    barrier = threading.Barrier(3)
    results = []

    def run() -> None:
        barrier.wait()
        results.append(worker.execute_one(job.job_id, CancellationController()))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()

    assert sum(result.claimed for result in results) == 1
    assert len(runtime.calls) == 1
    assert len(application.submit_calls) == 1


@pytest.mark.parametrize("disposition", list(FailureReportDisposition))
def test_runtime_infrastructure_failure_is_reported_with_original_identity(
    disposition: FailureReportDisposition,
) -> None:
    job = load_job("route")
    failure = ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution record validation failed.",
        retryable=True,
        details=[],
    )
    failure_id = "00000000-0000-0000-0000-000000000120"
    infrastructure_error = RuntimeInfrastructureError(failure_id, failure)
    application = FakeApplicationService(
        [job],
        failure_dispositions=[disposition],
    )
    runtime = FakeRuntime([infrastructure_error])
    backoff = ManualSubmissionBackoff()

    result = _worker(application, runtime, backoff=backoff).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.failure_disposition is disposition
    assert len(runtime.calls) == 1
    assert application.report_calls == [
        (job.job_id, CURRENT_EPOCH, failure_id, failure)
    ]
    assert application.submit_calls == []
    assert backoff.delays == []


def test_outcome_submission_retries_only_the_same_finalized_receipt() -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    application = FakeApplicationService([job])
    retry_codes = [
        ErrorCode.RESOURCE_PUBLISH_FAILED,
        ErrorCode.STATE_WRITE_FAILED,
        ErrorCode.REVISION_CONFLICT,
        ErrorCode.EXECUTION_RECORD_FAILED,
        ErrorCode.RESOURCE_PUBLISH_FAILED,
        ErrorCode.STATE_WRITE_FAILED,
        ErrorCode.EXECUTION_RECORD_FAILED,
    ]
    assert frozenset(retry_codes) == JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES
    scripted_failures = [application_port_error(code) for code in retry_codes]
    assert not next(
        failure
        for failure in scripted_failures
        if failure.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    ).error.retryable
    application.submit_failures.extend(scripted_failures)
    runtime = FakeRuntime([receipt])
    backoff = ManualSubmissionBackoff()

    result = _worker(application, runtime, backoff=backoff).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.delivery_completed
    assert result.outcome_disposition is OutcomeDisposition.APPLIED
    assert len(runtime.calls) == 1
    assert len(application.claim_calls) == 1
    assert len(application.submit_calls) == len(retry_codes) + 1
    assert backoff.delays == [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 5.0]
    assert all(
        outcome is receipt.job_outcome and file_ref is receipt.outcome_file_ref
        for outcome, file_ref in application.submit_calls
    )


@pytest.mark.parametrize("disposition", list(OutcomeDisposition))
def test_each_outcome_disposition_completes_delivery_without_retry(
    disposition: OutcomeDisposition,
) -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    application = FakeApplicationService(
        [job],
        outcome_dispositions=[disposition],
    )
    runtime = FakeRuntime([receipt])
    backoff = ManualSubmissionBackoff()

    result = _worker(application, runtime, backoff=backoff).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.delivery_completed
    assert result.outcome_disposition is disposition
    assert len(runtime.calls) == 1
    assert application.submit_calls == [
        (receipt.job_outcome, receipt.outcome_file_ref)
    ]
    assert backoff.delays == []


@pytest.mark.parametrize(
    "code",
    sorted(JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES, key=lambda code: code.value),
)
def test_outcome_submission_park_error_preserves_running_job_without_retry(
    code: ErrorCode,
) -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    parked = application_port_error(code)
    application = FakeApplicationService([job])
    application.submit_failures.append(parked)
    runtime = FakeRuntime([receipt])
    backoff = ManualSubmissionBackoff()

    with pytest.raises(ApplicationPortError) as caught:
        _worker(application, runtime, backoff=backoff).execute_one(
            job.job_id,
            CancellationController(),
        )

    assert caught.value is parked
    assert len(application.claim_calls) == 1
    assert len(runtime.calls) == 1
    assert application.submit_calls == [
        (receipt.job_outcome, receipt.outcome_file_ref)
    ]
    assert application.report_calls == []
    assert application.jobs[job.job_id].status is JobStatus.RUNNING
    assert backoff.delays == []


def test_failure_report_retries_original_id_and_failure_object_only() -> None:
    job = load_job("route")
    failure = ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution record validation failed.",
        retryable=True,
        details=[],
    )
    failure_id = "00000000-0000-0000-0000-000000000122"
    application = FakeApplicationService([job])
    application.report_failures.extend(
        [
            application_port_error(ErrorCode.STATE_WRITE_FAILED),
            application_port_error(ErrorCode.REVISION_CONFLICT),
        ]
    )
    runtime = FakeRuntime([RuntimeInfrastructureError(failure_id, failure)])
    backoff = ManualSubmissionBackoff()

    result = _worker(application, runtime, backoff=backoff).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.delivery_completed
    assert result.failure_disposition is FailureReportDisposition.APPLIED
    assert len(runtime.calls) == 1
    assert len(application.report_calls) == 3
    assert backoff.delays == [0.1, 0.2]
    assert all(
        call[0] == job.job_id
        and call[1] == CURRENT_EPOCH
        and call[2] is failure_id
        and call[3] is failure
        for call in application.report_calls
    )
    assert application.submit_calls == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_failure_report_state_fault_is_not_retried(
    code: ErrorCode,
) -> None:
    job = load_job("route")
    failure = ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution record validation failed.",
        retryable=True,
        details=[],
    )
    failure_id = "00000000-0000-0000-0000-000000000124"
    state_error = application_port_error(code)
    application = FakeApplicationService([job])
    application.report_failures.append(state_error)
    runtime = FakeRuntime([RuntimeInfrastructureError(failure_id, failure)])
    backoff = ManualSubmissionBackoff()

    with pytest.raises(ApplicationPortError) as caught:
        _worker(application, runtime, backoff=backoff).execute_one(
            job.job_id,
            CancellationController(),
        )

    assert caught.value is state_error
    assert len(runtime.calls) == 1
    assert application.report_calls == [
        (job.job_id, CURRENT_EPOCH, failure_id, failure)
    ]
    assert application.submit_calls == []
    assert backoff.delays == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_runtime_state_fault_is_fatal_and_creates_no_delivery(
    code: ErrorCode,
) -> None:
    job = load_job("route")
    state_error = application_port_error(code)
    application = FakeApplicationService([job])
    runtime = FakeRuntime([state_error])
    backoff = ManualSubmissionBackoff()
    cancellation = CancellationController()

    with pytest.raises(ApplicationPortError) as caught:
        _worker(application, runtime, backoff=backoff).execute_one(
            job.job_id,
            cancellation,
        )

    assert caught.value is state_error
    assert application.claim_calls == [(job.job_id, CURRENT_EPOCH)]
    assert len(runtime.calls) == 1
    assert application.submit_calls == []
    assert application.report_calls == []
    assert application.jobs[job.job_id].status is JobStatus.RUNNING
    assert backoff.delays == []
    assert not cancellation.retire()


def test_postcommit_null_case_view_still_completes_outcome_delivery() -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    application = FakeApplicationService([job])
    application.outcome_case_view_available = False

    result = _worker(application, FakeRuntime([receipt])).execute_one(
        job.job_id,
        CancellationController(),
    )

    assert result.delivery_completed
    assert result.outcome_disposition is OutcomeDisposition.APPLIED


def test_postcommit_null_case_view_still_completes_failure_report() -> None:
    job = load_job("route")
    failure = ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution record validation failed.",
        retryable=True,
        details=[],
    )
    failure_id = "00000000-0000-0000-0000-000000000125"
    application = FakeApplicationService([job])
    application.failure_case_view_available = False

    result = _worker(
        application,
        FakeRuntime([RuntimeInfrastructureError(failure_id, failure)]),
    ).execute_one(job.job_id, CancellationController())

    assert result.delivery_completed
    assert result.failure_disposition is FailureReportDisposition.APPLIED


@pytest.mark.parametrize(
    ("channel", "code"),
    [
        ("outcome", ErrorCode.IDEMPOTENCY_CONFLICT),
        ("outcome", ErrorCode.VALIDATION_ERROR),
        ("outcome", ErrorCode.STATE_CORRUPT),
        ("outcome", ErrorCode.STATE_SCHEMA_UNSUPPORTED),
        ("report", ErrorCode.VALIDATION_ERROR),
    ],
)
def test_non_retryable_application_port_error_fails_closed_immediately(
    channel: str,
    code: ErrorCode,
) -> None:
    job = load_job("route")
    backoff = ManualSubmissionBackoff()
    application = FakeApplicationService([job])
    if channel == "outcome":
        application.submit_failures.append(application_port_error(code))
        runtime = FakeRuntime([runtime_receipt(load_outcome("route"))])
    else:
        failure = ExecutionFailure(
            stage=ExecutionStage.EXECUTION_RECORD,
            code=ErrorCode.EXECUTION_RECORD_FAILED,
            message="Execution record validation failed.",
            retryable=True,
            details=[],
        )
        application.report_failures.append(application_port_error(code))
        runtime = FakeRuntime(
            [
                RuntimeInfrastructureError(
                    "00000000-0000-0000-0000-000000000123",
                    failure,
                )
            ]
        )

    with pytest.raises(ApplicationPortError) as caught:
        _worker(application, runtime, backoff=backoff).execute_one(
            job.job_id,
            CancellationController(),
        )

    assert caught.value.error.code is code
    assert len(runtime.calls) == 1
    assert backoff.delays == []


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        ErrorCode.REVISION_CONFLICT,
        ErrorCode.STATE_WRITE_FAILED,
    ],
)
def test_claim_application_port_error_is_not_retried_by_the_worker(
    code: ErrorCode,
) -> None:
    job = load_job("route")
    application = FakeApplicationService([job])
    application.claim_failures.append(
        application_port_error(code)
    )
    runtime = FakeRuntime()
    backoff = ManualSubmissionBackoff()

    with pytest.raises(ApplicationPortError) as caught:
        _worker(application, runtime, backoff=backoff).execute_one(
            job.job_id,
            CancellationController(),
        )

    assert caught.value.error.code is code
    assert len(application.claim_calls) == 1
    assert runtime.calls == []
    assert backoff.delays == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_WRITE_FAILED, ErrorCode.EXECUTION_RECORD_FAILED],
)
def test_shutdown_wakes_delivery_backoff_without_another_submission(
    code: ErrorCode,
) -> None:
    job = load_job("route")
    receipt = runtime_receipt(load_outcome("route"))
    application = FakeApplicationService([job])
    application.submit_failures.append(
        application_port_error(code)
    )
    runtime = FakeRuntime([receipt])
    backoff = ManualSubmissionBackoff()
    wait_gate = ManualGate()
    backoff.wait_gate = wait_gate
    worker = _worker(application, runtime, backoff=backoff)
    results = []

    thread = threading.Thread(
        target=lambda: results.append(
            worker.execute_one(job.job_id, CancellationController())
        )
    )
    thread.start()
    assert wait_gate.entered.wait(1.0)

    assert worker.request_shutdown()
    assert not worker.request_shutdown()
    thread.join(1.0)

    assert not thread.is_alive()
    assert len(results) == 1
    assert not results[0].delivery_completed
    assert len(runtime.calls) == 1
    assert len(application.submit_calls) == 1
    assert backoff.delays == [0.1]
    assert backoff.wake_calls == 1


def test_runtime_receipt_must_match_the_claimed_job_before_submission() -> None:
    job = load_job("route")
    other_job = clone_job(
        job,
        job_id="00000000-0000-0000-0000-000000000121",
        case_id="00000000-0000-0000-0000-000000000221",
    )
    other_outcome = clone_outcome(
        load_outcome("route"),
        other_job,
        outcome_id="00000000-0000-0000-0000-000000000321",
    )
    application = FakeApplicationService([job])
    runtime = FakeRuntime([runtime_receipt(other_outcome)])

    with pytest.raises(RuntimeError, match="does not match"):
        _worker(application, runtime).execute_one(
            job.job_id,
            CancellationController(),
        )

    assert len(runtime.calls) == 1
    assert application.submit_calls == []
