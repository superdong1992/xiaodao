"""In-process coordination primitives for the V1 file stores.

The two guards in this module deliberately protect different spans:

* :class:`InProcessAttachmentUploadGuard` serializes the complete lifecycle of
  one attachment upload, including the potentially long input stream.
* :class:`InProcessPublicationCommitGuard` protects the short formal
  publication/adoption through state-commit critical section.

Every storage component receives the same :class:`StorageCoordinationLock`.
That identity, rather than a similarly named second lock, is what closes the
publication/cleanup race described by S02.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from types import TracebackType
from typing import Self

from pydantic import TypeAdapter

from problem_locator.contracts import OpaqueId


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)


class StorageCoordinationLock:
    """A tracked, re-entrant lock shared by all formal storage operations.

    The publication-depth observation is intentionally kept on the shared
    lock.  A ``ResourceStore`` can therefore enforce that its caller holds an
    actual publication lease without depending on the concrete guard class.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._metadata_lock = threading.Lock()
        self._depth_by_thread: defaultdict[int, int] = defaultdict(int)
        self._publication_depth_by_thread: defaultdict[int, int] = defaultdict(int)

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            thread_id = threading.get_ident()
            with self._metadata_lock:
                self._depth_by_thread[thread_id] += 1
        return acquired

    def release(self) -> None:
        thread_id = threading.get_ident()
        with self._metadata_lock:
            depth = self._depth_by_thread.get(thread_id, 0)
            publication_depth = self._publication_depth_by_thread.get(thread_id, 0)
            if depth <= 0:
                raise RuntimeError("storage coordination lock is not held by this thread")
            if depth <= publication_depth:
                raise RuntimeError(
                    "a publication lease owns this storage coordination lock depth"
                )

        # RLock performs the authoritative owner check.  Metadata is updated
        # only after the real lock release succeeds.
        self._lock.release()
        with self._metadata_lock:
            remaining = self._depth_by_thread[thread_id] - 1
            if remaining:
                self._depth_by_thread[thread_id] = remaining
            else:
                self._depth_by_thread.pop(thread_id, None)

    def held_by_current_thread(self) -> bool:
        with self._metadata_lock:
            return self._depth_by_thread.get(threading.get_ident(), 0) > 0

    def depth_for_current_thread(self) -> int:
        with self._metadata_lock:
            return self._depth_by_thread.get(threading.get_ident(), 0)

    def publication_held_by_current_thread(self) -> bool:
        with self._metadata_lock:
            return (
                self._publication_depth_by_thread.get(threading.get_ident(), 0) > 0
            )

    def publication_depth_for_current_thread(self) -> int:
        with self._metadata_lock:
            return self._publication_depth_by_thread.get(threading.get_ident(), 0)

    def _mark_publication_acquired(self) -> None:
        thread_id = threading.get_ident()
        with self._metadata_lock:
            lock_depth = self._depth_by_thread.get(thread_id, 0)
            publication_depth = self._publication_depth_by_thread.get(thread_id, 0)
            if lock_depth <= publication_depth:
                raise RuntimeError(
                    "publication acquisition requires a newly acquired coordination depth"
                )
            self._publication_depth_by_thread[thread_id] = publication_depth + 1

    def _mark_publication_released(self) -> None:
        thread_id = threading.get_ident()
        with self._metadata_lock:
            depth = self._publication_depth_by_thread.get(thread_id, 0)
            if depth <= 0:
                raise RuntimeError("publication lease is not held by this thread")
            if depth == 1:
                self._publication_depth_by_thread.pop(thread_id, None)
            else:
                self._publication_depth_by_thread[thread_id] = depth - 1

    def __enter__(self) -> Self:
        if not self.acquire():  # pragma: no cover - blocking acquisition succeeds or raises
            raise RuntimeError("failed to acquire storage coordination lock")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class PublicationCommitLease:
    """One thread-bound ownership depth on a publication guard."""

    def __init__(
        self,
        guard: InProcessPublicationCommitGuard,
        owner_thread_id: int,
    ) -> None:
        self._guard = guard
        self._owner_thread_id = owner_thread_id
        self._released = False
        self._state_lock = threading.Lock()

    def is_released(self) -> bool:
        with self._state_lock:
            return self._released

    def release(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("publication lease cannot cross threads")
        with self._state_lock:
            if self._released:
                return
            self._guard._release(self)
            self._released = True

    def __enter__(self) -> Self:
        if self.is_released():
            raise RuntimeError("publication lease is released")
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("publication lease cannot cross threads")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class InProcessPublicationCommitGuard:
    """Acquire publication leases on one shared coordination lock."""

    def __init__(self, coordination_lock: StorageCoordinationLock) -> None:
        self.coordination_lock = coordination_lock
        self._metadata_lock = threading.Lock()
        self._active_leases: dict[int, PublicationCommitLease] = {}

    def acquire(self) -> PublicationCommitLease:
        self.coordination_lock.acquire()
        publication_marked = False
        try:
            self.coordination_lock._mark_publication_acquired()
            publication_marked = True
            lease = PublicationCommitLease(self, threading.get_ident())
            with self._metadata_lock:
                self._active_leases[id(lease)] = lease
            return lease
        except BaseException:
            if publication_marked:
                self.coordination_lock._mark_publication_released()
            self.coordination_lock.release()
            raise

    def _release(self, lease: PublicationCommitLease) -> None:
        with self._metadata_lock:
            if self._active_leases.get(id(lease)) is not lease:
                raise RuntimeError("publication lease is not active")
            self.coordination_lock._mark_publication_released()
            try:
                self.coordination_lock.release()
            except BaseException:
                self.coordination_lock._mark_publication_acquired()
                raise
            self._active_leases.pop(id(lease), None)

    def held_by_current_thread(self) -> bool:
        return self.coordination_lock.publication_held_by_current_thread()


class AttachmentUploadLease:
    """Opaque capability issued by exactly one attachment registry."""

    def __init__(
        self,
        registry: AttachmentUploadRegistry,
        attachment_id: OpaqueId,
        owner_thread_id: int,
        capability: object,
    ) -> None:
        self._registry = registry
        self._attachment_id = attachment_id
        self._owner_thread_id = owner_thread_id
        self._capability = capability
        self._released = False

    @property
    def attachment_id(self) -> OpaqueId:
        return self._attachment_id

    def is_released(self) -> bool:
        return self._registry._is_released(self)

    def release(self) -> None:
        self._registry._release(self)

    def __enter__(self) -> Self:
        self._registry.validate_lease(self.attachment_id, self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class AttachmentUploadRegistry:
    """FIFO per-ID registry shared by the upload guard and resource store."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiters: defaultdict[OpaqueId, deque[object]] = defaultdict(deque)
        self._active: dict[OpaqueId, AttachmentUploadLease] = {}

    @staticmethod
    def _validate_attachment_id(attachment_id: OpaqueId) -> OpaqueId:
        return _OPAQUE_ID_ADAPTER.validate_python(attachment_id)

    def acquire(self, attachment_id: OpaqueId) -> AttachmentUploadLease:
        validated_id = self._validate_attachment_id(attachment_id)
        ticket = object()
        with self._condition:
            queue = self._waiters[validated_id]
            queue.append(ticket)
            try:
                while queue[0] is not ticket or validated_id in self._active:
                    self._condition.wait()
            except BaseException:
                queue.remove(ticket)
                if not queue:
                    self._waiters.pop(validated_id, None)
                self._condition.notify_all()
                raise

            queue.popleft()
            if not queue:
                self._waiters.pop(validated_id, None)
            lease = AttachmentUploadLease(
                self,
                validated_id,
                threading.get_ident(),
                object(),
            )
            self._active[validated_id] = lease
            return lease

    def validate_lease(
        self,
        attachment_id: OpaqueId,
        lease: object,
    ) -> None:
        validated_id = self._validate_attachment_id(attachment_id)
        with self._condition:
            if not isinstance(lease, AttachmentUploadLease):
                raise ValueError("attachment upload lease was not issued by this registry")
            if lease._registry is not self:
                raise ValueError("attachment upload lease was not issued by this registry")
            if lease.attachment_id != validated_id:
                raise ValueError("attachment upload lease ID mismatch")
            if lease._owner_thread_id != threading.get_ident():
                raise RuntimeError("attachment upload lease cannot cross threads")
            if lease._released:
                raise ValueError("attachment upload lease is released")
            if self._active.get(validated_id) is not lease:
                raise ValueError("attachment upload lease is not active")

    def require_current_thread_lease(
        self,
        attachment_id: OpaqueId,
    ) -> AttachmentUploadLease:
        validated_id = self._validate_attachment_id(attachment_id)
        with self._condition:
            lease = self._active.get(validated_id)
            if lease is None or lease._released:
                raise RuntimeError("no active attachment upload lease")
            if lease._owner_thread_id != threading.get_ident():
                raise RuntimeError("attachment upload lease is held by another thread")
            return lease

    def held_by_current_thread(self, attachment_id: OpaqueId) -> bool:
        validated_id = self._validate_attachment_id(attachment_id)
        with self._condition:
            lease = self._active.get(validated_id)
            return (
                lease is not None
                and not lease._released
                and lease._owner_thread_id == threading.get_ident()
            )

    def active_attachment_ids(self) -> tuple[OpaqueId, ...]:
        with self._condition:
            return tuple(sorted(self._active))

    def waiting_count(self, attachment_id: OpaqueId) -> int:
        validated_id = self._validate_attachment_id(attachment_id)
        with self._condition:
            return len(self._waiters.get(validated_id, ()))

    def _is_released(self, lease: AttachmentUploadLease) -> bool:
        with self._condition:
            return lease._released

    def _release(self, lease: AttachmentUploadLease) -> None:
        if threading.get_ident() != lease._owner_thread_id:
            raise RuntimeError("attachment upload lease cannot cross threads")
        with self._condition:
            if lease._released:
                return
            if lease._registry is not self or self._active.get(lease.attachment_id) is not lease:
                raise RuntimeError("attachment upload lease is not active")
            lease._released = True
            self._active.pop(lease.attachment_id, None)
            self._condition.notify_all()


class InProcessAttachmentUploadGuard:
    """Frozen ``AttachmentUploadGuard`` implementation backed by a registry."""

    def __init__(self, registry: AttachmentUploadRegistry | None = None) -> None:
        self.registry = registry or AttachmentUploadRegistry()

    def acquire(self, attachment_id: OpaqueId) -> AttachmentUploadLease:
        return self.registry.acquire(attachment_id)


__all__ = [
    "AttachmentUploadLease",
    "AttachmentUploadRegistry",
    "InProcessAttachmentUploadGuard",
    "InProcessPublicationCommitGuard",
    "PublicationCommitLease",
    "StorageCoordinationLock",
]
