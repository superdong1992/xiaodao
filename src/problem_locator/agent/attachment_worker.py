"""Two bounded attachment tasks, each retaining its run signal and file lease."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time

from problem_locator.diagnostics import log_event


class AttachmentWorkRequired(Exception):
    """Hand prepared attachment adoption to a separate bounded task."""

    def __init__(self, resume):
        self.resume = resume
        super().__init__()


@dataclass(eq=False)
class AttachmentTask:
    conversation_id: str
    run_id: str
    signal: object
    lease: object
    resume: object
    state: str = "RESERVED"


class AttachmentWorkers:
    LIMIT = 2

    def __init__(self, agent):
        self.agent = agent
        self._condition = threading.Condition()
        self._tasks = {}
        self._waiting = set()
        self._queue = deque()
        self._threads = []
        self._stopping = False

    def busy(self, cid):
        with self._condition:
            if cid in self._tasks:
                return True
            if cid in self._waiting and len(self._tasks) >= self.LIMIT:
                return True
            self._waiting.discard(cid)
            return False

    def reserve(self, cid, rid, signal, resume):
        with self._condition:
            if self._stopping or cid in self._tasks:
                return None
            if len(self._tasks) >= self.LIMIT:
                # Preparing intake can itself notify the polling worker. Keep
                # that notification from repeatedly replaying database writes
                # while no attachment slot can accept this conversation.
                self._waiting.add(cid)
                return None
            lease = self.agent.usage_guard.acquire(cid)
            lease.__enter__()
            task = AttachmentTask(cid, rid, signal, lease, resume)
            self._tasks[cid] = task
            return task

    def start(self):
        with self._condition:
            if self._threads or self._stopping:
                return
            self._threads = [threading.Thread(target=self._run, name=f"agent-attachments-{i + 1}",
                daemon=True) for i in range(self.LIMIT)]
            for thread in self._threads:
                thread.start()

    def submit_background(self, task):
        """Hand off immediately when started; manual callers submit unlocked."""
        with self._condition:
            if not self._threads:
                return False
            self.submit(task)
            return True

    def submit(self, task):
        with self._condition:
            if self._stopping or self._tasks.get(task.conversation_id) is not task:
                return False
            if self._threads:
                task.state = "QUEUED"
                self._queue.append(task)
                self._condition.notify()
                return True
            # Explicit/manual run_once remains synchronous, but its caller no
            # longer owns the global intake lock. It consumes the same slots.
            task.state = "RUNNING"
        return self._execute(task)

    def _execute(self, task):
        try:
            return self.agent._advance_guarded(task.conversation_id, attachment_task=task)
        finally:
            self.discard(task)

    def discard(self, task):
        with self._condition:
            if self._tasks.get(task.conversation_id) is not task:
                return
            with self.agent._run_lock:
                key = (task.conversation_id, task.run_id)
                if self.agent._active_runs.get(key) is task.signal:
                    self.agent._active_runs.pop(key)
            task.lease.__exit__(None, None, None)
            self._tasks.pop(task.conversation_id)
            self._waiting.clear()
            self._condition.notify_all()
        self.agent._wake.set()
        self.agent._control_wake.set()

    def discard_unsubmitted(self, task):
        with self._condition:
            if task.state == "RESERVED":
                self.discard(task)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._queue)
                if self._stopping:
                    return
                task = self._queue.popleft()
                task.state = "RUNNING"
            try:
                self._execute(task)
            except Exception as error:
                # Scoped errors are already recorded against their frozen run.
                # Storage/control failures must not become a hot retry loop.
                self.agent._failure = type(error).__name__
                log_event("agent.attachment_worker.failed", error_type=type(error).__name__)
                return

    def shutdown(self, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            self._stopping = True
            self._waiting.clear()
            pending = [task for task in self._tasks.values() if task.state != "RUNNING"]
            self._queue.clear()
            for task in pending:
                self.discard(task)
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            empty = self._condition.wait_for(lambda: not self._tasks,
                max(0.0, deadline - time.monotonic()))
        return empty and all(not thread.is_alive() for thread in self._threads)
