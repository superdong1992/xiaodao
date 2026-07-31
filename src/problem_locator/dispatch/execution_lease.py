"""Process-local execution permit enforcing the V1 global worker limit."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator


class ExecutionPermit:
    """A non-reentrant permit shared by every Worker in one Scheduler service."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()
