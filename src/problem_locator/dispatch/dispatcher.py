"""Deterministic FIFO Dispatcher backed by one process-local worker thread."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable

from problem_locator.contracts import (
    ACTIVE_WORKERS,
    CancelReceipt,
    CancellationReason,
    DispatchReceipt,
)

from .cancellation import CancellationController
from .worker import JobWorker


FatalWorkerHandler = Callable[[str, Exception], None]


class InProcessDispatcher:
    """Queue only persisted Job IDs and globally execute at concurrency one."""

    def __init__(
        self,
        worker: JobWorker,
        *,
        on_fatal_worker_error: FatalWorkerHandler | None = None,
        thread_name: str = "problem-locator-job-worker",
    ) -> None:
        if ACTIVE_WORKERS != 1:
            raise RuntimeError("V1 requires ACTIVE_WORKERS=1")
        self._worker = worker
        self._on_fatal_worker_error = on_fatal_worker_error
        self._thread_name = thread_name
        self._condition = threading.Condition()
        self._queue: collections.deque[str] = collections.deque()
        self._queued: set[str] = set()
        self._running_job_id: str | None = None
        self._running_cancellation: CancellationController | None = None
        self._accepting = True
        self._claiming_enabled = False
        self._stop_requested = False
        self._thread: threading.Thread | None = None

    @property
    def claiming_enabled(self) -> bool:
        with self._condition:
            return self._claiming_enabled and not self._stop_requested

    @property
    def running_job_id(self) -> str | None:
        with self._condition:
            return self._running_job_id

    @property
    def queued_job_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._queue)

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()

    def submit(self, job_id: str) -> DispatchReceipt:
        with self._condition:
            if job_id in self._queued or job_id == self._running_job_id:
                return DispatchReceipt(job_id=job_id, accepted=False, duplicate=True)
            if not self._accepting:
                return DispatchReceipt(job_id=job_id, accepted=False, duplicate=False)
            self._queue.append(job_id)
            self._queued.add(job_id)
            self._condition.notify_all()
            return DispatchReceipt(job_id=job_id, accepted=True, duplicate=False)

    def cancel(self, job_id: str) -> CancelReceipt:
        with self._condition:
            if job_id in self._queued:
                self._queue.remove(job_id)
                self._queued.remove(job_id)
                self._condition.notify_all()
                return CancelReceipt(job_id=job_id, signalled=True)
            if job_id != self._running_job_id or self._running_cancellation is None:
                return CancelReceipt(job_id=job_id, signalled=False)
            signalled = self._running_cancellation.cancel(CancellationReason.USER_CANCEL)
            return CancelReceipt(job_id=job_id, signalled=signalled)

    def pause_claiming(self) -> None:
        with self._condition:
            self._claiming_enabled = False
            self._condition.notify_all()

    def enable_claiming(self) -> None:
        with self._condition:
            if self._stop_requested:
                raise RuntimeError("cannot enable claiming during shutdown")
            self._claiming_enabled = True
            self._condition.notify_all()

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._queue and self._running_job_id is None,
                timeout=max(0.0, deadline - time.monotonic()),
            )

    def shutdown(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._worker.request_shutdown()
        with self._condition:
            self._accepting = False
            self._claiming_enabled = False
            self._stop_requested = True
            self._queue.clear()
            self._queued.clear()
            if self._running_cancellation is not None:
                self._running_cancellation.cancel(CancellationReason.SERVICE_SHUTDOWN)
            self._condition.notify_all()
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stop_requested
                    or (self._claiming_enabled and bool(self._queue))
                )
                if self._stop_requested:
                    return
                job_id = self._queue.popleft()
                self._queued.remove(job_id)
                cancellation = CancellationController()
                self._running_job_id = job_id
                self._running_cancellation = cancellation

            fatal_error: Exception | None = None
            try:
                self._worker.execute_one(job_id, cancellation)
            except Exception as exc:  # fail closed; never retry Runtime implicitly
                fatal_error = exc
            finally:
                if fatal_error is not None:
                    with self._condition:
                        self._running_cancellation = None
                        self._claiming_enabled = False
                try:
                    if (
                        fatal_error is not None
                        and self._on_fatal_worker_error is not None
                    ):
                        self._on_fatal_worker_error(job_id, fatal_error)
                finally:
                    with self._condition:
                        self._running_job_id = None
                        self._running_cancellation = None
                        self._condition.notify_all()
