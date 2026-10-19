"""Remember idle queue observations without polling storage from background work."""
from __future__ import annotations

import threading


class QueueIdleObservation:
    """An empty scan is valid only until a newer queue notification arrives."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._idle = False

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._idle = False

    def begin_scan(self):
        with self._lock:
            self._idle = False
            return self._generation

    def observed_empty(self, generation):
        with self._lock:
            if generation == self._generation:
                self._idle = True

    @property
    def idle(self):
        with self._lock:
            return self._idle
