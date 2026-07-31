"""Injectable, shutdown-aware submission backoff primitives."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable


SUBMISSION_BACKOFF_DELAYS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)


def submission_backoff_delay(attempt_index: int) -> float:
    """Return the frozen delay for a zero-based failed submission attempt."""

    if attempt_index < 0:
        raise ValueError("attempt_index must be non-negative")
    return SUBMISSION_BACKOFF_DELAYS[min(attempt_index, len(SUBMISSION_BACKOFF_DELAYS) - 1)]


@runtime_checkable
class SubmissionBackoff(Protocol):
    """Wait between submissions; false means shutdown woke the wait."""

    def wait(self, delay_seconds: float) -> bool: ...

    def wake_for_shutdown(self) -> None: ...


class InterruptibleSubmissionBackoff:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._shutdown = False

    def wait(self, delay_seconds: float) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        with self._condition:
            if self._shutdown:
                return False
            woke_for_shutdown = self._condition.wait_for(
                lambda: self._shutdown,
                timeout=delay_seconds,
            )
            return not woke_for_shutdown

    def wake_for_shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
