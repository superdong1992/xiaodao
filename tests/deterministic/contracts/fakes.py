"""Public S00 fakes that implement only frozen contract ports.

These fakes intentionally live with the contract tests so every later slice
can test against the same synchronous ports without importing another slice's
implementation classes.  They favour observable calls and deterministic
failure injection over hidden convenience behaviour.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, ValidationError

from problem_locator.contracts.commands import (
    ApplicationCommand,
    ApplicationResponse,
    ArtifactListResponse,
    CancelReceipt,
    CaseQueryResponse,
    ClaimJob,
    ClaimReceipt,
    DispatchReceipt,
    FailureReceipt,
    GetCase,
    InterruptPreviousEpoch,
    ListArtifacts,
    OpenArtifact,
    OpenArtifactResult,
    OutcomeReceipt,
    RecoveryReceipt,
    SubmitJobOutcome,
)
from problem_locator.contracts.enums import (
    CancellationReason,
    ErrorCode,
    JobStatus,
    ResourceKind,
    ResourceType,
)
from problem_locator.contracts.errors import (
    ApplicationPortError,
    ERROR_SPECS,
    ExecutionFailure,
    PORT_ERROR_CODES,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES, MAX_CASE_RESOURCE_BYTES
from problem_locator.contracts.models import (
    Artifact,
    ApplicationError,
    ApplicationErrorDetail,
    AssetAvailabilityReport,
    AttachmentStagedRef,
    CaseAggregate,
    CaseResourceUsage,
    CommitReceipt,
    ContextSnapshot,
    DiagnosisState,
    ExecutionFileRef,
    ExecutionLogSinks,
    Job,
    MaterializedPath,
    PlannedResourceTarget,
    PublishedJobReceipt,
    ReadinessReport,
    ResolvedAsset,
    ResourceRef,
    RuntimeBindings,
    RuntimeExecutionReceipt,
    StagedResourceRef,
    StateExportObjectCounts,
    StateFile,
    StateMutation,
    TreeManifest,
    TreeManifestEntry,
    ValidationReport,
    VersionedRef,
    WaitSeconds,
    WorkspaceInputManifest,
)
from problem_locator.contracts.outcomes import (
    CaseSnapshot,
    CoordinatorPlanResult,
    JobOutcome,
    ValidatedTrigger,
    validate_coordinator_plan_result,
)
from problem_locator.contracts.ports import (
    AppendOnlyByteSink,
    ApplicationCommandPort,
    ApplicationQueryPort,
    AssetCatalogPort,
    AttachmentUploadGuard,
    AttachmentUploadLease,
    BinaryStream,
    CancellationSignal,
    Clock,
    ContextSnapshotProjector,
    Coordinator,
    Dispatcher,
    ExecutionRecordStore,
    IdGenerator,
    JobControlPort,
    LogparseBrokerFactory,
    LogparseBrokerSession,
    PublicationCommitGuard,
    PublicationCommitLease,
    ResourceStore,
    Runtime,
    StateAdminPort,
    StateChangeNotifier,
    StateRepository,
)
from problem_locator.contracts.serialization import (
    CONTRACT_REVISION,
    SCHEMA_VERSION,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


DEFAULT_FAKE_TIME = "2026-01-02T03:04:05.000Z"
_READ_CHUNK_BYTES = 64 * 1024
_MISSING = object()


def _clone(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_copy(deep=True)
    return copy.deepcopy(value)


def _raise_or_return(item: Any, *args: Any, **kwargs: Any) -> Any:
    if isinstance(item, BaseException):
        raise item
    if callable(item):
        return item(*args, **kwargs)
    return _clone(item)


def _port_error(
    code: ErrorCode,
    message: str,
    *,
    details: Sequence[ApplicationErrorDetail] = (),
) -> ApplicationPortError:
    """Build the one frozen modeled-failure channel used by public fakes."""

    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=list(details),
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _validate_raw_port_input(
    method_key: str,
    model_type: type[BaseModel],
    payload: Mapping[str, Any],
) -> BaseModel:
    """Rebuild a raw Port call before recording or consuming its script."""

    try:
        return model_type.model_validate(payload, strict=True)
    except (TypeError, ValueError, ValidationError):
        raise _port_error(
            ErrorCode.VALIDATION_ERROR,
            f"{method_key} received invalid raw input.",
        ) from None


def _rebuild_contract_input(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", warnings=False)
    return value


def _validate_scripted_port_error(
    method_key: str,
    error: ApplicationPortError,
) -> None:
    if error.error.code not in PORT_ERROR_CODES[method_key]:
        raise ValueError(
            f"{method_key} does not allow {error.error.code.value}"
        ) from error


def _take_port_script(
    method_key: str,
    script: _Script,
    *args: Any,
) -> Any:
    try:
        return script.take(method_key, *args)
    except ApplicationPortError as error:
        _validate_scripted_port_error(method_key, error)
        raise


class _Script:
    def __init__(self, values: Iterable[Any] = ()) -> None:
        self._values: deque[Any] = deque(values)

    def append(self, value: Any) -> None:
        self._values.append(value)

    def take(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if not self._values:
            raise AssertionError(f"no scripted result remains for {name}")
        return _raise_or_return(self._values.popleft(), *args, **kwargs)

    def __len__(self) -> int:
        return len(self._values)


class FakeClock:
    """Fixed/scriptable UTC clock."""

    def __init__(
        self,
        value: str = DEFAULT_FAKE_TIME,
        *,
        scripted_values: Iterable[str] = (),
    ) -> None:
        self._value = value
        self._scripted_values: deque[str] = deque(scripted_values)
        self.calls = 0
        self._lock = threading.Lock()

    def now(self) -> str:
        with self._lock:
            self.calls += 1
            if self._scripted_values:
                self._value = self._scripted_values.popleft()
            return self._value

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value

    def queue(self, *values: str) -> None:
        with self._lock:
            self._scripted_values.extend(values)


class DeterministicIdGenerator:
    """Deterministic UUID generator including the frozen UUIDv5 derivation."""

    def __init__(
        self,
        *,
        seed: str = "problem-locator-contract-fake",
        scripted_ids: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.seed = seed
        self._scripted_ids = {
            kind: deque(values) for kind, values in (scripted_ids or {}).items()
        }
        self._counters: defaultdict[str, int] = defaultdict(int)
        self.new_calls: list[str] = []
        self.derive_calls: list[tuple[str, tuple[str, ...]]] = []
        self._lock = threading.Lock()

    def new(self, kind: str) -> str:
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        with self._lock:
            self.new_calls.append(kind)
            scripted = self._scripted_ids.get(kind)
            if scripted:
                return scripted.popleft()
            self._counters[kind] += 1
            name = canonical_json_bytes(
                {
                    "kind": kind,
                    "parts": [self.seed, str(self._counters[kind])],
                }
            )[:-1].decode("utf-8")
            return str(uuid.uuid5(uuid.NAMESPACE_URL, name))

    def derive(self, kind: str, stable_parts: Sequence[str]) -> str:
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if isinstance(stable_parts, (str, bytes)):
            raise TypeError("stable_parts must be a sequence of strings")
        parts = tuple(stable_parts)
        if not parts or any(not isinstance(part, str) or not part for part in parts):
            raise ValueError("stable_parts must contain only non-empty strings")
        with self._lock:
            self.derive_calls.append((kind, parts))
        name = canonical_json_bytes({"kind": kind, "parts": list(parts)})[
            :-1
        ].decode("utf-8")
        return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


class PureContextSnapshotProjector:
    """Mechanical projection of the complete public DiagnosisState view."""

    def project(self, target_diagnosis_state: DiagnosisState) -> ContextSnapshot:
        state = target_diagnosis_state
        return ContextSnapshot.model_validate(
            {
                "diagnosis_state_revision": state.revision,
                "problem_spec": state.problem_spec,
                "user_facts": state.user_facts,
                "confirmed_facts": state.confirmed_facts,
                "active_hypotheses": state.active_hypotheses,
                "rejected_hypotheses": state.rejected_hypotheses,
                "open_questions": state.open_questions,
                "pending_requirements": state.pending_requirements,
                "evidence_refs": state.evidence_refs,
                "candidate_conclusion": state.candidate_conclusion,
            }
        )


class InMemoryBinaryStream:
    """Forward-only stream with explicit read observations."""

    def __init__(
        self,
        data: bytes = b"",
        *,
        fail_on_read_number: int | None = None,
        failure: BaseException | None = None,
    ) -> None:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self._data = data
        self._offset = 0
        self._closed = False
        self.fail_on_read_number = fail_on_read_number
        self.failure = failure or OSError("injected stream read failure")
        self.read_requests: list[int] = []
        self.returned_sizes: list[int] = []
        self.close_calls = 0
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def bytes_read(self) -> int:
        with self._lock:
            return self._offset

    def read(self, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with self._lock:
            if self._closed:
                raise ValueError("stream is closed")
            self.read_requests.append(max_bytes)
            if (
                self.fail_on_read_number is not None
                and len(self.read_requests) == self.fail_on_read_number
            ):
                raise self.failure
            chunk = self._data[self._offset : self._offset + max_bytes]
            self._offset += len(chunk)
            self.returned_sizes.append(len(chunk))
            return chunk

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            self._closed = True

    def __enter__(self) -> Self:
        if self.closed:
            raise ValueError("stream is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class InMemoryCancellationSignal:
    """Thread-safe first-reason-wins cancellation signal."""

    def __init__(self, reason: CancellationReason | None = None) -> None:
        self._reason = reason
        self._event = threading.Event()
        self._lock = threading.Lock()
        if reason is not None:
            self._event.set()

    @property
    def reason(self) -> CancellationReason | None:
        with self._lock:
            return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        return self._event.wait(timeout_seconds)

    def cancel(self, reason: CancellationReason) -> bool:
        if not isinstance(reason, CancellationReason):
            raise TypeError("reason must be CancellationReason")
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            self._event.set()
            return True


class _AttachmentLease:
    def __init__(
        self,
        guard: InMemoryAttachmentUploadGuard,
        attachment_id: str,
        owner_thread_id: int,
        capability: object,
    ) -> None:
        self._guard = guard
        self._attachment_id = attachment_id
        self._owner_thread_id = owner_thread_id
        self._capability = capability
        self._released = False
        self._lock = threading.Lock()

    @property
    def attachment_id(self) -> str:
        return self._attachment_id

    def is_released(self) -> bool:
        with self._lock:
            return self._released

    def release(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("attachment upload lease cannot cross threads")
        with self._lock:
            if self._released:
                return
            self._guard._release(self)
            self._released = True

    def __enter__(self) -> Self:
        if self.is_released():
            raise RuntimeError("attachment upload lease is released")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class InMemoryAttachmentUploadGuard:
    """FIFO per-ID guard; different attachment IDs never share a lock."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiters: defaultdict[str, deque[object]] = defaultdict(deque)
        self._active: dict[str, _AttachmentLease] = {}
        self.acquire_calls: list[str] = []
        self.release_calls: list[str] = []

    def acquire(self, attachment_id: str) -> AttachmentUploadLease:
        if not isinstance(attachment_id, str) or not attachment_id:
            raise ValueError("attachment_id must be non-empty")
        ticket = object()
        capability = object()
        with self._condition:
            self.acquire_calls.append(attachment_id)
            queue = self._waiters[attachment_id]
            queue.append(ticket)
            while queue[0] is not ticket or attachment_id in self._active:
                self._condition.wait()
            queue.popleft()
            if not queue:
                self._waiters.pop(attachment_id, None)
            lease = _AttachmentLease(
                self,
                attachment_id,
                threading.get_ident(),
                capability,
            )
            self._active[attachment_id] = lease
            return lease

    def _release(self, lease: _AttachmentLease) -> None:
        with self._condition:
            if self._active.get(lease.attachment_id) is not lease:
                raise RuntimeError("attachment upload lease is not active")
            del self._active[lease.attachment_id]
            self.release_calls.append(lease.attachment_id)
            self._condition.notify_all()

    def _validate(self, attachment_id: str, lease: AttachmentUploadLease) -> None:
        if not isinstance(lease, _AttachmentLease) or lease._guard is not self:
            raise ValueError("upload lease was not issued by this guard")
        if lease.attachment_id != attachment_id:
            raise ValueError("upload lease attachment ID mismatch")
        if lease.is_released():
            raise ValueError("upload lease is released")
        with self._condition:
            if self._active.get(attachment_id) is not lease:
                raise ValueError("upload lease is not active")


class _PublicationLease:
    def __init__(
        self,
        guard: InMemoryPublicationCommitGuard,
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
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class InMemoryPublicationCommitGuard:
    """Re-entrant short publication/commit critical section."""

    def __init__(self, lock: threading.RLock | None = None) -> None:
        self._lock = lock or threading.RLock()
        self.acquire_calls = 0
        self.release_calls = 0
        self._depth_by_thread: defaultdict[int, int] = defaultdict(int)
        self.events: list[tuple[str, int, int]] = []

    @property
    def coordination_lock(self) -> threading.RLock:
        return self._lock

    def acquire(self) -> PublicationCommitLease:
        self._lock.acquire()
        thread_id = threading.get_ident()
        self.acquire_calls += 1
        self._depth_by_thread[thread_id] += 1
        self.events.append(("acquire", thread_id, self._depth_by_thread[thread_id]))
        return _PublicationLease(self, thread_id)

    def _release(self, lease: _PublicationLease) -> None:
        thread_id = threading.get_ident()
        if self._depth_by_thread[thread_id] <= 0:
            raise RuntimeError("publication lease is not active")
        self._depth_by_thread[thread_id] -= 1
        if self._depth_by_thread[thread_id] == 0:
            del self._depth_by_thread[thread_id]
        self.release_calls += 1
        self.events.append(("release", thread_id, self._depth_by_thread.get(thread_id, 0)))
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return self._depth_by_thread.get(threading.get_ident(), 0) > 0

    def depth_for_current_thread(self) -> int:
        return self._depth_by_thread.get(threading.get_ident(), 0)


@dataclass(slots=True)
class _StoredResource:
    resource_kind: ResourceKind
    size: int
    sha256: str
    payload: bytes | None = None
    tree: dict[str, bytes] | None = None
    tree_manifest: TreeManifest | None = None


class InMemoryResourceStore:
    """Hash-validating in-memory implementation of the frozen ResourceStore."""

    def __init__(
        self,
        *,
        upload_guard: InMemoryAttachmentUploadGuard | None = None,
        publication_guard: InMemoryPublicationCommitGuard | None = None,
    ) -> None:
        self.upload_guard = upload_guard
        self.publication_guard = publication_guard
        self._staged: dict[tuple[str, str], _StoredResource] = {}
        self._staged_refs: dict[
            tuple[str, str], StagedResourceRef | AttachmentStagedRef
        ] = {}
        self._staged_completion_markers: set[tuple[str, str]] = set()
        self._published_stage_history: dict[
            tuple[str, str], tuple[StagedResourceRef | AttachmentStagedRef, str]
        ] = {}
        self._published: dict[str, _StoredResource] = {}
        self._quarantined: dict[str, tuple[str, _StoredResource]] = {}
        self._state_reference_counts: defaultdict[str, int] = defaultdict(int)
        self._outbox_reference_counts: defaultdict[str, int] = defaultdict(int)
        self._ordinary_orphans: set[str] = set()
        self.stage_file_calls: list[tuple[str, str]] = []
        self.stage_tree_calls: list[tuple[str, str, Path]] = []
        self.stage_attachment_calls: list[str] = []
        self.validate_staged_calls: list[StagedResourceRef] = []
        self.plan_target_calls: list[
            tuple[str, ResourceType, str, ResourceKind, int, str]
        ] = []
        self.publish_calls: list[tuple[Any, str]] = []
        self.capacity_calls: list[tuple[str, tuple[PlannedResourceTarget, ...]]] = []
        self.discard_calls: list[Any] = []
        self.seed_calls: list[tuple[str, int, int, bool]] = []
        self.cleanup_calls: list[tuple[str, str]] = []
        self.quarantine_events: list[tuple[str, str]] = []
        self._failures: defaultdict[str, deque[BaseException]] = defaultdict(deque)
        self._lock = (
            publication_guard.coordination_lock
            if publication_guard is not None
            else threading.RLock()
        )

    @property
    def published_storage_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._published))

    @property
    def quarantined_storage_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._quarantined))

    @property
    def staged_resource_count(self) -> int:
        with self._lock:
            return len(self._staged)

    @property
    def formal_resource_categories(self) -> dict[str, tuple[str, ...]]:
        """Return observable accounting classes for each unique formal key."""

        with self._lock:
            return {
                storage_key: tuple(
                    category
                    for category, present in (
                        ("STATE", self._state_reference_counts[storage_key] > 0),
                        ("OUTBOX", self._outbox_reference_counts[storage_key] > 0),
                        ("ORPHAN", storage_key in self._ordinary_orphans),
                    )
                    if present
                )
                for storage_key in sorted(self._published)
            }

    def _require_publication_lease(self) -> None:
        if (
            self.publication_guard is not None
            and not self.publication_guard.held_by_current_thread()
        ):
            raise RuntimeError("a PublicationCommitLease is required")

    @staticmethod
    def _target_fields(
        target: PlannedResourceTarget | ResourceRef,
    ) -> tuple[str, ResourceKind, int, str]:
        if isinstance(target, PlannedResourceTarget):
            return (
                target.final_storage_key,
                target.resource_kind,
                target.size,
                target.sha256,
            )
        if isinstance(target, ResourceRef):
            return (
                target.storage_key,
                target.resource_kind,
                target.size,
                target.sha256,
            )
        raise TypeError("target must be PlannedResourceTarget or ResourceRef")

    def seed_formal_resource(
        self,
        target: PlannedResourceTarget | ResourceRef,
        *,
        state_reference_count: int = 0,
        outbox_reference_count: int = 0,
        ordinary_orphan: bool | None = None,
        payload: bytes | None = None,
    ) -> ResourceRef:
        """Seed metadata without allocating multi-GiB test payloads.

        This is a Fake setup injection point, not a production Port method.
        Reference multiplicity is observable but capacity always counts the
        unique formal storage key exactly once.
        """

        if state_reference_count < 0 or outbox_reference_count < 0:
            raise ValueError("reference counts must be non-negative")
        storage_key, resource_kind, size, sha256 = self._target_fields(target)
        self._validate_storage_key(storage_key)
        if payload is not None:
            if resource_kind is not ResourceKind.FILE:
                raise ValueError("seed payload is supported only for FILE resources")
            if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha256:
                raise ValueError("seed payload does not match target metadata")
        if ordinary_orphan is None:
            ordinary_orphan = state_reference_count == 0 and outbox_reference_count == 0
        if ordinary_orphan and (state_reference_count or outbox_reference_count):
            raise ValueError("a referenced formal resource is not an ordinary orphan")
        with self._lock:
            if any(original_key == storage_key for original_key, _ in self._quarantined.values()):
                raise ValueError("quarantined resources are not adoptable formal targets")
            existing = self._published.get(storage_key)
            candidate = _StoredResource(
                resource_kind=resource_kind,
                size=size,
                sha256=sha256,
                payload=payload,
            )
            if existing is not None and not self._matches_ref(existing, target):
                raise ValueError("seeded formal resource content conflicts")
            if existing is None:
                self._published[storage_key] = candidate
            self._state_reference_counts[storage_key] += state_reference_count
            self._outbox_reference_counts[storage_key] += outbox_reference_count
            if ordinary_orphan:
                self._ordinary_orphans.add(storage_key)
            elif state_reference_count or outbox_reference_count:
                self._ordinary_orphans.discard(storage_key)
            self.seed_calls.append(
                (
                    storage_key,
                    state_reference_count,
                    outbox_reference_count,
                    ordinary_orphan,
                )
            )
            return ResourceRef(
                resource_kind=resource_kind,
                storage_key=storage_key,
                size=size,
                sha256=sha256,
            )

    def add_state_references(self, storage_key: str, count: int = 1) -> None:
        self._add_references(storage_key, self._state_reference_counts, count)

    def add_outbox_protections(self, storage_key: str, count: int = 1) -> None:
        self._add_references(storage_key, self._outbox_reference_counts, count)

    def _add_references(
        self,
        storage_key: str,
        reference_counts: defaultdict[str, int],
        count: int,
    ) -> None:
        if count <= 0:
            raise ValueError("count must be positive")
        with self._lock:
            if storage_key not in self._published:
                raise LookupError("formal resource not found")
            reference_counts[storage_key] += count
            self._ordinary_orphans.discard(storage_key)

    def release_state_references(self, storage_key: str, count: int = 1) -> None:
        self._release_references(storage_key, self._state_reference_counts, count)

    def release_outbox_protections(self, storage_key: str, count: int = 1) -> None:
        self._release_references(storage_key, self._outbox_reference_counts, count)

    def _release_references(
        self,
        storage_key: str,
        reference_counts: defaultdict[str, int],
        count: int,
    ) -> None:
        if count <= 0:
            raise ValueError("count must be positive")
        with self._lock:
            if reference_counts[storage_key] < count:
                raise ValueError("reference count underflow")
            reference_counts[storage_key] -= count
            if reference_counts[storage_key] == 0:
                reference_counts.pop(storage_key, None)
            if (
                storage_key in self._published
                and self._state_reference_counts[storage_key] == 0
                and self._outbox_reference_counts[storage_key] == 0
            ):
                self._ordinary_orphans.add(storage_key)

    def quarantine_ordinary_orphan(self, storage_key: str, cleanup_id: str) -> bool:
        """Recheck and atomically isolate one unreferenced formal object.

        The method acquires the exact coordination lock shared with the
        publication guard.  Once moved, the original key is no longer
        adoptable and no longer contributes to Case capacity.
        """

        if not cleanup_id:
            raise ValueError("cleanup_id must be non-empty")
        self._maybe_fail("quarantine")
        with self._lock:
            self.cleanup_calls.append((storage_key, cleanup_id))
            value = self._published.get(storage_key)
            if value is None:
                return False
            if (
                self._state_reference_counts[storage_key] > 0
                or self._outbox_reference_counts[storage_key] > 0
                or storage_key not in self._ordinary_orphans
            ):
                return False
            quarantine_key = f"tmp/quarantine/{cleanup_id}/{storage_key}"
            self._published.pop(storage_key)
            self._ordinary_orphans.discard(storage_key)
            self._state_reference_counts.pop(storage_key, None)
            self._outbox_reference_counts.pop(storage_key, None)
            self._quarantined[quarantine_key] = (storage_key, value)
            self.quarantine_events.append((storage_key, quarantine_key))
            return True

    def inject_failure(self, operation: str, failure: BaseException) -> None:
        self._failures[operation].append(failure)

    def _maybe_fail(self, operation: str) -> None:
        failures = self._failures[operation]
        if failures:
            failure = failures.popleft()
            if isinstance(failure, ApplicationPortError):
                _validate_scripted_port_error(
                    f"ResourceStore.{operation}",
                    failure,
                )
            raise failure

    @staticmethod
    def _read_stream(
        stream: BinaryStream,
        *,
        byte_limit: int | None = None,
    ) -> tuple[bytes, int, str]:
        payload = bytearray()
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not isinstance(chunk, bytes):
                raise TypeError("BinaryStream.read must return bytes")
            if len(chunk) > _READ_CHUNK_BYTES:
                raise ValueError("BinaryStream returned more than max_bytes")
            if not chunk:
                break
            size += len(chunk)
            if byte_limit is not None and size > byte_limit:
                raise _port_error(
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "The resource exceeds its byte limit.",
                )
            digest.update(chunk)
            payload.extend(chunk)
        return bytes(payload), size, digest.hexdigest()

    @staticmethod
    def _check_expected(
        size: int,
        sha256: str,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> None:
        if expected_size is not None and size != expected_size:
            raise _port_error(
                ErrorCode.RESOURCE_SIZE_MISMATCH,
                "The resource size does not match the expected size.",
            )
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The resource digest does not match the expected SHA-256.",
            )

    @staticmethod
    def _staging_id(owner_job_id: str, proposal_key: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"problem-locator-stage:{owner_job_id}:{proposal_key}",
            )
        )

    def stage_file(
        self,
        owner_job_id: str,
        proposal_key: str,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedResourceRef:
        self._maybe_fail("stage_file")
        payload, size, sha256 = self._read_stream(
            stream,
            byte_limit=MAX_CASE_RESOURCE_BYTES,
        )
        self._check_expected(size, sha256, expected_size, expected_sha256)
        staging_id = self._staging_id(owner_job_id, proposal_key)
        value = _StoredResource(ResourceKind.FILE, size, sha256, payload=payload)
        staged_ref = StagedResourceRef(
            staging_id=staging_id,
            owner_job_id=owner_job_id,
            proposal_key=proposal_key,
            resource_kind=ResourceKind.FILE,
            size=size,
            sha256=sha256,
            tree_manifest=None,
        )
        key = ("proposal", staging_id)
        with self._lock:
            self.stage_file_calls.append((owner_job_id, proposal_key))
            self._staged[key] = value
            self._staged_refs[key] = _clone(staged_ref)
            self._staged_completion_markers.add(key)
            self._published_stage_history.pop(key, None)
        return staged_ref

    def stage_generated_file(
        self,
        owner_job_id: str,
        proposal_key: str,
        staging_id: str,
        stream: BinaryStream,
        expected_size: int,
        expected_sha256: str,
    ) -> StagedResourceRef:
        self._maybe_fail("stage_generated_file")
        key = ("proposal", staging_id)
        with self._lock:
            existing_ref = self._staged_refs.get(key)
            existing = self._staged.get(key)
            if existing_ref is not None or existing is not None:
                if (
                    not isinstance(existing_ref, StagedResourceRef)
                    or existing is None
                    or existing_ref.owner_job_id != owner_job_id
                    or existing_ref.proposal_key != proposal_key
                    or existing_ref.resource_kind is not ResourceKind.FILE
                    or existing_ref.size != expected_size
                    or existing_ref.sha256 != expected_sha256
                    or existing.size != expected_size
                    or existing.sha256 != expected_sha256
                ):
                    raise _port_error(
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                        "The generated staged resource conflicts with its retry.",
                    )
                return _clone(existing_ref)
        payload, size, sha256 = self._read_stream(
            stream,
            byte_limit=MAX_CASE_RESOURCE_BYTES,
        )
        self._check_expected(size, sha256, expected_size, expected_sha256)
        staged_ref = StagedResourceRef(
            staging_id=staging_id,
            owner_job_id=owner_job_id,
            proposal_key=proposal_key,
            resource_kind=ResourceKind.FILE,
            size=size,
            sha256=sha256,
            tree_manifest=None,
        )
        with self._lock:
            self.stage_file_calls.append((owner_job_id, proposal_key))
            self._staged[key] = _StoredResource(
                ResourceKind.FILE,
                size,
                sha256,
                payload=payload,
            )
            self._staged_refs[key] = _clone(staged_ref)
            self._staged_completion_markers.add(key)
            self._published_stage_history.pop(key, None)
        return staged_ref

    def stage_tree(
        self,
        owner_job_id: str,
        proposal_key: str,
        root: Path,
        expected_manifest_hash: str | None = None,
    ) -> StagedResourceRef:
        self._maybe_fail("stage_tree")
        root = Path(root)
        if not root.is_dir() or root.is_symlink():
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The staged tree root must be a real directory.",
            )
        files: dict[str, bytes] = {}
        entries: list[TreeManifestEntry] = []
        for candidate in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            if candidate.is_symlink():
                raise _port_error(
                    ErrorCode.PATH_VIOLATION,
                    "Links are forbidden in staged trees.",
                )
            if candidate.is_dir():
                continue
            file_stat = candidate.stat(follow_symlinks=False)
            mode = file_stat.st_mode
            if not stat.S_ISREG(mode):
                raise _port_error(
                    ErrorCode.PATH_VIOLATION,
                    "Staged tree entries must be ordinary files.",
                )
            if file_stat.st_nlink != 1:
                raise _port_error(
                    ErrorCode.PATH_VIOLATION,
                    "Hard links are forbidden in staged trees.",
                )
            relative = candidate.relative_to(root).as_posix()
            data = candidate.read_bytes()
            files[relative] = data
            entries.append(
                TreeManifestEntry(
                    path=relative,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
        manifest = TreeManifest(version=1, entries=entries)
        tree_sha256 = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        if expected_manifest_hash is not None and tree_sha256 != expected_manifest_hash:
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The tree manifest digest does not match the expected SHA-256.",
            )
        size = sum(entry.size for entry in entries)
        if size > MAX_CASE_RESOURCE_BYTES:
            raise _port_error(
                ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "The staged tree exceeds the Case resource byte limit.",
            )
        staging_id = self._staging_id(owner_job_id, proposal_key)
        value = _StoredResource(
            ResourceKind.DIRECTORY,
            size,
            tree_sha256,
            tree=files,
            tree_manifest=manifest,
        )
        staged_ref = StagedResourceRef(
            staging_id=staging_id,
            owner_job_id=owner_job_id,
            proposal_key=proposal_key,
            resource_kind=ResourceKind.DIRECTORY,
            size=size,
            sha256=tree_sha256,
            tree_manifest=manifest,
        )
        key = ("proposal", staging_id)
        with self._lock:
            self.stage_tree_calls.append((owner_job_id, proposal_key, root))
            self._staged[key] = value
            self._staged_refs[key] = _clone(staged_ref)
            self._staged_completion_markers.add(key)
            self._published_stage_history.pop(key, None)
        return staged_ref

    def stage_attachment(
        self,
        attachment_id: str,
        upload_lease: AttachmentUploadLease,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> AttachmentStagedRef:
        self._maybe_fail("stage_attachment")
        if self.upload_guard is not None:
            try:
                self.upload_guard._validate(attachment_id, upload_lease)
            except ValueError:
                raise _port_error(
                    ErrorCode.UPLOAD_INCOMPLETE,
                    "The attachment upload lease is no longer valid.",
                ) from None
        else:
            if upload_lease.attachment_id != attachment_id or upload_lease.is_released():
                raise _port_error(
                    ErrorCode.UPLOAD_INCOMPLETE,
                    "The attachment upload lease is no longer valid.",
                )
        payload, size, sha256 = self._read_stream(
            stream,
            byte_limit=MAX_ATTACHMENT_BYTES,
        )
        self._check_expected(size, sha256, expected_size, expected_sha256)
        staged_ref = AttachmentStagedRef(
            attachment_id=attachment_id,
            resource_kind=ResourceKind.FILE,
            size=size,
            sha256=sha256,
        )
        key = ("attachment", attachment_id)
        with self._lock:
            self.stage_attachment_calls.append(attachment_id)
            self._staged[key] = _StoredResource(
                ResourceKind.FILE,
                size,
                sha256,
                payload=payload,
            )
            self._staged_refs[key] = _clone(staged_ref)
            self._staged_completion_markers.add(key)
            self._published_stage_history.pop(key, None)
        return staged_ref

    @staticmethod
    def _validate_storage_key(storage_key: str) -> None:
        if not storage_key or storage_key.startswith("/") or "\\" in storage_key:
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The formal storage key is outside the allowed resource root.",
            )
        parts = storage_key.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The formal storage key contains an unsafe segment.",
            )

    @staticmethod
    def _staged_key(
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> tuple[str, str]:
        if isinstance(staged_ref, StagedResourceRef):
            return ("proposal", staged_ref.staging_id)
        if isinstance(staged_ref, AttachmentStagedRef):
            return ("attachment", staged_ref.attachment_id)
        raise TypeError("unsupported staged reference")

    @staticmethod
    def _matches_ref(value: _StoredResource, ref: Any) -> bool:
        return (
            value.resource_kind == ref.resource_kind
            and value.size == ref.size
            and value.sha256 == ref.sha256
        )

    @staticmethod
    def _content_matches_metadata(value: _StoredResource) -> bool:
        try:
            if value.resource_kind is ResourceKind.FILE:
                return (
                    value.payload is not None
                    and len(value.payload) == value.size
                    and hashlib.sha256(value.payload).hexdigest() == value.sha256
                )
            if value.tree is None or value.tree_manifest is None:
                return False
            entries = [
                TreeManifestEntry(
                    path=relative,
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
                for relative, payload in sorted(value.tree.items())
            ]
            manifest = TreeManifest(version=1, entries=entries)
            manifest_hash = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
            return (
                manifest == value.tree_manifest
                and sum(entry.size for entry in entries) == value.size
                and manifest_hash == value.sha256
            )
        except (TypeError, ValueError):
            return False

    def validate_staged(self, staged_ref: StagedResourceRef) -> None:
        self._maybe_fail("validate_staged")
        if not isinstance(staged_ref, StagedResourceRef):
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The staged resource receipt has an invalid shape.",
            )
        key = self._staged_key(staged_ref)
        with self._lock:
            self.validate_staged_calls.append(_clone(staged_ref))
            value = self._staged.get(key)
            if value is not None:
                stored_ref = self._staged_refs.get(key)
                if key not in self._staged_completion_markers:
                    raise _port_error(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "The staged resource completion marker is missing.",
                    )
                if (
                    stored_ref != staged_ref
                    or not self._matches_ref(value, staged_ref)
                    or not self._content_matches_metadata(value)
                ):
                    raise _port_error(
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                        "The staged resource receipt or bytes have drifted.",
                    )
                return None

            history = self._published_stage_history.get(key)
            if history is None:
                raise _port_error(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "The staged resource does not exist.",
                )
            stored_ref, final_storage_key = history
            published = self._published.get(final_storage_key)
            if (
                stored_ref != staged_ref
                or published is None
                or not self._matches_ref(published, staged_ref)
                or not self._content_matches_metadata(published)
            ):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The previously published staged resource has drifted.",
                )
            return None

    def plan_target(
        self,
        case_id: str,
        resource_type: ResourceType,
        resource_id: str,
        resource_kind: ResourceKind,
        size: int,
        sha256: str,
    ) -> PlannedResourceTarget:
        self._maybe_fail("plan_target")
        if (
            not isinstance(resource_type, ResourceType)
            or not isinstance(resource_kind, ResourceKind)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or (resource_type is ResourceType.ATTACHMENT and resource_kind is not ResourceKind.FILE)
        ):
            raise _port_error(
                ErrorCode.VALIDATION_ERROR,
                "The requested resource target has an invalid shape.",
            )
        size_limit = (
            MAX_ATTACHMENT_BYTES
            if resource_type is ResourceType.ATTACHMENT
            else MAX_CASE_RESOURCE_BYTES
        )
        if size > size_limit:
            raise _port_error(
                ErrorCode.VALIDATION_ERROR,
                "The requested resource target exceeds its byte limit.",
            )
        collection = {
            ResourceType.ATTACHMENT: "attachments",
            ResourceType.EVIDENCE: "evidence",
            ResourceType.ARTIFACT: "artifacts",
        }[resource_type]
        suffix = "payload" if resource_kind is ResourceKind.FILE else "tree"
        final_storage_key = (
            f"resources/cases/{case_id}/{collection}/{resource_id}/{suffix}"
        )
        try:
            target = PlannedResourceTarget(
                case_id=case_id,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_kind=resource_kind,
                size=size,
                sha256=sha256,
                final_storage_key=final_storage_key,
            )
        except (TypeError, ValidationError, ValueError):
            raise _port_error(
                ErrorCode.VALIDATION_ERROR,
                "The requested resource target identifiers or digest are invalid.",
            ) from None
        with self._lock:
            self.plan_target_calls.append(
                (case_id, resource_type, resource_id, resource_kind, size, sha256)
            )
        return target

    def publish(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
        final_storage_key: str,
    ) -> ResourceRef:
        self._maybe_fail("publish")
        self._require_publication_lease()
        self._validate_storage_key(final_storage_key)
        key = self._staged_key(staged_ref)
        with self._lock:
            self.publish_calls.append((_clone(staged_ref), final_storage_key))
            existing = self._published.get(final_storage_key)
            if existing is not None:
                if (
                    not self._matches_ref(existing, staged_ref)
                    or not self._content_matches_metadata(existing)
                ):
                    raise _port_error(
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                        "The formal resource conflicts with the staged receipt.",
                    )
                staged_value = self._staged.get(key)
                staged_receipt = self._staged_refs.get(key)
                if staged_value is not None and (
                    key not in self._staged_completion_markers
                    or staged_receipt != staged_ref
                    or not self._matches_ref(staged_value, staged_ref)
                    or not self._content_matches_metadata(staged_value)
                ):
                    raise _port_error(
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                        "The staged resource receipt or bytes have drifted.",
                    )
                previous = self._published_stage_history.get(key)
                if (
                    staged_value is None
                    and previous is not None
                    and previous[1] != final_storage_key
                ):
                    raise _port_error(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "The staged resource was already moved to another target.",
                    )
                self._published_stage_history[key] = (
                    _clone(staged_ref),
                    final_storage_key,
                )
                self._staged.pop(key, None)
                self._staged_refs.pop(key, None)
                self._staged_completion_markers.discard(key)
                return ResourceRef(
                    resource_kind=existing.resource_kind,
                    storage_key=final_storage_key,
                    size=existing.size,
                    sha256=existing.sha256,
                )
            value = self._staged.get(key)
            if value is None:
                raise _port_error(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "The staged resource does not exist.",
                )
            if (
                key not in self._staged_completion_markers
                or self._staged_refs.get(key) != staged_ref
                or not self._matches_ref(value, staged_ref)
                or not self._content_matches_metadata(value)
            ):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The staged resource receipt or bytes have drifted.",
                )
            self._published[final_storage_key] = value
            self._ordinary_orphans.add(final_storage_key)
            del self._staged[key]
            self._staged_refs.pop(key, None)
            self._staged_completion_markers.discard(key)
            self._published_stage_history[key] = (
                _clone(staged_ref),
                final_storage_key,
            )
            return ResourceRef(
                resource_kind=value.resource_kind,
                storage_key=final_storage_key,
                size=value.size,
                sha256=value.sha256,
            )

    def validate_case_capacity(
        self,
        case_id: str,
        planned_final_targets: Sequence[PlannedResourceTarget],
    ) -> CaseResourceUsage:
        self._maybe_fail("validate_case_capacity")
        self._require_publication_lease()
        targets = tuple(planned_final_targets)
        keys = [target.final_storage_key for target in targets]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "Planned targets must be uniquely sorted by storage key.",
            )
        prefix = f"resources/cases/{case_id}/"
        with self._lock:
            self.capacity_calls.append((case_id, _clone(targets)))
            current_bytes = sum(
                value.size
                for storage_key, value in self._published.items()
                if storage_key.startswith(prefix)
            )
            new_bytes = 0
            for target in targets:
                if not target.final_storage_key.startswith(prefix):
                    raise _port_error(
                        ErrorCode.PATH_VIOLATION,
                        "A planned target is outside the Case resource root.",
                    )
                existing = self._published.get(target.final_storage_key)
                if existing is None:
                    new_bytes += target.size
                elif not self._matches_ref(existing, target):
                    raise _port_error(
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                        "A planned target conflicts with a published resource.",
                    )
            total_bytes = current_bytes + new_bytes
            usage = CaseResourceUsage(
                current_bytes=current_bytes,
                new_bytes=new_bytes,
                total_bytes=total_bytes,
                limit_bytes=MAX_CASE_RESOURCE_BYTES,
            )
            if total_bytes > MAX_CASE_RESOURCE_BYTES:
                raise _port_error(
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "The Case resource capacity is exceeded.",
                    details=(
                        ApplicationErrorDetail(
                            field="case_resource_bytes",
                            resource_type="CASE",
                            resource_id=case_id,
                            resource_ref=None,
                            expected=None,
                            actual=None,
                            limit=MAX_CASE_RESOURCE_BYTES,
                            observed=total_bytes,
                        ),
                    ),
                )
            return usage

    def open_read(self, resource_ref: ResourceRef) -> BinaryStream:
        self._maybe_fail("open_read")
        with self._lock:
            value = self._published.get(resource_ref.storage_key)
            if value is None:
                raise _port_error(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "The published resource does not exist.",
                )
            if value.size != resource_ref.size:
                raise _port_error(
                    ErrorCode.RESOURCE_SIZE_MISMATCH,
                    "The published resource size has drifted.",
                )
            if (
                value.resource_kind != resource_ref.resource_kind
                or value.sha256 != resource_ref.sha256
            ):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The published resource metadata has drifted.",
                )
            if value.resource_kind != ResourceKind.FILE or value.payload is None:
                raise ValueError("directory resources cannot be opened as a byte stream")
            return InMemoryBinaryStream(value.payload)

    def materialize_read_only(
        self,
        resource_ref: ResourceRef,
        destination: Path,
    ) -> MaterializedPath:
        self._maybe_fail("materialize_read_only")
        destination = Path(destination)
        with self._lock:
            value = self._published.get(resource_ref.storage_key)
            if value is None:
                raise _port_error(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "The published resource does not exist.",
                )
            if value.size != resource_ref.size:
                raise _port_error(
                    ErrorCode.RESOURCE_SIZE_MISMATCH,
                    "The published resource size has drifted.",
                )
            if (
                value.resource_kind != resource_ref.resource_kind
                or value.sha256 != resource_ref.sha256
            ):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The published resource metadata has drifted.",
                )
            if value.resource_kind == ResourceKind.FILE:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(value.payload or b"")
                destination.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            else:
                destination.mkdir(parents=True, exist_ok=True)
                for relative, data in (value.tree or {}).items():
                    target = destination / Path(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                for directory in sorted(
                    (path for path in destination.rglob("*") if path.is_dir()),
                    reverse=True,
                ):
                    directory.chmod(
                        stat.S_IRUSR
                        | stat.S_IXUSR
                        | stat.S_IRGRP
                        | stat.S_IXGRP
                        | stat.S_IROTH
                        | stat.S_IXOTH
                    )
                destination.chmod(
                    stat.S_IRUSR
                    | stat.S_IXUSR
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH
                )
        return MaterializedPath(path=str(destination), read_only=True)

    def discard(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> None:
        self._maybe_fail("discard")
        key = self._staged_key(staged_ref)
        with self._lock:
            self.discard_calls.append(_clone(staged_ref))
            self._staged.pop(key, None)
            self._staged_refs.pop(key, None)
            self._staged_completion_markers.discard(key)


class InMemoryStateRepository:
    """Deep-copying StateFile repository with structural mutation support."""

    def __init__(self, state: StateFile | None = None) -> None:
        self._state = _clone(state) if state is not None else None
        self.commit_calls: list[tuple[int, int | None, StateMutation]] = []
        self._commit_failures: deque[BaseException] = deque()
        self._read_failures: defaultdict[str, deque[ApplicationPortError]] = (
            defaultdict(deque)
        )
        self.validation_report: ValidationReport | None = None
        self._lock = threading.RLock()

    def seed(self, state: StateFile) -> None:
        with self._lock:
            self._state = _clone(state)

    def fail_next_commit(self, failure: BaseException) -> None:
        self._commit_failures.append(failure)

    def inject_read_failure(
        self,
        method_name: str,
        failure: ApplicationPortError,
    ) -> None:
        method_key = f"StateRepository.{method_name}"
        if method_key not in {
            "StateRepository.read_case",
            "StateRepository.read_job",
            "StateRepository.read_artifact",
            "StateRepository.read_snapshot",
        }:
            raise ValueError(f"unknown StateRepository read method: {method_name}")
        _validate_scripted_port_error(method_key, failure)
        self._read_failures[method_name].append(failure)

    def _maybe_fail_read(self, method_name: str) -> None:
        failures = self._read_failures[method_name]
        if failures:
            raise failures.popleft()

    def _require_state(self) -> StateFile:
        if self._state is None:
            raise RuntimeError("InMemoryStateRepository has no seeded StateFile")
        return self._state

    def read_case(self, case_id: str) -> CaseAggregate:
        with self._lock:
            self._maybe_fail_read("read_case")
            aggregate = self._require_state().cases.get(case_id)
            if aggregate is None:
                raise _port_error(
                    ErrorCode.CASE_NOT_FOUND,
                    "The requested Case does not exist.",
                )
            return _clone(aggregate)

    def read_job(self, job_id: str) -> Job:
        with self._lock:
            self._maybe_fail_read("read_job")
            for aggregate in self._require_state().cases.values():
                job = aggregate.jobs.get(job_id)
                if job is not None:
                    return _clone(job)
            raise _port_error(
                ErrorCode.JOB_NOT_FOUND,
                "The requested Job does not exist.",
            )

    def read_artifact(self, artifact_id: str) -> Artifact:
        with self._lock:
            self._maybe_fail_read("read_artifact")
            for aggregate in self._require_state().cases.values():
                artifact = aggregate.artifacts.get(artifact_id)
                if artifact is not None:
                    return _clone(artifact)
            raise _port_error(
                ErrorCode.ARTIFACT_NOT_FOUND,
                "The requested Artifact does not exist.",
            )

    def read_snapshot(self) -> StateFile:
        with self._lock:
            self._maybe_fail_read("read_snapshot")
            return _clone(self._require_state())

    @staticmethod
    def _empty_aggregate(case: Any) -> dict[str, Any]:
        return {
            "case": case,
            "jobs": {},
            "outcomes": {},
            "outcome_processing_records": {},
            "execution_failure_records": {},
            "attachments": {},
            "evidence": {},
            "artifacts": {},
        }

    @staticmethod
    def _find_case_id_for_job(cases: Mapping[str, Any], job_id: str) -> str:
        matches = [case_id for case_id, aggregate in cases.items() if job_id in aggregate["jobs"]]
        if len(matches) != 1:
            raise LookupError(f"Job must belong to exactly one Case: {job_id}")
        return matches[0]

    @staticmethod
    def _insert_unique(target: dict[str, Any], key: str, value: Any) -> None:
        if key in target:
            raise ValueError(f"duplicate immutable object: {key}")
        target[key] = value

    @staticmethod
    def _upsert_recovery_processing_record(
        target: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        recovery_id = record["recovery_id"]
        existing = target.get(recovery_id)
        if existing is None:
            target[recovery_id] = record
            return
        if existing == record:
            return
        identity_fields = (
            "recovery_id",
            "current_runtime_epoch",
            "interrupted_job_ids",
            "pending_job_ids",
        )
        same_first_receipt = all(
            existing[field] == record[field] for field in identity_fields
        )
        if (
            same_first_receipt
            and existing["completed_at"] is None
            and record["completed_at"] is not None
        ):
            target[recovery_id] = record
            return
        raise ValueError(
            "RecoveryProcessingRecord is immutable except for its first completion"
        )

    def commit(
        self,
        expected_generation: int,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> CommitReceipt:
        with self._lock:
            if self._commit_failures:
                failure = self._commit_failures.popleft()
                if isinstance(failure, ApplicationPortError):
                    _validate_scripted_port_error(
                        "StateRepository.commit",
                        failure,
                    )
                raise failure
            current = self._require_state()
            self.commit_calls.append(
                (expected_generation, expected_case_revision, _clone(mutation))
            )
            if current.generation != expected_generation:
                raise _port_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The state generation changed before commit.",
                )
            state_data = current.model_dump(mode="python")
            cases: dict[str, Any] = state_data["cases"]
            mutation_data = mutation.model_dump(mode="python")

            upsert_case = mutation_data["upsert_case"]
            affected_case_id: str | None = None
            if upsert_case is not None:
                affected_case_id = upsert_case["case_id"]
                if affected_case_id not in cases:
                    cases[affected_case_id] = self._empty_aggregate(upsert_case)
                else:
                    cases[affected_case_id]["case"] = upsert_case

            if expected_case_revision is not None:
                if affected_case_id is None:
                    candidates: set[str] = set()
                    for job in mutation_data["insert_jobs"]:
                        candidates.add(job["case_id"])
                    for outcome in mutation_data["insert_outcomes"]:
                        candidates.add(outcome["case_id"])
                    for collection in ("upsert_attachments", "insert_evidence", "insert_artifacts"):
                        for item in mutation_data[collection]:
                            candidates.add(item["case_id"])
                    for update in mutation_data["job_lifecycle_updates"]:
                        candidates.add(self._find_case_id_for_job(cases, update["job_id"]))
                    if len(candidates) != 1:
                        raise ValueError("cannot infer one Case for expected_case_revision")
                    affected_case_id = candidates.pop()
                existing_case = current.cases.get(affected_case_id)
                if existing_case is None or existing_case.case.case_revision != expected_case_revision:
                    raise _port_error(
                        ErrorCode.REVISION_CONFLICT,
                        "The Case revision changed before commit.",
                    )

            runtime_by_id = {
                record["runtime_epoch"]: record for record in state_data["runtime_epochs"]
            }
            for record in mutation_data["upsert_runtime_epoch_records"]:
                runtime_by_id[record["runtime_epoch"]] = record
            state_data["runtime_epochs"] = list(runtime_by_id.values())

            for record in mutation_data["upsert_recovery_processing_records"]:
                self._upsert_recovery_processing_record(
                    state_data["recovery_processing_records"],
                    record,
                )

            for job in mutation_data["insert_jobs"]:
                aggregate = cases[job["case_id"]]
                self._insert_unique(aggregate["jobs"], job["job_id"], job)
            for update in mutation_data["job_lifecycle_updates"]:
                case_id = self._find_case_id_for_job(cases, update["job_id"])
                job = cases[case_id]["jobs"][update["job_id"]]
                if job["status"] != update["expected_status"]:
                    raise ValueError("Job lifecycle status conflict")
                job["status"] = update["target_status"]
                for field in ("started_at", "finished_at", "runtime_epoch"):
                    if update[field] is not None:
                        job[field] = update[field]
            for outcome in mutation_data["insert_outcomes"]:
                aggregate = cases[outcome["case_id"]]
                self._insert_unique(
                    aggregate["outcomes"], outcome["outcome_id"], outcome
                )
            for record in mutation_data["insert_outcome_processing_records"]:
                case_id = self._find_case_id_for_job(cases, record["job_id"])
                self._insert_unique(
                    cases[case_id]["outcome_processing_records"],
                    record["outcome_id"],
                    record,
                )
            for record in mutation_data["insert_execution_failure_records"]:
                case_id = self._find_case_id_for_job(cases, record["job_id"])
                self._insert_unique(
                    cases[case_id]["execution_failure_records"],
                    record["failure_id"],
                    record,
                )
            for item, collection, key_name in (
                ("upsert_attachments", "attachments", "attachment_id"),
                ("insert_evidence", "evidence", "evidence_id"),
                ("insert_artifacts", "artifacts", "artifact_id"),
            ):
                for value in mutation_data[item]:
                    target = cases[value["case_id"]][collection]
                    if item == "upsert_attachments":
                        target[value[key_name]] = value
                    else:
                        self._insert_unique(target, value[key_name], value)
            for record in mutation_data["insert_idempotency_records"]:
                compound_key = f"{record['operation']}:{record['idempotency_key']}"
                self._insert_unique(
                    state_data["idempotency_records"], compound_key, record
                )

            state_data["generation"] = current.generation + 1
            if upsert_case is not None:
                state_data["updated_at"] = upsert_case["updated_at"]
            new_state = StateFile.model_validate(state_data)
            self._state = new_state
            case_revision = (
                new_state.cases[affected_case_id].case.case_revision
                if affected_case_id is not None
                else None
            )
            return CommitReceipt(
                generation=new_state.generation,
                case_revision=case_revision,
            )

    @staticmethod
    def _object_counts(state: StateFile) -> StateExportObjectCounts:
        aggregates = tuple(state.cases.values())
        return StateExportObjectCounts(
            cases=len(aggregates),
            jobs=sum(len(item.jobs) for item in aggregates),
            outcomes=sum(len(item.outcomes) for item in aggregates),
            outcome_processing_records=sum(
                len(item.outcome_processing_records) for item in aggregates
            ),
            execution_failure_records=sum(
                len(item.execution_failure_records) for item in aggregates
            ),
            attachments=sum(len(item.attachments) for item in aggregates),
            evidence=sum(len(item.evidence) for item in aggregates),
            artifacts=sum(len(item.artifacts) for item in aggregates),
            idempotency_records=len(state.idempotency_records),
            runtime_epochs=len(state.runtime_epochs),
            recovery_processing_records=len(state.recovery_processing_records),
        )

    def validate_all(self) -> ValidationReport:
        with self._lock:
            if self.validation_report is not None:
                return _clone(self.validation_report)
            state = StateFile.model_validate(
                self._require_state().model_dump(mode="python")
            )
            return ValidationReport(
                valid=True,
                schema_version=state.schema_version,
                contract_revision=state.contract_revision,
                generation=state.generation,
                object_counts=self._object_counts(state),
                errors=[],
            )

    def export_snapshot(self) -> bytes:
        with self._lock:
            return canonical_json_bytes(self._require_state())


def _ref_key(ref: VersionedRef) -> tuple[str, str, str]:
    return (ref.id, ref.version, ref.content_hash)


class FakeAssetCatalog:
    """Exact VersionedRef catalog with separately scripted role bindings."""

    def __init__(
        self,
        *,
        assets: Iterable[ResolvedAsset] = (),
        route: RuntimeBindings | None = None,
        diagnose: Mapping[tuple[str, str, str], RuntimeBindings] | None = None,
        review: Mapping[tuple[str, str, str], RuntimeBindings] | None = None,
    ) -> None:
        self._assets = {_ref_key(asset.ref): _clone(asset) for asset in assets}
        self._route = _clone(route)
        self._diagnose = {key: _clone(value) for key, value in (diagnose or {}).items()}
        self._review = {key: _clone(value) for key, value in (review or {}).items()}
        self.check_calls: list[tuple[VersionedRef, ...]] = []
        self.resolve_calls: list[VersionedRef] = []
        self.route_calls = 0
        self.diagnose_calls: list[VersionedRef] = []
        self.review_calls: list[VersionedRef] = []
        self._failures: defaultdict[str, deque[ApplicationPortError]] = (
            defaultdict(deque)
        )

    def add(self, asset: ResolvedAsset) -> None:
        self._assets[_ref_key(asset.ref)] = _clone(asset)

    def inject_failure(
        self,
        operation: str,
        failure: ApplicationPortError,
    ) -> None:
        if operation == "check":
            raise ValueError("check uses its report channel and allows no exception")
        method_key = f"AssetCatalogPort.{operation}"
        if method_key not in PORT_ERROR_CODES:
            raise ValueError(f"unknown AssetCatalog operation: {operation}")
        _validate_scripted_port_error(method_key, failure)
        self._failures[operation].append(failure)

    def _maybe_fail(self, operation: str) -> None:
        failures = self._failures[operation]
        if failures:
            raise failures.popleft()

    def check(self, refs: Sequence[VersionedRef]) -> AssetAvailabilityReport:
        refs_tuple = tuple(_clone(ref) for ref in refs)
        self.check_calls.append(refs_tuple)
        missing = [ref for ref in refs_tuple if _ref_key(ref) not in self._assets]
        return AssetAvailabilityReport(available=not missing, missing_refs=missing)

    def resolve(self, ref: VersionedRef) -> ResolvedAsset:
        self._maybe_fail("resolve")
        self.resolve_calls.append(_clone(ref))
        value = self._assets.get(_ref_key(ref))
        if value is None:
            raise _port_error(
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The requested pinned asset version is unavailable.",
            )
        return _clone(value)

    def route_bindings(self) -> RuntimeBindings:
        self._maybe_fail("route_bindings")
        self.route_calls += 1
        if self._route is None:
            raise _port_error(
                ErrorCode.CONFIG_INVALID,
                "The built-in route runtime bindings are not configured.",
            )
        return _clone(self._route)

    def diagnose_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        self._maybe_fail("diagnose_bindings")
        self.diagnose_calls.append(_clone(skill_ref))
        value = self._diagnose.get(_ref_key(skill_ref))
        if value is None:
            raise _port_error(
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            )
        return _clone(value)

    def review_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        self._maybe_fail("review_bindings")
        self.review_calls.append(_clone(skill_ref))
        value = self._review.get(_ref_key(skill_ref))
        if value is None:
            raise _port_error(
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned review runtime bindings are unavailable.",
            )
        return _clone(value)


class _FakeLogparseBrokerSession:
    def __init__(self, endpoint: str, token: str, job_id: str) -> None:
        if not endpoint or not token:
            raise ValueError("broker endpoint and token must be non-empty")
        self.endpoint = endpoint
        self.token = token
        self.job_id = job_id
        self.closed = False
        self.token_valid = True
        self.close_calls = 0
        self.live_children = 0
        self._accepted_parse_request_bytes: bytes | None = None
        self._lock = threading.Lock()

    def agent_environment(self) -> dict[str, str]:
        with self._lock:
            if self.closed or not self.token_valid:
                raise RuntimeError("logparse broker session is closed")
            return {
                "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": self.endpoint,
                "PROBLEM_LOCATOR_LOGPARSE_TOKEN": self.token,
            }

    def parse_request_bytes(self) -> bytes | None:
        with self._lock:
            return self._accepted_parse_request_bytes

    def audit_bytes(self) -> bytes:
        with self._lock:
            return canonical_json_bytes(
                {
                    "schema_version": 1,
                    "job_id": self.job_id,
                    "operations": [],
                }
            )

    def _record_parse_request(self, request_bytes: bytes) -> None:
        """Fake-only hook recording the broker's one accepted parse request."""

        if type(request_bytes) is not bytes:
            raise TypeError("parse request bytes must be bytes")
        parse_canonical_json_bytes(request_bytes)
        with self._lock:
            if self.closed:
                raise RuntimeError("a closed broker cannot accept a parse request")
            if self._accepted_parse_request_bytes is not None:
                raise RuntimeError("the broker already accepted its parse request")
            self._accepted_parse_request_bytes = request_bytes

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            if self.closed:
                return
            self.token_valid = False
            self.live_children = 0
            self.endpoint = ""
            self.closed = True


class FakeLogparseBrokerFactory:
    """Records exact job/manifest/cancellation broker bindings."""

    def __init__(
        self,
        *,
        opener: Callable[
            [Job, Path, WorkspaceInputManifest, CancellationSignal],
            LogparseBrokerSession,
        ]
        | None = None,
    ) -> None:
        self.opener = opener
        self.open_calls: list[
            tuple[Job, Path, WorkspaceInputManifest, CancellationSignal]
        ] = []
        self.sessions: list[LogparseBrokerSession] = []

    def open(
        self,
        job: Job,
        workspace_root: Path,
        workspace_manifest: WorkspaceInputManifest,
        cancellation: CancellationSignal,
    ) -> LogparseBrokerSession:
        workspace_root = Path(workspace_root)
        self.open_calls.append(
            (_clone(job), workspace_root, _clone(workspace_manifest), cancellation)
        )
        if self.opener is not None:
            session = self.opener(job, workspace_root, workspace_manifest, cancellation)
        else:
            ordinal = len(self.sessions) + 1
            session = _FakeLogparseBrokerSession(
                f"inmemory://problem-locator/logparse/{job.job_id}/{ordinal}",
                f"contract-test-token-{ordinal}",
                job.job_id,
            )
        self.sessions.append(session)
        return session


class RecordingDispatcher:
    """Deduplicating dispatcher recorder with explicit rejection injection."""

    def __init__(self) -> None:
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.accepted_job_ids: list[str] = []
        self.cancelled_job_ids: list[str] = []
        self._known: set[str] = set()
        self.reject_next_submit = False
        self._lock = threading.Lock()

    def submit(self, job_id: str) -> DispatchReceipt:
        with self._lock:
            self.submit_calls.append(job_id)
            if self.reject_next_submit:
                self.reject_next_submit = False
                return DispatchReceipt(job_id=job_id, accepted=False, duplicate=False)
            duplicate = job_id in self._known
            if not duplicate:
                self._known.add(job_id)
                self.accepted_job_ids.append(job_id)
            return DispatchReceipt(
                job_id=job_id,
                accepted=not duplicate,
                duplicate=duplicate,
            )

    def cancel(self, job_id: str) -> CancelReceipt:
        with self._lock:
            self.cancel_calls.append(job_id)
            signalled = job_id in self._known and job_id not in self.cancelled_job_ids
            if signalled:
                self.cancelled_job_ids.append(job_id)
            return CancelReceipt(job_id=job_id, signalled=signalled)


class InMemoryStateChangeNotifier:
    """Generation-aware condition variable; notifications are only hints."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._generation_by_case: dict[str, int] = {}
        self.notify_calls: list[tuple[str, int]] = []
        self.wait_calls: list[tuple[str, int, float]] = []

    def notify(self, case_id: str, generation: int) -> None:
        if generation < 0:
            raise ValueError("generation must be non-negative")
        with self._condition:
            self.notify_calls.append((case_id, generation))
            self._generation_by_case[case_id] = max(
                generation,
                self._generation_by_case.get(case_id, -1),
            )
            self._condition.notify_all()

    def wait_for_change(
        self,
        case_id: str,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._condition:
            self.wait_calls.append((case_id, after_generation, timeout_seconds))
            deadline = time.monotonic() + timeout_seconds
            while self._generation_by_case.get(case_id, -1) <= after_generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class _CombinedLogCounter:
    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError("combined log limit must be non-negative")
        self.limit = limit
        self.total = 0
        self.lock = threading.Lock()


class _InMemoryAppendOnlyByteSink:
    def __init__(self, counter: _CombinedLogCounter) -> None:
        self._counter = counter
        self._data = bytearray()
        self._closed = False
        self.flush_calls = 0
        self.close_calls = 0

    @property
    def data(self) -> bytes:
        with self._counter.lock:
            return bytes(self._data)

    def write(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("write requires non-empty bytes")
        with self._counter.lock:
            if self._closed:
                raise ValueError("sink is closed")
            if self._counter.total + len(chunk) > self._counter.limit:
                raise ValueError("combined execution log limit exceeded")
            self._data.extend(chunk)
            self._counter.total += len(chunk)

    def flush(self) -> None:
        with self._counter.lock:
            self.flush_calls += 1

    def close(self) -> None:
        with self._counter.lock:
            self.close_calls += 1
            self._closed = True


class InMemoryExecutionRecordStore:
    """Canonical byte execution-record store with shared bounded log sinks."""

    def __init__(self) -> None:
        self._job_bytes: dict[str, bytes] = {}
        self._outcome_bytes: dict[str, bytes] = {}
        self._rejected_agent_output_bytes: dict[str, bytes] = {}
        self._audit_bytes: dict[tuple[str, str], bytes] = {}
        self.log_sinks: dict[str, ExecutionLogSinks] = {}
        self.publish_job_calls: list[Job] = []
        self.publish_outcome_calls: list[tuple[str, bytes]] = []
        self.publish_rejected_agent_output_calls: list[tuple[str, bytes]] = []
        self._failures: defaultdict[str, deque[BaseException]] = defaultdict(deque)
        self._lock = threading.RLock()

    def inject_failure(self, operation: str, failure: BaseException) -> None:
        self._failures[operation].append(failure)

    def _maybe_fail(self, operation: str) -> None:
        failures = self._failures[operation]
        if failures:
            failure = failures.popleft()
            if isinstance(failure, ApplicationPortError):
                _validate_scripted_port_error(
                    f"ExecutionRecordStore.{operation}",
                    failure,
                )
            raise failure

    @staticmethod
    def _file_ref(relative_key: str, data: bytes) -> ExecutionFileRef:
        return ExecutionFileRef(
            relative_key=relative_key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def publish_job(self, job: Job) -> ExecutionFileRef:
        self._maybe_fail("publish_job")
        if job.status != JobStatus.PENDING:
            raise _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The Job execution record is not publishable.",
            )
        data = canonical_json_bytes(job)
        with self._lock:
            self.publish_job_calls.append(_clone(job))
            existing = self._job_bytes.get(job.job_id)
            if existing is not None and existing != data:
                raise _port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The Job execution record conflicts with existing bytes.",
                )
            self._job_bytes[job.job_id] = data
            return self._file_ref(f"jobs/{job.job_id}/job.json", data)

    def publish_outcome_bytes(
        self,
        job_id: str,
        canonical_bytes: bytes,
    ) -> ExecutionFileRef:
        self._maybe_fail("publish_outcome_bytes")
        try:
            outcome = parse_canonical_json_bytes(canonical_bytes, JobOutcome)
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The Outcome execution record is invalid.",
            ) from None
        if outcome.job_id != job_id:
            raise _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The Outcome execution record does not match its Job.",
            )
        with self._lock:
            self.publish_outcome_calls.append((job_id, canonical_bytes))
            existing = self._outcome_bytes.get(job_id)
            if existing is not None and existing != canonical_bytes:
                raise _port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The Outcome execution record conflicts with existing bytes.",
                )
            self._outcome_bytes[job_id] = canonical_bytes
            return self._file_ref(
                f"jobs/{job_id}/job_outcome.json",
                canonical_bytes,
            )

    def publish_rejected_agent_output_bytes(
        self,
        job_id: str,
        raw_bytes: bytes,
    ) -> ExecutionFileRef:
        self._maybe_fail("publish_rejected_agent_output_bytes")
        if type(raw_bytes) is not bytes:
            raise _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The rejected Agent output is not exact bytes.",
            )
        with self._lock:
            self.publish_rejected_agent_output_calls.append((job_id, raw_bytes))
            existing = self._rejected_agent_output_bytes.get(job_id)
            if existing is not None and existing != raw_bytes:
                raise _port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The rejected Agent output conflicts with existing bytes.",
                )
            self._rejected_agent_output_bytes[job_id] = raw_bytes
            return self._file_ref(
                f"jobs/{job_id}/agent_job_outcome.rejected.json",
                raw_bytes,
            )

    def publish_audit_bytes(
        self,
        job_id: str,
        filename: str,
        raw_bytes: bytes,
    ) -> ExecutionFileRef:
        self._maybe_fail("publish_audit_bytes")
        if type(raw_bytes) is not bytes or not filename:
            raise _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The audit execution record is invalid.",
            )
        key = (job_id, filename)
        with self._lock:
            existing = self._audit_bytes.get(key)
            if existing is not None and existing != raw_bytes:
                raise _port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The audit execution record conflicts with existing bytes.",
                )
            self._audit_bytes[key] = raw_bytes
        return self._file_ref(f"jobs/{job_id}/{filename}", raw_bytes)

    def read_audit_bytes(self, job_id: str, filename: str) -> bytes | None:
        self._maybe_fail("read_audit_bytes")
        with self._lock:
            if filename == "job.json":
                return self._job_bytes.get(job_id)
            if filename == "job_outcome.json":
                return self._outcome_bytes.get(job_id)
            return self._audit_bytes.get((job_id, filename))

    def read_published_job(self, job_id: str) -> PublishedJobReceipt | None:
        self._maybe_fail("read_published_job")
        with self._lock:
            data = self._job_bytes.get(job_id)
            if data is None:
                return None
            try:
                job = parse_canonical_json_bytes(data, Job)
                if job.job_id != job_id or job.status != JobStatus.PENDING:
                    raise ValueError("invalid binding")
                return PublishedJobReceipt(
                    job=job,
                    job_file_ref=self._file_ref(f"jobs/{job_id}/job.json", data),
                )
            except (TypeError, ValueError, ValidationError):
                raise _port_error(
                    ErrorCode.EXECUTION_RECORD_FAILED,
                    "The published Job execution record is invalid.",
                ) from None

    def read_published_outcome(
        self,
        job_id: str,
    ) -> RuntimeExecutionReceipt | None:
        self._maybe_fail("read_published_outcome")
        with self._lock:
            data = self._outcome_bytes.get(job_id)
            if data is None:
                return None
            try:
                outcome = parse_canonical_json_bytes(data, JobOutcome)
                if outcome.job_id != job_id:
                    raise ValueError("invalid binding")
                return RuntimeExecutionReceipt(
                    job_outcome=outcome,
                    outcome_file_ref=self._file_ref(
                        f"jobs/{job_id}/job_outcome.json",
                        data,
                    ),
                )
            except (TypeError, ValueError, ValidationError):
                raise _port_error(
                    ErrorCode.EXECUTION_RECORD_FAILED,
                    "The published Outcome execution record is invalid.",
                ) from None

    def open_log_sinks(
        self,
        job_id: str,
        combined_limit_bytes: int,
    ) -> ExecutionLogSinks:
        self._maybe_fail("open_log_sinks")
        counter = _CombinedLogCounter(combined_limit_bytes)
        sinks = ExecutionLogSinks(
            stdout=_InMemoryAppendOnlyByteSink(counter),
            stderr=_InMemoryAppendOnlyByteSink(counter),
            combined_limit_bytes=combined_limit_bytes,
        )
        with self._lock:
            self.log_sinks[job_id] = sinks
        return sinks


class ScriptedRuntime:
    def __init__(self, results: Iterable[Any] = ()) -> None:
        self._script = _Script(results)
        self.calls: list[tuple[Job, CancellationSignal]] = []

    def queue(self, *results: Any) -> None:
        for result in results:
            self._script.append(result)

    def execute(
        self,
        job: Job,
        cancellation: CancellationSignal,
    ) -> RuntimeExecutionReceipt:
        self.calls.append((_clone(job), cancellation))
        return _take_port_script(
            "Runtime.execute",
            self._script,
            job,
            cancellation,
        )


class ScriptedCoordinator:
    def __init__(self, plans: Iterable[Any] = ()) -> None:
        self._script = _Script(plans)
        self.calls: list[tuple[CaseSnapshot, ValidatedTrigger]] = []

    def queue(self, *plans: Any) -> None:
        for plan in plans:
            self._script.append(plan)

    def plan(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        self.calls.append((_clone(snapshot), _clone(trigger)))
        result = self._script.take("Coordinator.plan", snapshot, trigger)
        return validate_coordinator_plan_result(trigger, result)


class RecordingApplicationCommand:
    def __init__(self, responses: Iterable[Any] = ()) -> None:
        self._script = _Script(responses)
        self.calls: list[ApplicationCommand] = []

    def queue(self, *responses: Any) -> None:
        for response in responses:
            self._script.append(response)

    def execute(self, command: ApplicationCommand) -> ApplicationResponse:
        self.calls.append(_clone(command))
        return _take_port_script(
            "ApplicationCommandPort.execute",
            self._script,
            command,
        )


class StubApplicationQuery:
    def __init__(self) -> None:
        self._scripts = {
            "get_case": _Script(),
            "list_artifacts": _Script(),
            "open_artifact": _Script(),
        }
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def queue(self, method: str, *results: Any) -> None:
        script = self._scripts.get(method)
        if script is None:
            raise ValueError(f"unknown query method: {method}")
        for result in results:
            script.append(result)

    def get_case(
        self,
        case_id: str,
        wait_for_job_id: str | None = None,
        wait_seconds: WaitSeconds = 0,
    ) -> CaseQueryResponse:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.get_case",
            GetCase,
            {
                "case_id": case_id,
                "wait_for_job_id": wait_for_job_id,
                "wait_seconds": wait_seconds,
            },
        )
        assert isinstance(validated, GetCase)
        args = (
            validated.case_id,
            validated.wait_for_job_id,
            validated.wait_seconds,
        )
        self.calls.append(("get_case", args))
        return _take_port_script(
            "ApplicationQueryPort.get_case",
            self._scripts["get_case"],
            *args,
        )

    def list_artifacts(
        self,
        case_id: str,
        include_internal: bool = False,
    ) -> ArtifactListResponse:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.list_artifacts",
            ListArtifacts,
            {"case_id": case_id, "include_internal": include_internal},
        )
        assert isinstance(validated, ListArtifacts)
        args = (validated.case_id, validated.include_internal)
        self.calls.append(("list_artifacts", args))
        return _take_port_script(
            "ApplicationQueryPort.list_artifacts",
            self._scripts["list_artifacts"],
            *args,
        )

    def open_artifact(
        self,
        case_id: str,
        artifact_id: str,
    ) -> OpenArtifactResult:
        validated = _validate_raw_port_input(
            "ApplicationQueryPort.open_artifact",
            OpenArtifact,
            {"case_id": case_id, "artifact_id": artifact_id},
        )
        assert isinstance(validated, OpenArtifact)
        args = (validated.case_id, validated.artifact_id)
        self.calls.append(("open_artifact", args))
        return _take_port_script(
            "ApplicationQueryPort.open_artifact",
            self._scripts["open_artifact"],
            *args,
        )


class StubJobControl:
    def __init__(self) -> None:
        self._scripts = {
            "claim_job": _Script(),
            "submit_outcome": _Script(),
            "report_execution_infrastructure_failure": _Script(),
            "interrupt_previous_epoch": _Script(),
        }
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def queue(self, method: str, *results: Any) -> None:
        script = self._scripts.get(method)
        if script is None:
            raise ValueError(f"unknown job-control method: {method}")
        for result in results:
            script.append(result)

    def claim_job(self, job_id: str, runtime_epoch: str) -> ClaimReceipt:
        validated = _validate_raw_port_input(
            "JobControlPort.claim_job",
            ClaimJob,
            {"job_id": job_id, "runtime_epoch": runtime_epoch},
        )
        assert isinstance(validated, ClaimJob)
        args = (validated.job_id, validated.runtime_epoch)
        self.calls.append(("claim_job", args))
        return _take_port_script(
            "JobControlPort.claim_job",
            self._scripts["claim_job"],
            *args,
        )

    def submit_outcome(
        self,
        job_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
    ) -> OutcomeReceipt:
        validated = _validate_raw_port_input(
            "JobControlPort.submit_outcome",
            SubmitJobOutcome,
            {
                "job_outcome": _rebuild_contract_input(job_outcome),
                "outcome_file_ref": _rebuild_contract_input(outcome_file_ref),
            },
        )
        assert isinstance(validated, SubmitJobOutcome)
        args = (_clone(validated.job_outcome), _clone(validated.outcome_file_ref))
        self.calls.append(("submit_outcome", args))
        return _take_port_script(
            "JobControlPort.submit_outcome",
            self._scripts["submit_outcome"],
            validated.job_outcome,
            validated.outcome_file_ref,
        )

    def report_execution_infrastructure_failure(
        self,
        job_id: str,
        runtime_epoch: str,
        failure_id: str,
        execution_failure: ExecutionFailure,
    ) -> FailureReceipt:
        args = (job_id, runtime_epoch, failure_id, _clone(execution_failure))
        self.calls.append(("report_execution_infrastructure_failure", args))
        return _take_port_script(
            "JobControlPort.report_execution_infrastructure_failure",
            self._scripts["report_execution_infrastructure_failure"],
            job_id,
            runtime_epoch,
            failure_id,
            execution_failure,
        )

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryReceipt:
        validated = _validate_raw_port_input(
            "JobControlPort.interrupt_previous_epoch",
            InterruptPreviousEpoch,
            {
                "current_runtime_epoch": current_runtime_epoch,
                "recovery_id": recovery_id,
            },
        )
        assert isinstance(validated, InterruptPreviousEpoch)
        args = (validated.current_runtime_epoch, validated.recovery_id)
        self.calls.append(("interrupt_previous_epoch", args))
        return _take_port_script(
            "JobControlPort.interrupt_previous_epoch",
            self._scripts["interrupt_previous_epoch"],
            *args,
        )


class StubStateAdmin:
    def __init__(self) -> None:
        self._scripts = {
            "readiness": _Script(),
            "validate_state": _Script(),
            "export_state": _Script(),
        }
        self.calls: list[str] = []

    def queue(self, method: str, *results: Any) -> None:
        script = self._scripts.get(method)
        if script is None:
            raise ValueError(f"unknown state-admin method: {method}")
        for result in results:
            script.append(result)

    def readiness(self) -> ReadinessReport:
        self.calls.append("readiness")
        return _take_port_script(
            "StateAdminPort.readiness",
            self._scripts["readiness"],
        )

    def validate_state(self) -> ValidationReport:
        self.calls.append("validate_state")
        return _take_port_script(
            "StateAdminPort.validate_state",
            self._scripts["validate_state"],
        )

    def export_state(self) -> bytes:
        self.calls.append("export_state")
        return _take_port_script(
            "StateAdminPort.export_state",
            self._scripts["export_state"],
        )


class CountingLogparseAdapter:
    """Small parse/target-logs script used to prove parse-once behaviour."""

    def __init__(
        self,
        *,
        parse_results: Iterable[Any] = (),
        target_log_results: Iterable[Any] = (),
    ) -> None:
        self._parse_script = _Script(parse_results)
        self._target_script = _Script(target_log_results)
        self.parse_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.target_log_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @property
    def parse_count(self) -> int:
        return len(self.parse_calls)

    @property
    def target_logs_count(self) -> int:
        return len(self.target_log_calls)

    def queue_parse(self, *results: Any) -> None:
        for result in results:
            self._parse_script.append(result)

    def queue_target_logs(self, *results: Any) -> None:
        for result in results:
            self._target_script.append(result)

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        self.parse_calls.append((_clone(args), _clone(kwargs)))
        return self._parse_script.take("logparse parse", *args, **kwargs)

    def parse_targets(self, *args: Any, **kwargs: Any) -> Any:
        return self.parse(*args, **kwargs)

    def target_logs(self, *args: Any, **kwargs: Any) -> Any:
        self.target_log_calls.append((_clone(args), _clone(kwargs)))
        return self._target_script.take("logparse target-logs", *args, **kwargs)

    def mech_target_logs(self, *args: Any, **kwargs: Any) -> Any:
        return self.target_logs(*args, **kwargs)

    def reset_counts(self) -> None:
        self.parse_calls.clear()
        self.target_log_calls.clear()


__all__ = [
    "CountingLogparseAdapter",
    "DeterministicIdGenerator",
    "FakeAssetCatalog",
    "FakeClock",
    "FakeLogparseBrokerFactory",
    "InMemoryAttachmentUploadGuard",
    "InMemoryBinaryStream",
    "InMemoryCancellationSignal",
    "InMemoryExecutionRecordStore",
    "InMemoryPublicationCommitGuard",
    "InMemoryResourceStore",
    "InMemoryStateChangeNotifier",
    "InMemoryStateRepository",
    "PureContextSnapshotProjector",
    "RecordingApplicationCommand",
    "RecordingDispatcher",
    "ScriptedCoordinator",
    "ScriptedRuntime",
    "StubApplicationQuery",
    "StubJobControl",
    "StubStateAdmin",
]


# Keep the public fake-to-port relationship executable and discoverable.
assert isinstance(FakeClock(), Clock)
assert isinstance(DeterministicIdGenerator(), IdGenerator)
assert isinstance(PureContextSnapshotProjector(), ContextSnapshotProjector)
assert isinstance(InMemoryStateRepository(), StateRepository)
assert isinstance(InMemoryResourceStore(), ResourceStore)
assert isinstance(InMemoryPublicationCommitGuard(), PublicationCommitGuard)
assert isinstance(InMemoryAttachmentUploadGuard(), AttachmentUploadGuard)
assert isinstance(InMemoryBinaryStream(), BinaryStream)
assert isinstance(InMemoryCancellationSignal(), CancellationSignal)
assert isinstance(FakeAssetCatalog(), AssetCatalogPort)
assert isinstance(FakeLogparseBrokerFactory(), LogparseBrokerFactory)
assert isinstance(RecordingDispatcher(), Dispatcher)
assert isinstance(InMemoryStateChangeNotifier(), StateChangeNotifier)
assert isinstance(InMemoryExecutionRecordStore(), ExecutionRecordStore)
assert isinstance(ScriptedRuntime(), Runtime)
assert isinstance(ScriptedCoordinator(), Coordinator)
assert isinstance(RecordingApplicationCommand(), ApplicationCommandPort)
assert isinstance(StubApplicationQuery(), ApplicationQueryPort)
assert isinstance(StubJobControl(), JobControlPort)
assert isinstance(StubStateAdmin(), StateAdminPort)
