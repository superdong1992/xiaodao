"""Independent ROUTE/DIAGNOSE queues with one execution per Case."""
from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable

from problem_locator.contracts import CancelReceipt, CancellationReason, DispatchReceipt, JobType
from problem_locator.diagnostics import log_event
from problem_locator.journey import record_journey_event
from .cancellation import CancellationController
from .worker import JobWorker

FatalWorkerHandler = Callable[[str, Exception], None]


class InProcessDispatcher:
    def __init__(self, worker: JobWorker, *,
                 job_identity: Callable[[str], tuple[str, JobType]] | None = None,
                 route_workers: int = 1, diagnose_workers: int = 2,
                 on_fatal_worker_error: FatalWorkerHandler | None = None,
                 thread_name: str = "problem-locator-job-worker") -> None:
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in (route_workers, diagnose_workers)):
            raise ValueError("worker counts must be positive integers")
        self._worker = worker
        self._job_identity = job_identity or (lambda key: (key, JobType.DIAGNOSE))
        self._counts = {"ROUTE": route_workers, "DIAGNOSE": diagnose_workers}
        self._on_fatal_worker_error = on_fatal_worker_error
        self._thread_name = thread_name
        self._condition = threading.Condition()
        self._queues = {name: collections.deque() for name in self._counts}
        self._queued: dict[str, tuple[str, str, float]] = {}
        self._running: dict[str, tuple[str, CancellationController]] = {}
        self._active_cases: set[str] = set()
        self._finishing: set[str] = set()
        self._accepting = True
        self._claiming_enabled = False
        self._stop_requested = False
        self._threads: list[threading.Thread] = []

    @property
    def claiming_enabled(self) -> bool:
        with self._condition:
            return self._claiming_enabled and not self._stop_requested

    @property
    def running_job_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._running)

    @property
    def running_job_id(self) -> str | None:
        return next(iter(self.running_job_ids), None)

    @property
    def queued_job_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._queued)

    def start(self) -> None:
        with self._condition:
            if self._threads:
                return
            for lane, count in self._counts.items():
                for index in range(count):
                    thread = threading.Thread(target=self._run, args=(lane,),
                        name=f"{self._thread_name}-{lane.lower()}-{index}", daemon=True)
                    self._threads.append(thread)
                    thread.start()

    def submit(self, job_id: str) -> DispatchReceipt:
        case_id, job_type = self._job_identity(job_id)
        lane = "ROUTE" if job_type is JobType.ROUTE else "DIAGNOSE"
        with self._condition:
            if job_id in self._queued or job_id in self._running:
                return DispatchReceipt(job_id=job_id, accepted=False, duplicate=True)
            if not self._accepting:
                return DispatchReceipt(job_id=job_id, accepted=False, duplicate=False)
            self._queues[lane].append(job_id)
            self._queued[job_id] = (case_id, lane, time.perf_counter())
            self._condition.notify_all()
            return DispatchReceipt(job_id=job_id, accepted=True, duplicate=False)

    def cancel(self, job_id: str) -> CancelReceipt:
        with self._condition:
            queued = self._queued.pop(job_id, None)
            if queued is not None:
                self._queues[queued[1]].remove(job_id)
                self._condition.notify_all()
                return CancelReceipt(job_id=job_id, signalled=True)
            current = self._running.get(job_id)
            return CancelReceipt(job_id=job_id, signalled=False if current is None or job_id in self._finishing
                else current[1].cancel(CancellationReason.USER_CANCEL))

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
        with self._condition:
            return self._condition.wait_for(lambda: not self._queued and not self._running, timeout_seconds)

    def shutdown(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        self._worker.request_shutdown()
        with self._condition:
            self._accepting = self._claiming_enabled = False
            self._stop_requested = True
            self._queued.clear()
            for queue in self._queues.values():
                queue.clear()
            for _, cancellation in self._running.values():
                cancellation.cancel(CancellationReason.SERVICE_SHUTDOWN)
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)

    def _eligible(self, lane: str) -> str | None:
        if not self._claiming_enabled:
            return None
        return next((key for key in self._queues[lane]
                     if self._queued[key][0] not in self._active_cases), None)

    def _run(self, lane: str) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stop_requested or self._eligible(lane) is not None)
                if self._stop_requested:
                    return
                job_id = self._eligible(lane)
                assert job_id is not None
                self._queues[lane].remove(job_id)
                case_id, _, queued_at = self._queued.pop(job_id)
                cancellation = CancellationController()
                self._running[job_id] = (case_id, cancellation)
                self._active_cases.add(case_id)
            started = time.perf_counter()
            record_journey_event("job.queue.dequeued", case_id=case_id, job_id=job_id,
                duration_ms=(started - queued_at) * 1000, data={"queue": lane})
            log_event("worker.job.started", job_id=job_id)
            try:
                self._worker.execute_one(job_id, cancellation)
            except Exception as error:
                self.pause_claiming()
                with self._condition:
                    self._finishing.add(job_id)
                if self._on_fatal_worker_error is not None:
                    self._on_fatal_worker_error(job_id, error)
            else:
                log_event("worker.job.completed", job_id=job_id,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3))
            finally:
                with self._condition:
                    self._running.pop(job_id, None)
                    self._finishing.discard(job_id)
                    self._active_cases.discard(case_id)
                    self._condition.notify_all()
