from __future__ import annotations

import collections
import threading
from collections.abc import Callable, Iterable
from typing import Any

from problem_locator.contracts import (
    ApplicationPortError,
    Artifact,
    CancelReceipt,
    ClaimReceipt,
    CommitReceipt,
    DispatchReceipt,
    ErrorCode,
    ExecutionFailure,
    ExecutionFileRef,
    FailureReceipt,
    FailureReportDisposition,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    OutcomeReceipt,
    PORT_ERROR_CODES,
    RecoveryReceipt,
    RuntimeExecutionReceipt,
    StateFile,
    StateMutation,
    ValidationReport,
    canonical_json_bytes,
)

from ._support import case_view_for_job, clone_job


def _raise_scripted_port_failure(
    method: str,
    failures: collections.deque[Exception],
) -> None:
    if not failures:
        return
    error = failures.popleft()
    if not isinstance(error, ApplicationPortError):
        raise AssertionError(f"{method} fake failures must use ApplicationPortError")
    if error.error.code not in PORT_ERROR_CODES[method]:
        raise AssertionError(
            f"{error.error.code.value} is not allowed for {method}"
        )
    raise error


class ManualGate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self._released = threading.Event()

    def arrive_and_wait(self, timeout_seconds: float = 1.0) -> None:
        self.entered.set()
        if not self._released.wait(timeout_seconds):
            raise TimeoutError("ManualGate was not released")

    def release(self) -> None:
        self._released.set()


class FakeApplicationService:
    """Thread-safe JobControlPort fake with only frozen receipt surfaces."""

    def __init__(
        self,
        jobs: Iterable[Job] = (),
        *,
        outcome_dispositions: Iterable[OutcomeDisposition] = (),
        failure_dispositions: Iterable[FailureReportDisposition] = (),
    ) -> None:
        self.jobs = {job.job_id: job for job in jobs}
        self.outcome_dispositions = collections.deque(outcome_dispositions)
        self.failure_dispositions = collections.deque(failure_dispositions)
        self.claim_rejections: set[str] = set()
        self.asset_failures: set[str] = set()
        self.claim_failures: collections.deque[Exception] = collections.deque()
        self.submit_failures: collections.deque[Exception] = collections.deque()
        self.report_failures: collections.deque[Exception] = collections.deque()
        self.interrupt_failures: collections.deque[Exception] = collections.deque()
        self.claim_gate: ManualGate | None = None
        self.submit_gate: ManualGate | None = None
        self.report_gate: ManualGate | None = None
        self.claim_calls: list[tuple[str, str]] = []
        self.submit_calls: list[tuple[JobOutcome, ExecutionFileRef]] = []
        self.report_calls: list[tuple[str, str, str, ExecutionFailure]] = []
        self.interrupt_calls: list[tuple[str, str]] = []
        self.operation_log: list[tuple[str, str]] = []
        self.returned_outcome_dispositions: list[OutcomeDisposition] = []
        self.outcome_case_view_available = True
        self.failure_case_view_available = True
        self.on_claim: Callable[[Job], None] | None = None
        self.on_submit: (
            Callable[[JobOutcome, ExecutionFileRef, OutcomeDisposition], None] | None
        ) = None
        self.on_interrupt: Callable[[str, str], RecoveryReceipt] | None = None
        self._lock = threading.RLock()

    def claim_job(self, job_id: str, runtime_epoch: str) -> ClaimReceipt:
        if self.claim_gate is not None:
            self.claim_gate.arrive_and_wait()
        with self._lock:
            self.claim_calls.append((job_id, runtime_epoch))
            self.operation_log.append(("claim", job_id))
            _raise_scripted_port_failure(
                "JobControlPort.claim_job",
                self.claim_failures,
            )
            if job_id in self.asset_failures:
                return ClaimReceipt(
                    claimed=False,
                    job=None,
                    failure_applied=True,
                    failure_code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
                )
            job = self.jobs.get(job_id)
            if (
                job_id in self.claim_rejections
                or job is None
                or job.status is not JobStatus.PENDING
            ):
                return ClaimReceipt(
                    claimed=False,
                    job=None,
                    failure_applied=False,
                    failure_code=None,
                )
            running = clone_job(job, status=JobStatus.RUNNING, runtime_epoch=runtime_epoch)
            self.jobs[job_id] = running
            if self.on_claim is not None:
                self.on_claim(running)
            return ClaimReceipt(
                claimed=True,
                job=running,
                failure_applied=False,
                failure_code=None,
            )

    def submit_outcome(
        self,
        job_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
    ) -> OutcomeReceipt:
        if self.submit_gate is not None:
            self.submit_gate.arrive_and_wait()
        with self._lock:
            self.submit_calls.append((job_outcome, outcome_file_ref))
            self.operation_log.append(("submit", job_outcome.job_id))
            _raise_scripted_port_failure(
                "JobControlPort.submit_outcome",
                self.submit_failures,
            )
            disposition = (
                self.outcome_dispositions.popleft()
                if self.outcome_dispositions
                else OutcomeDisposition.APPLIED
            )
            if self.on_submit is not None:
                self.on_submit(job_outcome, outcome_file_ref, disposition)
            self.returned_outcome_dispositions.append(disposition)
            job = self.jobs.get(job_outcome.job_id)
            if job is None:
                raise AssertionError("Outcome referenced an unknown fake Job")
            return OutcomeReceipt(
                disposition=disposition,
                case_view=(
                    case_view_for_job(job)
                    if self.outcome_case_view_available
                    else None
                ),
            )

    def report_execution_infrastructure_failure(
        self,
        job_id: str,
        runtime_epoch: str,
        failure_id: str,
        execution_failure: ExecutionFailure,
    ) -> FailureReceipt:
        if self.report_gate is not None:
            self.report_gate.arrive_and_wait()
        with self._lock:
            self.report_calls.append(
                (job_id, runtime_epoch, failure_id, execution_failure)
            )
            self.operation_log.append(("report", job_id))
            _raise_scripted_port_failure(
                "JobControlPort.report_execution_infrastructure_failure",
                self.report_failures,
            )
            disposition = (
                self.failure_dispositions.popleft()
                if self.failure_dispositions
                else FailureReportDisposition.APPLIED
            )
            return FailureReceipt(
                failure_id=failure_id,
                disposition=disposition,
                case_view=(
                    case_view_for_job(self.jobs[job_id])
                    if self.failure_case_view_available
                    else None
                ),
            )

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryReceipt:
        with self._lock:
            self.interrupt_calls.append((current_runtime_epoch, recovery_id))
            self.operation_log.append(("interrupt", recovery_id))
            _raise_scripted_port_failure(
                "JobControlPort.interrupt_previous_epoch",
                self.interrupt_failures,
            )
            if self.on_interrupt is None:
                return RecoveryReceipt(
                    recovery_id=recovery_id,
                    interrupted_job_ids=[],
                    pending_job_ids=[],
                )
            return self.on_interrupt(current_runtime_epoch, recovery_id)


FakeJobClaimer = FakeApplicationService


class FakeRuntime:
    """Scripted Runtime Port fake with cancellation and concurrency probes."""

    def __init__(self, results: Iterable[Any] = ()) -> None:
        self.results = collections.deque(results)
        self.calls: list[tuple[Job, Any]] = []
        self.wait_for_cancellation = False
        self.execution_gate: ManualGate | None = None
        self.cleanup_gate: ManualGate | None = None
        self.observed_cancellation_reasons: list[Any] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def execute(self, job: Job, cancellation: Any) -> RuntimeExecutionReceipt:
        with self._lock:
            self.calls.append((job, cancellation))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.execution_gate is not None:
                self.execution_gate.arrive_and_wait()
            if self.wait_for_cancellation:
                if not cancellation.wait(1.0):
                    raise TimeoutError("cancellation was not delivered")
                self.observed_cancellation_reasons.append(cancellation.reason)
            if self.cleanup_gate is not None:
                self.cleanup_gate.arrive_and_wait()
            if not self.results:
                raise AssertionError("FakeRuntime has no scripted result")
            result = self.results.popleft()
            if isinstance(result, ApplicationPortError):
                if result.error.code not in PORT_ERROR_CODES["Runtime.execute"]:
                    raise AssertionError(
                        f"{result.error.code.value} is not allowed for Runtime.execute"
                    )
                raise result
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                result = result(job, cancellation)
            return result
        finally:
            with self._lock:
                self.active -= 1


class FakeRecoveryView:
    """Mutable StateRepository view used only through the frozen Port methods."""

    def __init__(self, state: StateFile) -> None:
        self._state = state
        self.read_snapshot_calls = 0
        self.read_snapshot_script: collections.deque[Exception | None] = (
            collections.deque()
        )
        self._lock = threading.RLock()

    def replace(self, state: StateFile) -> None:
        with self._lock:
            self._state = state

    def read_snapshot(self) -> StateFile:
        with self._lock:
            self.read_snapshot_calls += 1
            if self.read_snapshot_script:
                scripted = self.read_snapshot_script.popleft()
                if scripted is not None:
                    if not isinstance(scripted, ApplicationPortError):
                        raise AssertionError(
                            "StateRepository.read_snapshot fake failures must use "
                            "ApplicationPortError"
                        )
                    if (
                        scripted.error.code
                        not in PORT_ERROR_CODES["StateRepository.read_snapshot"]
                    ):
                        raise AssertionError(
                            f"{scripted.error.code.value} is not allowed for "
                            "StateRepository.read_snapshot"
                        )
                    raise scripted
            return self._state.model_copy(deep=True)

    def read_case(self, case_id: str):
        with self._lock:
            return self._state.cases[case_id].model_copy(deep=True)

    def read_job(self, job_id: str) -> Job:
        with self._lock:
            for aggregate in self._state.cases.values():
                if job_id in aggregate.jobs:
                    return aggregate.jobs[job_id].model_copy(deep=True)
        raise KeyError(job_id)

    def read_artifact(self, artifact_id: str) -> Artifact:
        with self._lock:
            for aggregate in self._state.cases.values():
                if artifact_id in aggregate.artifacts:
                    return aggregate.artifacts[artifact_id].model_copy(deep=True)
        raise KeyError(artifact_id)

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> CommitReceipt:
        raise AssertionError("S05 must not call StateRepository.commit")

    def validate_all(self) -> ValidationReport:
        raise NotImplementedError

    def export_snapshot(self) -> bytes:
        with self._lock:
            return canonical_json_bytes(self._state)


class DeterministicEpochFactory:
    def __init__(self, *epochs: str) -> None:
        self.epochs = collections.deque(epochs or (CURRENT_EPOCH,))
        self.calls: list[tuple[str, ...]] = []
        self._current: str | None = None

    def create(self, historical_epochs: Iterable[str]) -> str:
        self.calls.append(tuple(historical_epochs))
        if self._current is not None:
            return self._current
        if not self.epochs:
            raise AssertionError("no deterministic epoch remains")
        self._current = self.epochs.popleft()
        return self._current


class ManualSubmissionBackoff:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.shutdown = False
        self.wake_calls = 0
        self.wait_gate: ManualGate | None = None

    def wait(self, delay_seconds: float) -> bool:
        self.delays.append(delay_seconds)
        if self.wait_gate is not None:
            self.wait_gate.arrive_and_wait()
        return not self.shutdown

    def wake_for_shutdown(self) -> None:
        self.wake_calls += 1
        self.shutdown = True
        if self.wait_gate is not None:
            self.wait_gate.release()


class RecordingDispatcher:
    """Dispatcher fake used by recovery without bypassing production semantics."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self._known: set[str] = set()
        self.cancelled: list[str] = []
        self.reject_ids: set[str] = set()
        self.claiming_enabled = False

    def submit(self, job_id: str) -> DispatchReceipt:
        if job_id in self._known:
            return DispatchReceipt(job_id=job_id, accepted=False, duplicate=True)
        if job_id in self.reject_ids:
            return DispatchReceipt(job_id=job_id, accepted=False, duplicate=False)
        self._known.add(job_id)
        self.submitted.append(job_id)
        return DispatchReceipt(job_id=job_id, accepted=True, duplicate=False)

    def cancel(self, job_id: str) -> CancelReceipt:
        if job_id not in self._known:
            return CancelReceipt(job_id=job_id, signalled=False)
        self._known.remove(job_id)
        self.cancelled.append(job_id)
        return CancelReceipt(job_id=job_id, signalled=True)

    def pause_claiming(self) -> None:
        self.claiming_enabled = False

    def enable_claiming(self) -> None:
        self.claiming_enabled = True
