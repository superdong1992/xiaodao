"""Independent service-shutdown state shared across dispatch components."""

from __future__ import annotations

import threading


class SchedulerShutdownSignal:
    """Track service shutdown independently of first-wins Job cancellation."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()

    def request(self) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            return True

    def is_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        return self._event.wait(timeout_seconds)
