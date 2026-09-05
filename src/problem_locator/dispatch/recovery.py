"""Fresh process epoch startup; active Jobs and old outboxes are never replayed."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import (
    ErrorCode,
    ExecutionRecordStore,
    JobControlPort,
    StateRepository,
)

from .backoff import (
    InterruptibleSubmissionBackoff,
    SubmissionBackoff,
)
from .dispatcher import InProcessDispatcher
from .runtime_epoch import RuntimeEpochContext, RuntimeEpochFactory
from .shutdown import SchedulerShutdownSignal


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    runtime_epoch: str | None
    completed: bool
    replayed_job_ids: tuple[str, ...]
    interrupted_job_ids: tuple[str, ...]
    pending_job_ids: tuple[str, ...]
    failure_type: str | None = None
    failure_code: ErrorCode | None = None


class RecoveryCoordinator:
    """Install a new epoch without reading historical state or execution records."""

    def __init__(
        self,
        repository: StateRepository,
        execution_records: ExecutionRecordStore,
        job_control: JobControlPort,
        dispatcher: InProcessDispatcher,
        epoch_factory: RuntimeEpochFactory,
        epoch_context: RuntimeEpochContext,
        shutdown_signal: SchedulerShutdownSignal | None = None,
        submission_backoff: SubmissionBackoff | None = None,
    ) -> None:
        self._repository = repository
        self._execution_records = execution_records
        self._job_control = job_control
        self._dispatcher = dispatcher
        self._epoch_factory = epoch_factory
        self._epoch_context = epoch_context
        self._shutdown_signal = (
            shutdown_signal
            if shutdown_signal is not None
            else SchedulerShutdownSignal()
        )
        self._submission_backoff = (
            submission_backoff
            if submission_backoff is not None
            else InterruptibleSubmissionBackoff()
        )

    def recover(self) -> RecoveryResult:
        """Start a fresh process epoch; active work is deliberately not replayed."""
        self._dispatcher.pause_claiming()
        runtime_epoch = self._epoch_factory.create(())
        self._epoch_context.install(runtime_epoch)
        self._dispatcher.enable_claiming()
        return RecoveryResult(runtime_epoch=runtime_epoch, completed=True,
            replayed_job_ids=(), interrupted_job_ids=(), pending_job_ids=())
