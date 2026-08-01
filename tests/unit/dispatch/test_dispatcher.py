from __future__ import annotations

import hashlib
import json
import threading

from problem_locator.contracts import (
    Dispatcher,
    ErrorCode,
    FixtureManifest,
    JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    PORT_ERROR_CODES,
    canonical_json_bytes,
)
from problem_locator.dispatch import (
    InProcessDispatcher,
    InterruptibleSubmissionBackoff,
    submission_backoff_delay,
)

from ._support import DISPATCH_FIXTURES, load_dispatch_fixture


JOB_IDS = [
    "00000000-0000-0000-0000-000000000110",
    "00000000-0000-0000-0000-000000000111",
    "00000000-0000-0000-0000-000000000112",
]


class _RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute_one(self, job_id, _cancellation):
        self.calls.append(job_id)

    def request_shutdown(self) -> bool:
        return True


def test_dispatcher_is_fifo_idempotent_and_only_queues_job_ids() -> None:
    worker = _RecordingWorker()
    dispatcher = InProcessDispatcher(worker)  # type: ignore[arg-type]
    assert isinstance(dispatcher, Dispatcher)
    dispatcher.start()

    receipts = [dispatcher.submit(job_id) for job_id in JOB_IDS]
    duplicate = dispatcher.submit(JOB_IDS[1])

    assert all(receipt.accepted and not receipt.duplicate for receipt in receipts)
    assert not duplicate.accepted and duplicate.duplicate
    assert dispatcher.queued_job_ids == tuple(JOB_IDS)
    assert worker.calls == []  # Recovery has not enabled claiming yet.

    dispatcher.enable_claiming()
    assert dispatcher.wait_until_idle(1.0)
    assert worker.calls == JOB_IDS
    assert dispatcher.shutdown(1.0)


def test_idle_is_not_observable_until_fatal_callback_completes() -> None:
    callback_entered = threading.Event()
    callback_release = threading.Event()
    callback_calls: list[tuple[str, Exception]] = []

    class _FailingWorker(_RecordingWorker):
        def execute_one(self, job_id, _cancellation):
            self.calls.append(job_id)
            raise RuntimeError("fatal worker failure")

    def record_fatal(job_id: str, error: Exception) -> None:
        callback_calls.append((job_id, error))
        callback_entered.set()
        assert callback_release.wait(1.0)

    worker = _FailingWorker()
    dispatcher = InProcessDispatcher(
        worker,  # type: ignore[arg-type]
        on_fatal_worker_error=record_fatal,
    )
    dispatcher.start()
    dispatcher.submit(JOB_IDS[0])
    dispatcher.enable_claiming()

    assert callback_entered.wait(1.0)
    assert dispatcher.running_job_id == JOB_IDS[0]
    assert not dispatcher.claiming_enabled
    assert not dispatcher.cancel(JOB_IDS[0]).signalled
    assert not dispatcher.wait_until_idle(0.0)

    callback_release.set()
    assert dispatcher.wait_until_idle(1.0)
    assert len(callback_calls) == 1
    callback_job_id, callback_error = callback_calls[0]
    assert callback_job_id == JOB_IDS[0]
    assert isinstance(callback_error, RuntimeError)
    assert not dispatcher.claiming_enabled
    assert dispatcher.shutdown(1.0)


def test_pending_cancel_removes_only_the_queue_signal() -> None:
    worker = _RecordingWorker()
    dispatcher = InProcessDispatcher(worker)  # type: ignore[arg-type]
    dispatcher.start()
    dispatcher.submit(JOB_IDS[0])

    assert dispatcher.cancel(JOB_IDS[0]).signalled
    assert not dispatcher.cancel(JOB_IDS[0]).signalled
    dispatcher.enable_claiming()
    assert dispatcher.wait_until_idle(1.0)
    assert worker.calls == []
    assert dispatcher.shutdown(1.0)


def test_shutdown_rejects_new_dispatch_without_rolling_back_business_state() -> None:
    dispatcher = InProcessDispatcher(_RecordingWorker())  # type: ignore[arg-type]
    dispatcher.start()
    assert dispatcher.shutdown(1.0)

    rejected = dispatcher.submit(JOB_IDS[0])
    assert not rejected.accepted
    assert not rejected.duplicate


def test_frozen_submission_backoff_sequence_and_shutdown_wakeup() -> None:
    assert [submission_backoff_delay(index) for index in range(9)] == [
        0.1,
        0.2,
        0.5,
        1.0,
        2.0,
        5.0,
        5.0,
        5.0,
        5.0,
    ]

    backoff = InterruptibleSubmissionBackoff()
    completed: list[bool] = []
    started = threading.Event()

    def wait() -> None:
        started.set()
        completed.append(backoff.wait(5.0))

    thread = threading.Thread(target=wait)
    thread.start()
    assert started.wait(1.0)
    backoff.wake_for_shutdown()
    thread.join(1.0)
    assert not thread.is_alive()
    assert completed == [False]


def test_delivery_error_policy_fixture_matches_r3_public_error_sets() -> None:
    policies = load_dispatch_fixture("delivery-error-policy.json")["policies"]
    retry_rows = [row for row in policies if row["action"] == "RETRY_SAME_RECEIPT"]
    park_rows = [row for row in policies if row["action"] == "PARK_UNTIL_RESTART"]
    runtime_rows = [row for row in policies if row["channel"] == "Runtime.execute"]

    assert {ErrorCode(row["code"]) for row in retry_rows} == (
        JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES
    )
    assert {ErrorCode(row["code"]) for row in park_rows} == (
        JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES
    )
    assert {ErrorCode(row["code"]) for row in runtime_rows} == PORT_ERROR_CODES[
        "Runtime.execute"
    ]
    assert all(row["automatic_retry"] for row in retry_rows)
    assert all(not row["automatic_retry"] for row in park_rows + runtime_rows)
    assert all(not row["rerun_runtime"] for row in policies)
    assert all(not row["creates_outcome"] for row in policies)
    assert all(
        row["preserve_finalized_outbox"]
        == (row["channel"] == "JobControlPort.submit_outcome")
        for row in policies
    )


def test_dispatch_fixture_manifest_matches_every_owned_file() -> None:
    manifest_path = DISPATCH_FIXTURES / "fixture-manifest.json"
    manifest = FixtureManifest.model_validate_json(manifest_path.read_bytes())
    actual_files = sorted(
        path
        for path in DISPATCH_FIXTURES.iterdir()
        if path.is_file() and path.name != manifest_path.name
    )

    assert manifest.root == "tests/fixtures/components/dispatch-scheduler"
    assert [entry.path for entry in manifest.files] == [
        path.name for path in actual_files
    ]
    for entry, path in zip(manifest.files, actual_files, strict=True):
        payload = path.read_bytes()
        assert entry.size == len(payload)
        assert entry.sha256 == hashlib.sha256(payload).hexdigest()
        assert canonical_json_bytes(json.loads(payload)) == payload
