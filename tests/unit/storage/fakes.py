"""Observable, production-independent test doubles for the S02 storage slice.

The helpers in this module intentionally model only injectable operating-system
boundaries.  They do not import any concrete storage adapter and they reuse the
S00 clock, ID generator, and logical byte stream rather than creating a second
public contract surface.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Self, TypeAlias

from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock as _S00FakeClock,
)
from tests.contracts.scenario_fakes import (
    CountingBinaryStream as _S00CountingBinaryStream,
)


Pathish: TypeAlias = str | os.PathLike[str]
FileSyncTarget: TypeAlias = Pathish | BinaryIO
FileSyncOperation: TypeAlias = Literal[
    "sync_file",
    "sync_directory",
    "readonly_file",
    "readonly_directory",
]


class FixedClock(_S00FakeClock):
    """S02 spelling of the shared fixed/scriptable S00 ``FakeClock``."""


# The S00 stream already retains one 1 MiB chunk regardless of logical size and
# implements the frozen forward-only BinaryStream protocol.  Re-exporting the
# exact class keeps 2.5 GiB/5 GiB tests cheap without duplicating that protocol.
CountingBinaryStream = _S00CountingBinaryStream


@dataclass(frozen=True, slots=True)
class FileSyncEvent:
    """One attempted file durability or read-only operation."""

    operation: FileSyncOperation
    path: Path
    occurrence: int


class FakeFileSync:
    """Record file/dir sync and read-only calls with exact fault scheduling.

    Fault occurrence numbers are absolute and one-based per operation.  The
    failed call is recorded before the configured exception is raised, allowing
    tests to assert the precise durability boundary that was reached.
    """

    OPERATIONS = frozenset(
        {"sync_file", "sync_directory", "readonly_file", "readonly_directory"}
    )

    def __init__(self) -> None:
        self.events: list[FileSyncEvent] = []
        self._counts: dict[FileSyncOperation, int] = {
            "sync_file": 0,
            "sync_directory": 0,
            "readonly_file": 0,
            "readonly_directory": 0,
        }
        self._failures: dict[tuple[FileSyncOperation, int], BaseException] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_operation(operation: str) -> FileSyncOperation:
        if operation not in FakeFileSync.OPERATIONS:
            raise ValueError(f"unsupported file-sync operation: {operation!r}")
        return operation  # type: ignore[return-value]

    def fail_on(
        self,
        operation: FileSyncOperation,
        occurrence: int,
        error: BaseException | None = None,
    ) -> None:
        """Fail exactly the specified one-based call of *operation*."""

        parsed = self._validate_operation(operation)
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence <= 0:
            raise ValueError("occurrence must be a positive integer")
        failure = error or OSError(f"injected {parsed} failure #{occurrence}")
        with self._lock:
            key = (parsed, occurrence)
            if occurrence <= self._counts[parsed]:
                raise ValueError("cannot schedule a fault for an occurrence already observed")
            if key in self._failures:
                raise ValueError("a fault is already scheduled for that occurrence")
            self._failures[key] = failure

    def fail_next(
        self,
        operation: FileSyncOperation,
        error: BaseException | None = None,
    ) -> int:
        """Fail the next call and return its one-based occurrence number."""

        parsed = self._validate_operation(operation)
        with self._lock:
            occurrence = self._counts[parsed] + 1
        self.fail_on(parsed, occurrence, error)
        return occurrence

    @staticmethod
    def _path_from_file_target(target: FileSyncTarget) -> Path:
        if isinstance(target, (str, os.PathLike)):
            return Path(target)
        name = getattr(target, "name", None)
        if isinstance(name, (str, os.PathLike)):
            return Path(name)
        raise TypeError("file-sync handle must expose a filesystem name")

    def _record(self, operation: FileSyncOperation, path: Pathish) -> None:
        parsed = self._validate_operation(operation)
        with self._lock:
            occurrence = self._counts[parsed] + 1
            self._counts[parsed] = occurrence
            self.events.append(FileSyncEvent(parsed, Path(path), occurrence))
            failure = self._failures.pop((parsed, occurrence), None)
        if failure is not None:
            raise failure

    def sync_file(self, path_or_handle: FileSyncTarget) -> None:
        self._record("sync_file", self._path_from_file_target(path_or_handle))

    def sync_directory(self, path: Pathish) -> None:
        self._record("sync_directory", path)

    def set_read_only(self, path: Pathish, *, is_directory: bool = False) -> None:
        self._record("readonly_directory" if is_directory else "readonly_file", path)

    def set_file_read_only(self, path: Pathish) -> None:
        self.set_read_only(path, is_directory=False)

    def set_directory_read_only(self, path: Pathish) -> None:
        self.set_read_only(path, is_directory=True)

    def make_read_only(self, path: Pathish) -> None:
        target = Path(path)
        self.set_read_only(target, is_directory=target.is_dir())

    # These aliases make the helper convenient for adapters that spell sync
    # after its underlying system call.
    fsync_file = sync_file
    fsync_directory = sync_directory

    def count(self, operation: FileSyncOperation) -> int:
        parsed = self._validate_operation(operation)
        with self._lock:
            return self._counts[parsed]

    def calls(self, operation: FileSyncOperation | None = None) -> tuple[FileSyncEvent, ...]:
        with self._lock:
            if operation is None:
                return tuple(self.events)
            parsed = self._validate_operation(operation)
            return tuple(event for event in self.events if event.operation == parsed)


@dataclass(frozen=True, slots=True)
class InstanceLockEvent:
    """One acquire/release observation from the logical instance-lock backend."""

    action: Literal["acquired", "blocked", "released"]
    path: Path
    owner_id: int | None


class FakeInstanceLockHandle:
    """Lifetime handle returned by :class:`FakeInstanceLockBackend`."""

    def __init__(
        self,
        backend: FakeInstanceLockBackend,
        path: Path,
        identity: tuple[object, ...],
        owner_id: int,
    ) -> None:
        self._backend = backend
        self.path = path
        self._identity = identity
        self.owner_id = owner_id
        self._released = False
        self._state_lock = threading.Lock()

    @property
    def released(self) -> bool:
        with self._state_lock:
            return self._released

    def release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._backend._release(self)
            self._released = True

    close = release

    def __enter__(self) -> Self:
        if self.released:
            raise RuntimeError("instance-lock handle is already released")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class FakeInstanceLockBackend:
    """Non-blocking backend for both FileInstanceLock and direct fake use.

    ``acquire(open_handle)``/``release(open_handle)`` match the production
    backend protocol.  Passing a path instead returns a lifetime handle, which
    is convenient for storage tests that do not need to open a real file.
    Both forms contend on the normalized lock-file name.
    """

    def __init__(self) -> None:
        self.events: list[InstanceLockEvent] = []
        self._active: dict[tuple[object, ...], tuple[object, int, Path]] = {}
        self._next_owner_id = 1
        self._acquire_calls = 0
        self._acquire_failures: dict[int, BaseException] = {}
        self._lock = threading.Lock()

    @property
    def acquire_calls(self) -> int:
        with self._lock:
            return self._acquire_calls

    def fail_on_acquire(
        self,
        occurrence: int,
        error: BaseException | None = None,
    ) -> None:
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence <= 0:
            raise ValueError("occurrence must be a positive integer")
        with self._lock:
            if occurrence <= self._acquire_calls:
                raise ValueError("cannot schedule an acquire fault in the past")
            if occurrence in self._acquire_failures:
                raise ValueError("an acquire fault is already scheduled for that occurrence")
            self._acquire_failures[occurrence] = error or OSError(
                f"injected instance-lock acquire failure #{occurrence}"
            )

    @staticmethod
    def _target_identity_and_path(
        target: Pathish | BinaryIO,
    ) -> tuple[tuple[object, ...], Path]:
        if isinstance(target, (str, os.PathLike)):
            path = Path(target)
            return ("path", path), path
        descriptor = target.fileno()
        descriptor_stat = os.fstat(descriptor)
        name = getattr(target, "name", None)
        if isinstance(name, (str, os.PathLike)):
            event_path = Path(name)
        else:
            # ``os.fdopen`` exposes the integer descriptor as ``name``.  The
            # inode identity below still makes separately opened handles to
            # the same lock file contend correctly.
            event_path = Path(f"<fd:{descriptor}>")
        return ("inode", descriptor_stat.st_dev, descriptor_stat.st_ino), event_path

    def acquire(
        self,
        target: Pathish | BinaryIO,
    ) -> FakeInstanceLockHandle | None:
        identity, normalized = self._target_identity_and_path(target)
        direct_path_call = isinstance(target, (str, os.PathLike))
        with self._lock:
            self._acquire_calls += 1
            failure = self._acquire_failures.pop(self._acquire_calls, None)
            if failure is not None:
                raise failure
            active = self._active.get(identity)
            # Membership is the backend's lock authority.  Avoid consulting
            # the handle's separate state lock while holding this lock, which
            # would invert the order used by ``handle.release()``.
            if active is not None:
                self.events.append(InstanceLockEvent("blocked", normalized, None))
                raise BlockingIOError(f"instance lock is already held: {normalized}")
            owner_id = self._next_owner_id
            self._next_owner_id += 1
            if direct_path_call:
                handle = FakeInstanceLockHandle(self, normalized, identity, owner_id)
                active_owner: object = handle
            else:
                handle = None
                active_owner = target
            self._active[identity] = (active_owner, owner_id, normalized)
            self.events.append(InstanceLockEvent("acquired", normalized, owner_id))
            return handle

    def _release(self, handle: FakeInstanceLockHandle) -> None:
        with self._lock:
            active = self._active.get(handle._identity)
            if active is None or active[0] is not handle:
                raise RuntimeError("instance-lock handle is not active")
            del self._active[handle._identity]
            self.events.append(InstanceLockEvent("released", handle.path, handle.owner_id))

    def release(self, handle: BinaryIO) -> None:
        identity, normalized = self._target_identity_and_path(handle)
        with self._lock:
            active = self._active.get(identity)
            if active is None or active[0] is not handle:
                raise RuntimeError("instance-lock backend handle is not active")
            del self._active[identity]
            self.events.append(InstanceLockEvent("released", active[2], active[1]))

    def is_locked(self, path: Pathish) -> bool:
        identity, _ = self._target_identity_and_path(path)
        with self._lock:
            return identity in self._active

    def is_handle_locked(self, handle: BinaryIO) -> bool:
        identity, _ = self._target_identity_and_path(handle)
        with self._lock:
            return identity in self._active


# The shorter S02 specification name remains available while the explicit
# ``Backend`` suffix documents how the fake is intended to be injected.
FakeInstanceLock = FakeInstanceLockBackend


@dataclass(frozen=True, slots=True)
class ReplaceEvent:
    """One attempted atomic replace."""

    source: Path
    destination: Path
    occurrence: int


class FaultInjectingReplace:
    """Callable ``os.replace`` boundary with deterministic call-number faults."""

    def __init__(
        self,
        delegate: Callable[[Pathish, Pathish], None] | None = None,
    ) -> None:
        self._delegate = delegate or os.replace
        self.events: list[ReplaceEvent] = []
        self._failures: dict[int, BaseException] = {}
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.events)

    def fail_on(
        self,
        occurrence: int,
        error: BaseException | None = None,
    ) -> None:
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence <= 0:
            raise ValueError("occurrence must be a positive integer")
        with self._lock:
            if occurrence <= len(self.events):
                raise ValueError("cannot schedule a replace fault in the past")
            if occurrence in self._failures:
                raise ValueError("a replace fault is already scheduled for that occurrence")
            self._failures[occurrence] = error or OSError(
                f"injected replace failure #{occurrence}"
            )

    def fail_next(self, error: BaseException | None = None) -> int:
        with self._lock:
            occurrence = len(self.events) + 1
        self.fail_on(occurrence, error)
        return occurrence

    def replace(self, source: Pathish, destination: Pathish) -> None:
        with self._lock:
            occurrence = len(self.events) + 1
            self.events.append(ReplaceEvent(Path(source), Path(destination), occurrence))
            failure = self._failures.pop(occurrence, None)
        if failure is not None:
            raise failure
        self._delegate(source, destination)

    __call__ = replace


@dataclass(slots=True)
class _CheckpointGate:
    entered: threading.Event
    proceed: threading.Event
    claimed: bool = False


class BarrierStorageCoordinationLock:
    """Re-entrant coordination lock with deterministic one-shot checkpoints.

    The four automatic checkpoints are ``before_acquire``, ``after_acquire``,
    ``before_release``, and ``after_release``.  Tests or adapters may call
    :meth:`checkpoint` with additional names (for example
    ``after_reference_check``) to freeze an exact TOCTOU window.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._gates: dict[str, _CheckpointGate] = {}
        self._depth_by_thread: dict[int, int] = {}
        self._publication_depth_by_thread: dict[int, int] = {}
        self.events: list[tuple[Literal["acquire", "release"], int, int]] = []
        self.checkpoint_events: list[tuple[str, int]] = []

    @property
    def coordination_lock(self) -> BarrierStorageCoordinationLock:
        return self

    def arm_checkpoint(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("checkpoint name must be a non-empty string")
        with self._control_lock:
            if name in self._gates:
                raise ValueError(f"checkpoint is already armed: {name}")
            self._gates[name] = _CheckpointGate(threading.Event(), threading.Event())

    arm = arm_checkpoint

    def checkpoint(self, name: str) -> None:
        """Record *name* and block the first caller if that checkpoint is armed."""

        thread_id = threading.get_ident()
        gate: _CheckpointGate | None
        with self._control_lock:
            self.checkpoint_events.append((name, thread_id))
            candidate = self._gates.get(name)
            if candidate is None or candidate.claimed:
                return
            candidate.claimed = True
            candidate.entered.set()
            gate = candidate
        gate.proceed.wait()
        with self._control_lock:
            if self._gates.get(name) is gate:
                del self._gates[name]

    def wait_for_checkpoint(self, name: str, timeout_seconds: float = 1.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._control_lock:
            gate = self._gates.get(name)
            if gate is None:
                raise ValueError(f"checkpoint is not armed: {name}")
            entered = gate.entered
        return entered.wait(timeout_seconds)

    wait_until_blocked = wait_for_checkpoint

    def release_checkpoint(self, name: str) -> None:
        with self._control_lock:
            gate = self._gates.get(name)
            if gate is None:
                raise ValueError(f"checkpoint is not armed: {name}")
            gate.proceed.set()

    release_barrier = release_checkpoint

    def release_all_checkpoints(self) -> None:
        with self._control_lock:
            gates = tuple(self._gates.values())
        for gate in gates:
            gate.proceed.set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.checkpoint("before_acquire")
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking acquire")
        if timeout == -1:
            acquired = self._lock.acquire(blocking)
        else:
            acquired = self._lock.acquire(blocking, timeout)
        if not acquired:
            return False
        thread_id = threading.get_ident()
        with self._control_lock:
            depth = self._depth_by_thread.get(thread_id, 0) + 1
            self._depth_by_thread[thread_id] = depth
            self.events.append(("acquire", thread_id, depth))
        self.checkpoint("after_acquire")
        return True

    def release(self) -> None:
        self.checkpoint("before_release")
        thread_id = threading.get_ident()
        with self._control_lock:
            depth = self._depth_by_thread.get(thread_id, 0)
            publication_depth = self._publication_depth_by_thread.get(thread_id, 0)
            if depth <= 0:
                raise RuntimeError("coordination lock is not held by the current thread")
            if depth <= publication_depth:
                raise RuntimeError(
                    "a publication lease owns this coordination lock depth"
                )
            remaining = depth - 1
            if remaining:
                self._depth_by_thread[thread_id] = remaining
            else:
                del self._depth_by_thread[thread_id]
            self.events.append(("release", thread_id, remaining))
        self._lock.release()
        self.checkpoint("after_release")

    def held_by_current_thread(self) -> bool:
        with self._control_lock:
            return self._depth_by_thread.get(threading.get_ident(), 0) > 0

    def depth_for_current_thread(self) -> int:
        with self._control_lock:
            return self._depth_by_thread.get(threading.get_ident(), 0)

    def publication_held_by_current_thread(self) -> bool:
        with self._control_lock:
            return self._publication_depth_by_thread.get(threading.get_ident(), 0) > 0

    def publication_depth_for_current_thread(self) -> int:
        with self._control_lock:
            return self._publication_depth_by_thread.get(threading.get_ident(), 0)

    def _mark_publication_acquired(self) -> None:
        thread_id = threading.get_ident()
        with self._control_lock:
            depth = self._depth_by_thread.get(thread_id, 0)
            publication_depth = self._publication_depth_by_thread.get(thread_id, 0)
            if depth <= publication_depth:
                raise RuntimeError(
                    "publication acquisition requires a new coordination depth"
                )
            self._publication_depth_by_thread[thread_id] = publication_depth + 1

    def _mark_publication_released(self) -> None:
        thread_id = threading.get_ident()
        with self._control_lock:
            depth = self._publication_depth_by_thread.get(thread_id, 0)
            if depth <= 0:
                raise RuntimeError("publication lease is not held by the current thread")
            if depth == 1:
                del self._publication_depth_by_thread[thread_id]
            else:
                self._publication_depth_by_thread[thread_id] = depth - 1

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


__all__ = [
    "BarrierStorageCoordinationLock",
    "CountingBinaryStream",
    "DeterministicIdGenerator",
    "FakeFileSync",
    "FakeInstanceLock",
    "FakeInstanceLockBackend",
    "FakeInstanceLockHandle",
    "FileSyncEvent",
    "FixedClock",
    "InstanceLockEvent",
    "ReplaceEvent",
    "FaultInjectingReplace",
]
