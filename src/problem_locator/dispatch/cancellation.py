"""Thread-safe, first-reason-wins cancellation control."""

from __future__ import annotations

import threading

from problem_locator.contracts import CancellationReason


class CancellationController:
    """Mutable S05 controller exposing only the frozen signal surface to Runtime."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._reason: CancellationReason | None = None
        self._retired = False

    @property
    def reason(self) -> CancellationReason | None:
        with self._condition:
            return self._reason

    def is_cancelled(self) -> bool:
        with self._condition:
            return self._reason is not None

    def wait(self, timeout_seconds: float | None) -> bool:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        with self._condition:
            if self._reason is not None:
                return True
            self._condition.wait_for(
                lambda: self._reason is not None,
                timeout=timeout_seconds,
            )
            return self._reason is not None

    def cancel(self, reason: CancellationReason) -> bool:
        if not isinstance(reason, CancellationReason):
            raise TypeError("reason must be a CancellationReason")
        with self._condition:
            if self._retired or self._reason is not None:
                return False
            self._reason = reason
            self._condition.notify_all()
            return True

    def retire(self) -> bool:
        """Close the mutable controller as soon as Runtime execution ends."""

        with self._condition:
            if self._retired:
                return False
            self._retired = True
            return True
