from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
import time


class ChildPidReadyMonotonic:
    """Advance test time only after a descendant PID is atomically observable."""

    def __init__(
        self,
        marker: Path,
        *,
        setup_timeout_seconds: float = 5.0,
        real_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if setup_timeout_seconds <= 0:
            raise ValueError("setup_timeout_seconds must be positive")
        self._marker = marker
        self._setup_timeout_seconds = setup_timeout_seconds
        self._real_monotonic = real_monotonic
        self._real_started = real_monotonic()
        self._baseline = self._real_started
        self._ready_at: float | None = None
        self._ready_pid: int | None = None
        self._lock = threading.Lock()

    @property
    def ready_pid(self) -> int | None:
        with self._lock:
            return self._ready_pid

    def __call__(self) -> float:
        now = self._real_monotonic()
        with self._lock:
            if self._ready_at is None:
                ready_pid = self._read_ready_pid()
                if ready_pid is not None:
                    self._ready_pid = ready_pid
                    self._ready_at = now
                elif now - self._real_started < self._setup_timeout_seconds:
                    return self._baseline
                else:
                    # Release the logical clock after the independent setup
                    # ceiling so a broken fixture fails instead of hanging.
                    return self._baseline + (now - self._real_started)
            assert self._ready_at is not None
            return self._baseline + (now - self._ready_at)

    def _read_ready_pid(self) -> int | None:
        try:
            raw = self._marker.read_text(encoding="ascii")
        except (FileNotFoundError, OSError, UnicodeError):
            return None
        if not raw.isdecimal():
            return None
        pid = int(raw)
        return pid if pid > 0 else None
