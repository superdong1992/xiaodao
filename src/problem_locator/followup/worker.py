"""Hand each report question to a new Generic diagnosis without a model gate."""
from __future__ import annotations

import threading

from problem_locator.contracts import CancellationReason
from problem_locator.diagnostics import log_event
from problem_locator.dispatch.cancellation import CancellationController


class FollowupWorker:
    def __init__(self, service, command, *, backend=None):
        self.service = service
        # Retain injection compatibility; diagnosis owns the only model work.
        self.backend = backend
        self._processing = threading.Lock()
        self._lock = threading.Lock()
        self._active = None

    def cancel(self, *, conversation_id=None, followup_id=None, shutdown=False):
        with self._lock:
            if self._active is None:
                return
            task, signal = self._active
            if (shutdown or (conversation_id is None or task["conversation_id"] == conversation_id)
                    and (followup_id is None or task["followup_id"] == followup_id)):
                signal.cancel(CancellationReason.SERVICE_SHUTDOWN if shutdown else CancellationReason.USER_CANCEL)

    def run_once(self):
        service = self.service
        if not service.enabled or service._stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        task = None
        observation = service.foreground_activity.begin_scan()
        try:
            task = service.store.claim_task()
            if task is None:
                if service.store.last_claim_empty:
                    service.foreground_activity.observed_empty(observation)
                return False
            signal = CancellationController()
            with self._lock:
                self._active = (task, signal)
            with service.agent.usage_guard.acquire(task["conversation_id"]):
                if signal.is_cancelled() or service._stop.is_set():
                    raise InterruptedError()
                return service.dispatch_generic(task)
        except Exception as exc:
            if task is not None:
                try:
                    service.store.finish_task(task["followup_id"], code="AGENT_FOLLOWUP_FAILED",
                        interrupted=service._stop.is_set(), checkpoint=False)
                except Exception:
                    # Never retry an uncertain commit; a committed diagnosis
                    # already has its durable run link.
                    pass
            log_event("agent.followup.failed", model_invoked=False, error_type=type(exc).__name__)
            return False
        finally:
            with self._lock:
                self._active = None
            self._processing.release()
