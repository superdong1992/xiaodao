"""Runtime epoch generation and process-local installation."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from problem_locator.contracts import IdGenerator


class RuntimeEpochContext:
    """Install exactly one immutable epoch for a Scheduler service instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtime_epoch: str | None = None

    @property
    def current(self) -> str | None:
        with self._lock:
            return self._runtime_epoch

    def install(self, runtime_epoch: str) -> bool:
        with self._lock:
            if self._runtime_epoch is None:
                self._runtime_epoch = runtime_epoch
                return True
            if self._runtime_epoch != runtime_epoch:
                raise RuntimeError("a different runtime epoch is already installed")
            return False

    def require(self) -> str:
        with self._lock:
            if self._runtime_epoch is None:
                raise RuntimeError("runtime epoch is not installed")
            return self._runtime_epoch


class RuntimeEpochFactory:
    """Generate one non-reused epoch from the frozen IdGenerator Port."""

    def __init__(self, id_generator: IdGenerator) -> None:
        self._id_generator = id_generator
        self._runtime_epoch: str | None = None

    def create(self, historical_epochs: Iterable[str]) -> str:
        historical = set(historical_epochs)
        if self._runtime_epoch is not None:
            return self._runtime_epoch
        candidate = self._id_generator.new("runtime_epoch")
        if candidate in historical:
            raise RuntimeError("IdGenerator reused a historical runtime epoch")
        self._runtime_epoch = candidate
        return candidate
