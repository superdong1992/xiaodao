"""Keep Case resources alive for the complete lifetime of an application read."""

from __future__ import annotations

import threading
from contextlib import ExitStack, nullcontext, suppress
from types import TracebackType
from typing import Self

from problem_locator.contracts.ports import BinaryStream, StateRepository


def case_resource_usage(repository: StateRepository, case_id: str):
    """Use the lifecycle guard when the repository supports history cleanup."""

    acquire = getattr(repository, "case_usage", None)
    return nullcontext() if acquire is None else acquire(case_id)


class CaseResourceStream:
    """Transfer a Case lease with its stream, including cancellation and errors."""

    def __init__(self, stream: BinaryStream, resources: ExitStack) -> None:
        self._stream = stream
        self._resources = resources
        self._lock = threading.RLock()
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
            try:
                return self._stream.read(max_bytes)
            except BaseException:
                with suppress(Exception):
                    self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # ExitStack releases the Case even when the underlying close fails.
            self._resources.close()

    def __enter__(self) -> Self:
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()


__all__ = ["CaseResourceStream", "case_resource_usage"]
