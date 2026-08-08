"""Production composition root for Problem Locator V1.

This module is the only place where the independently implemented V1 slices
are joined.  Importing it does not acquire a lock, create storage, start a
thread, or inspect configured assets; those effects begin only when one of the
explicit factory functions is called.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from problem_locator.application import ApplicationService, build_application_service
from problem_locator.contracts import (
    ERROR_SPECS,
    ApplicationError,
    ApplicationPortError,
    AttachmentStatus,
    CancelReceipt,
    Dispatcher,
    DispatchReceipt,
    ErrorCode,
    LogparseBrokerFactory,
    ReadinessCheck,
    ReadinessReport,
    ResolvedAsset,
    ResourceKind,
    SCHEMA_VERSION,
    StateExport,
    StateExportObjectCounts,
    StateExportResource,
    StateFile,
    UPLOAD_TEMP_RETENTION_SECONDS,
    ValidationIssue,
    ValidationReport,
    canonical_json_bytes,
)
from problem_locator.dispatch import RecoveryResult, SchedulerService
from problem_locator.diagnostics import log_event
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.entrypoints.cli import CliHooks, main as cli_main, run_uvicorn
from problem_locator.entrypoints.replay import (
    ReplayRequest,
    ReplayResult,
    run_replay_job as execute_replay_job,
)
from problem_locator.entrypoints.settings import Settings
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.interfaces.composition_hooks import (
    InterfaceDependencies,
    create_asgi_app,
)
from problem_locator.runtime.agent_backend import AgentBackend
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from problem_locator.storage.atomic import require_ordinary_file, require_real_directory
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    InProcessAttachmentUploadGuard,
    InProcessPublicationCommitGuard,
    StorageCoordinationLock,
)
from problem_locator.storage.execution_records import FileExecutionRecordStore
from problem_locator.storage.layout import StorageLayout, UnsupportedDataFormatError
from problem_locator.storage.platform import (
    FileInstanceLock,
    PlatformFileSync,
    PlatformReplaceOperation,
)
from problem_locator.storage.quarantine import QuarantineMover
from problem_locator.storage.resource_store import FileResourceStore
from problem_locator.storage.retention import RetentionScanner
from problem_locator.storage.retention_cleaner import (
    CleanupRunResult,
    StorageRetentionCleaner,
)
from problem_locator.storage.state_repository import JsonFileStateRepository


_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_READINESS_NAMES = (
    "CONFIG",
    "INSTANCE_LOCK",
    "STATE",
    "DATA_DIRECTORIES",
    "RECOVERY",
)


def _application_error(code: ErrorCode, message: str) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=message,
        details=[],
        retryable=ERROR_SPECS[code].application_retryable,
    )


def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(_application_error(code, message))


def _empty_counts() -> StateExportObjectCounts:
    return StateExportObjectCounts(
        cases=0,
        jobs=0,
        outcomes=0,
        outcome_processing_records=0,
        execution_failure_records=0,
        attachments=0,
        evidence=0,
        artifacts=0,
        idempotency_records=0,
        runtime_epochs=0,
        recovery_processing_records=0,
    )


def _invalid_report(error: ApplicationError) -> ValidationReport:
    code = error.code
    if code not in {
        ErrorCode.INSTANCE_LOCKED,
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
    }:
        code = ErrorCode.STATE_CORRUPT
    return ValidationReport(
        valid=False,
        schema_version=None,
        contract_revision=None,
        generation=None,
        object_counts=_empty_counts(),
        errors=[
            ValidationIssue(
                code=code.value,
                object_type="StateFile",
                object_id=None,
                field_path=None,
                message=(
                    "The installation is already open by another process."
                    if code is ErrorCode.INSTANCE_LOCKED
                    else "The stored state could not be validated."
                ),
            )
        ],
    )


class ProductionClock:
    """UTC clock with the frozen millisecond RFC 3339 representation."""

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")


class UuidIdGenerator:
    """Random production IDs plus deterministic UUIDv5 derived identities."""

    @staticmethod
    def _kind(kind: str) -> str:
        if not isinstance(kind, str) or not kind:
            raise ValueError("ID kind must be non-empty text")
        return kind

    def new(self, kind: str) -> str:
        self._kind(kind)
        return str(uuid.uuid4())

    def derive(self, kind: str, stable_parts: Sequence[str]) -> str:
        validated_kind = self._kind(kind)
        if isinstance(stable_parts, (str, bytes)) or not isinstance(
            stable_parts, Sequence
        ):
            raise TypeError("stable_parts must be a sequence of strings")
        parts = list(stable_parts)
        if not parts or any(
            not isinstance(part, str) or not part for part in parts
        ):
            raise ValueError(
                "stable_parts must contain only non-empty strings"
            )
        name = canonical_json_bytes(
            {"kind": validated_kind, "parts": parts}
        )[:-1].decode("utf-8")
        return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


class ThreadStateChangeNotifier:
    """Process-local generation notifier with no lost wakeups."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest_generation: dict[str, int] = {}

    @staticmethod
    def _generation(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
        return value

    def notify(self, case_id: str, generation: int) -> None:
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id must be non-empty text")
        current = self._generation(generation, "generation")
        with self._condition:
            previous = self._latest_generation.get(case_id, -1)
            if current > previous:
                self._latest_generation[case_id] = current
            self._condition.notify_all()

    def wait_for_change(
        self,
        case_id: str,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id must be non-empty text")
        after = self._generation(after_generation, "after_generation")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            return self._condition.wait_for(
                lambda: self._latest_generation.get(case_id, -1) > after,
                timeout=max(0.0, deadline - time.monotonic()),
            )


class LateBoundDispatcher:
    """One-time binding that breaks the S03/S05 construction cycle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: Dispatcher | None = None

    @property
    def target(self) -> Dispatcher | None:
        with self._lock:
            return self._target

    def bind(self, target: Dispatcher) -> None:
        if not isinstance(target, Dispatcher):
            raise TypeError("dispatcher target must implement Dispatcher")
        with self._lock:
            if self._target is not None:
                raise RuntimeError("dispatcher is already bound")
            self._target = target

    def submit(self, job_id: str) -> DispatchReceipt:
        target = self.target
        if target is None:
            return DispatchReceipt(job_id=job_id, accepted=False, duplicate=False)
        return target.submit(job_id)

    def cancel(self, job_id: str) -> CancelReceipt:
        target = self.target
        if target is None:
            return CancelReceipt(job_id=job_id, signalled=False)
        return target.cancel(job_id)


def _layout_directories(layout: StorageLayout) -> tuple[Path, ...]:
    return (
        layout.data_root,
        layout.resources,
        layout.cases_resources,
        layout.jobs,
        layout.temporary,
        layout.uploads,
        layout.proposals,
        layout.workspaces,
        layout.quarantine,
        layout.state_temporary,
    )


def _directories_valid(layout: StorageLayout) -> bool:
    try:
        for directory in _layout_directories(layout):
            require_real_directory(directory)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _state_error_from_report(report: ValidationReport) -> ApplicationPortError:
    code = ErrorCode.STATE_CORRUPT
    if report.errors:
        try:
            candidate = ErrorCode(report.errors[0].code)
        except ValueError:
            candidate = ErrorCode.STATE_CORRUPT
        if candidate is ErrorCode.STATE_SCHEMA_UNSUPPORTED:
            code = candidate
    return _port_error(code, "The stored state could not be exported safely.")


def _state_resources(state: StateFile) -> list[StateExportResource]:
    by_key: dict[str, StateExportResource] = {}

    def include(resource: StateExportResource) -> None:
        existing = by_key.get(resource.storage_key)
        if existing is not None and existing != resource:
            raise ValueError("one storage key has conflicting resource metadata")
        by_key[resource.storage_key] = resource

    for aggregate in state.cases.values():
        for attachment in aggregate.attachments.values():
            if attachment.status is AttachmentStatus.READY:
                assert (
                    attachment.storage_key is not None
                    and attachment.size is not None
                    and attachment.sha256 is not None
                )
                include(
                    StateExportResource(
                        resource_kind=ResourceKind.FILE,
                        storage_key=attachment.storage_key,
                        size=attachment.size,
                        sha256=attachment.sha256,
                    )
                )
        for evidence in aggregate.evidence.values():
            if evidence.resource_ref is not None:
                include(
                    StateExportResource.model_validate(
                        evidence.resource_ref.model_dump(mode="python")
                    )
                )
        for artifact in aggregate.artifacts.values():
            include(
                StateExportResource(
                    resource_kind=artifact.resource_kind,
                    storage_key=artifact.storage_key,
                    size=artifact.size,
                    sha256=artifact.sha256,
                )
            )
    return [by_key[key] for key in sorted(by_key)]


def _export_state(
    repository: JsonFileStateRepository,
    coordination_lock: StorageCoordinationLock,
) -> bytes:
    with coordination_lock:
        report = repository.validate_all()
        if not report.valid:
            raise _state_error_from_report(report)
        state = repository.read_snapshot()
        if (
            report.schema_version != state.schema_version
            or report.contract_revision != state.contract_revision
            or report.generation != state.generation
        ):
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The stored state changed during export validation.",
            )
        try:
            exported = StateExport(
                export_schema_version=SCHEMA_VERSION,
                schema_version=state.schema_version,
                contract_revision=state.contract_revision,
                source_generation=state.generation,
                installation_id=state.installation_id,
                object_counts=report.object_counts,
                state=state,
                resources=_state_resources(state),
            )
            return canonical_json_bytes(exported)
        except (TypeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The stored state could not be exported safely.",
            ) from exc


class ServiceStateAdmin:
    """Thin StateAdmin facade for one already-open service installation."""

    def __init__(
        self,
        *,
        layout: StorageLayout,
        instance_lock: FileInstanceLock,
        coordination_lock: StorageCoordinationLock,
        repository: JsonFileStateRepository,
        scheduler: SchedulerService,
    ) -> None:
        self._layout = layout
        self._instance_lock = instance_lock
        self._coordination_lock = coordination_lock
        self._repository = repository
        self._scheduler = scheduler

    def readiness(self) -> ReadinessReport:
        try:
            validation = self._repository.validate_all()
        except Exception:
            validation = _invalid_report(
                _application_error(
                    ErrorCode.STATE_CORRUPT,
                    "The stored state could not be validated.",
                )
            )
        checks_passed = {
            "CONFIG": True,
            "INSTANCE_LOCK": self._instance_lock.is_acquired(),
            "STATE": validation.valid,
            "DATA_DIRECTORIES": _directories_valid(self._layout),
            "RECOVERY": self._scheduler.ready,
        }
        messages = {
            "CONFIG": "Configuration validation failed.",
            "INSTANCE_LOCK": "The installation lock is not held.",
            "STATE": "State validation failed.",
            "DATA_DIRECTORIES": "The storage layout is unavailable.",
            "RECOVERY": "Startup recovery is incomplete.",
        }
        checks = [
            ReadinessCheck(
                name=name,
                passed=checks_passed[name],
                message=None if checks_passed[name] else messages[name],
            )
            for name in _READINESS_NAMES
        ]
        if all(checks_passed.values()):
            return ReadinessReport(ready=True, checks=checks, error=None)

        if not checks_passed["INSTANCE_LOCK"]:
            code = ErrorCode.INSTANCE_LOCKED
        elif not checks_passed["STATE"]:
            try:
                code = ErrorCode(validation.errors[0].code)
            except (IndexError, ValueError):
                code = ErrorCode.STATE_CORRUPT
            if code not in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                code = ErrorCode.STATE_CORRUPT
        elif not checks_passed["DATA_DIRECTORIES"]:
            code = ErrorCode.STATE_CORRUPT
        else:
            code = (
                self._scheduler.fatal_worker_error_code
                or (
                    self._scheduler.recovery_result.failure_code
                    if self._scheduler.recovery_result is not None
                    else None
                )
                or ErrorCode.DISPATCH_REJECTED
            )
        return ReadinessReport(
            ready=False,
            checks=checks,
            error=_application_error(code, "The service is not ready."),
        )

    def validate_state(self) -> ValidationReport:
        return self._repository.validate_all()

    def export_state(self) -> bytes:
        return _export_state(self._repository, self._coordination_lock)


class StandaloneStateAdmin:
    """Lock-scoped StateAdmin used only by offline validate/export commands."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)

    @contextmanager
    def _open_repository(
        self,
    ) -> Iterator[tuple[JsonFileStateRepository, StorageCoordinationLock]]:
        try:
            layout = StorageLayout.at(self._data_root)
            layout.validate_v2_data_format()
            for directory in _layout_directories(layout):
                require_real_directory(directory)
            require_ordinary_file(layout.state)
        except UnsupportedDataFormatError as exc:
            raise _port_error(
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                "The DATA_ROOT data format is unsupported; configure a fresh DATA_ROOT.",
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The stored installation layout is invalid.",
            ) from exc

        instance_lock = FileInstanceLock(layout.instance_lock)
        try:
            instance_lock.acquire()
        except (OSError, RuntimeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.INSTANCE_LOCKED,
                "The installation is already open by another process.",
            ) from exc

        try:
            coordination_lock = StorageCoordinationLock()
            records = FileExecutionRecordStore(layout.data_root, coordination_lock)
            repository = JsonFileStateRepository(
                layout.data_root,
                coordination_lock,
                ProductionClock(),
                UuidIdGenerator(),
                execution_record_store=records,
            )
            yield repository, coordination_lock
        except ApplicationPortError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The stored state could not be opened safely.",
            ) from exc
        finally:
            instance_lock.release()

    def readiness(self) -> ReadinessReport:
        validation = self.validate_state()
        try:
            layout_valid = _directories_valid(StorageLayout.at(self._data_root))
        except (TypeError, ValueError):
            layout_valid = False
        passed = {
            "CONFIG": True,
            # Offline calls release their lock before returning and never run
            # Scheduler recovery, so they must not masquerade as a service.
            "INSTANCE_LOCK": False,
            "STATE": validation.valid,
            "DATA_DIRECTORIES": layout_valid,
            "RECOVERY": False,
        }
        checks = [
            ReadinessCheck(
                name=name,
                passed=passed[name],
                message=(
                    None
                    if passed[name]
                    else "Offline administration is not a running service."
                ),
            )
            for name in _READINESS_NAMES
        ]
        return ReadinessReport(
            ready=False,
            checks=checks,
            error=_application_error(
                (
                    ErrorCode.INSTANCE_LOCKED
                    if validation.valid
                    else ErrorCode.STATE_CORRUPT
                ),
                "Offline administration is not a running service.",
            ),
        )

    def validate_state(self) -> ValidationReport:
        try:
            with self._open_repository() as (repository, _):
                return repository.validate_all()
        except ApplicationPortError as exc:
            return _invalid_report(exc.error)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _invalid_report(
                _application_error(
                    ErrorCode.STATE_CORRUPT,
                    "The stored state could not be validated.",
                )
            )

    def export_state(self) -> bytes:
        try:
            with self._open_repository() as (repository, coordination_lock):
                return _export_state(repository, coordination_lock)
        except ApplicationPortError as exc:
            if exc.error.code in {
                ErrorCode.INSTANCE_LOCKED,
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                ErrorCode.RESOURCE_NOT_FOUND,
                ErrorCode.RESOURCE_HASH_MISMATCH,
                ErrorCode.RESOURCE_SIZE_MISMATCH,
            }:
                raise
            raise _port_error(
                ErrorCode.STATE_CORRUPT,
                "The stored state could not be exported safely.",
            ) from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _port_error(
                ErrorCode.INSTANCE_LOCKED,
                "The installation lock could not be released safely.",
            ) from exc


class RetentionService:
    """Single managed thread for S02's startup and 24-hour cleanup cadence."""

    def __init__(
        self,
        *,
        layout: StorageLayout,
        coordination_lock: StorageCoordinationLock,
        repository: JsonFileStateRepository,
        resource_store: FileResourceStore,
        execution_records: FileExecutionRecordStore,
        ids: UuidIdGenerator,
        clock: ProductionClock,
        file_sync: PlatformFileSync,
        replacer: PlatformReplaceOperation,
        interval_seconds: float = UPLOAD_TEMP_RETENTION_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("cleanup interval must be positive")
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_result: CleanupRunResult | None = None
        self._last_failure_code: ErrorCode | None = None
        self._last_failure_type: str | None = None
        self.cleaner = StorageRetentionCleaner(
            layout,
            coordination_lock,
            repository,
            resource_store,
            execution_records,
            ids,
            RetentionScanner(layout, clock),
            QuarantineMover(layout, coordination_lock, file_sync, replacer),
            resource_store.stage_registry,
            resource_store.attachment_registry,
            is_interrupted=self._stop.is_set,
        )

    @property
    def started(self) -> bool:
        with self._lock:
            return self._thread is not None

    @property
    def thread_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_result(self) -> CleanupRunResult | None:
        with self._lock:
            return self._last_result

    @property
    def last_failure_code(self) -> ErrorCode | None:
        with self._lock:
            return self._last_failure_code

    @property
    def last_failure_type(self) -> str | None:
        with self._lock:
            return self._last_failure_type

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if self._stop.is_set():
                raise RuntimeError("cleanup service is already stopped")
            self._thread = threading.Thread(
                target=self._run,
                name="problem-locator-retention",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.cleaner.run_once()
            except ApplicationPortError as exc:
                log_event(
                    "retention.run.application_error",
                    level=logging.ERROR,
                    error_code=exc.error.code,
                    application_error=exc.error,
                    error=exc,
                )
                with self._lock:
                    self._last_result = None
                    self._last_failure_code = exc.error.code
                    self._last_failure_type = type(exc).__name__
            except Exception as exc:
                log_event(
                    "retention.run.unhandled_error",
                    level=logging.ERROR,
                    error=exc,
                )
                with self._lock:
                    self._last_result = None
                    self._last_failure_code = None
                    self._last_failure_type = type(exc).__name__
            else:
                log_event(
                    "retention.run.completed",
                    deleted_count=len(result.deleted),
                    quarantined_count=len(result.quarantined),
                    failed_deletion_count=len(result.failed_deletions),
                    skipped_count=len(result.skipped),
                    interrupted=result.interrupted,
                )
                with self._lock:
                    self._last_result = result
                    self._last_failure_code = None
                    self._last_failure_type = None
            if self._stop.wait(self._interval_seconds):
                return


@dataclass(slots=True, repr=False)
class ServiceComposition:
    """The unique production object graph and its managed lifecycle."""

    settings: Settings
    clock: ProductionClock
    ids: UuidIdGenerator
    notifier: ThreadStateChangeNotifier
    layout: StorageLayout
    instance_lock: FileInstanceLock
    coordination_lock: StorageCoordinationLock
    publication_guard: InProcessPublicationCommitGuard
    attachment_registry: AttachmentUploadRegistry
    upload_guard: InProcessAttachmentUploadGuard
    execution_records: FileExecutionRecordStore
    repository: JsonFileStateRepository
    resource_store: FileResourceStore
    file_sync: PlatformFileSync
    replacer: PlatformReplaceOperation
    logparse_asset: ResolvedAsset
    logparse_broker_factory: LogparseBrokerFactory
    asset_catalog: VersionedAssetCatalog
    dispatcher: LateBoundDispatcher
    application: ApplicationService
    runtime: DiagnosisRuntime
    scheduler: SchedulerService
    retention: RetentionService
    state_admin: ServiceStateAdmin
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _lifecycle_condition: threading.Condition = field(init=False, repr=False)
    _start_in_progress: bool = field(default=False, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)

    def __repr__(self) -> str:
        return (
            "ServiceComposition(started="
            f"{self.started!r}, closed={self.closed!r}, data_root=<configured>)"
        )

    @property
    def started(self) -> bool:
        with self._lifecycle_condition:
            return self._started

    @property
    def closed(self) -> bool:
        with self._lifecycle_condition:
            return self._closed

    def start(self) -> RecoveryResult:
        with self._lifecycle_condition:
            if self._closed or self._closing:
                raise RuntimeError("service composition is closing")
            if self._started:
                result = self.scheduler.recovery_result
                if result is None:  # pragma: no cover - guarded by this class
                    raise RuntimeError("started scheduler has no recovery result")
                return result
            if self._start_in_progress:
                self._lifecycle_condition.wait_for(
                    lambda: not self._start_in_progress
                )
                result = self.scheduler.recovery_result
                if not self._started or result is None:
                    raise RuntimeError("service startup did not complete")
                return result
            self._start_in_progress = True

        try:
            result = self.scheduler.start()
            if result.completed:
                self.retention.start()
        finally:
            with self._lifecycle_condition:
                self._start_in_progress = False
                self._lifecycle_condition.notify_all()
        with self._lifecycle_condition:
            if self._closing or self._closed:
                raise RuntimeError("service closed during startup")
            self._started = True
        return result

    def close(self, timeout_seconds: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._lifecycle_condition:
            if self._closed:
                return
            if self._closing:
                self._lifecycle_condition.wait_for(lambda: not self._closing)
                if self._closed:
                    return
            self._closing = True

        deadline = time.monotonic() + timeout_seconds
        scheduler_stopped = self.scheduler.shutdown(timeout_seconds)
        retention_stopped = self.retention.shutdown(
            max(0.0, deadline - time.monotonic())
        )
        with self._lifecycle_condition:
            if self._start_in_progress:
                self._lifecycle_condition.wait_for(
                    lambda: not self._start_in_progress,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            safe_to_release = (
                scheduler_stopped
                and retention_stopped
                and not self._start_in_progress
            )

        if not safe_to_release:
            with self._lifecycle_condition:
                self._closing = False
                self._lifecycle_condition.notify_all()
            raise RuntimeError(
                "managed service threads did not stop before the shutdown deadline"
            )

        self.instance_lock.release()
        with self._lifecycle_condition:
            self._closed = True
            self._closing = False
            self._lifecycle_condition.notify_all()


@dataclass(slots=True)
class _StartupFailureOwner:
    error: ApplicationError
    checks_passed: dict[str, bool]
    validation: ValidationReport
    instance_lock: FileInstanceLock | None = None
    _closed: bool = False

    def close(self, timeout_seconds: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
        del timeout_seconds
        if self._closed:
            return
        if self.instance_lock is not None:
            self.instance_lock.release()
        self._closed = True


class _FailedStateAdmin:
    def __init__(self, owner: _StartupFailureOwner) -> None:
        self._owner = owner

    def readiness(self) -> ReadinessReport:
        passed = dict(self._owner.checks_passed)
        if self._owner.instance_lock is not None:
            passed["INSTANCE_LOCK"] = self._owner.instance_lock.is_acquired()
        checks = [
            ReadinessCheck(
                name=name,
                passed=passed.get(name, False),
                message=(
                    None
                    if passed.get(name, False)
                    else "Startup validation did not complete."
                ),
            )
            for name in _READINESS_NAMES
        ]
        return ReadinessReport(
            ready=False,
            checks=checks,
            error=self._owner.error.model_copy(deep=True),
        )

    def validate_state(self) -> ValidationReport:
        return self._owner.validation.model_copy(deep=True)

    def export_state(self) -> bytes:
        code = self._owner.error.code
        if code not in {
            ErrorCode.INSTANCE_LOCKED,
            ErrorCode.STATE_CORRUPT,
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            ErrorCode.RESOURCE_NOT_FOUND,
            ErrorCode.RESOURCE_HASH_MISMATCH,
            ErrorCode.RESOURCE_SIZE_MISMATCH,
        }:
            code = ErrorCode.STATE_CORRUPT
        raise _port_error(code, "The stored state could not be exported safely.")


class _UnavailableApplication:
    """Fail-closed Ports used only while a startup error is observable."""

    def __init__(self, startup_error: ApplicationError) -> None:
        command_code = startup_error.code
        if command_code not in {
            ErrorCode.CONFIG_INVALID,
            ErrorCode.STATE_CORRUPT,
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        }:
            command_code = ErrorCode.STATE_CORRUPT
        query_code = startup_error.code
        if query_code not in {
            ErrorCode.STATE_CORRUPT,
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        }:
            query_code = ErrorCode.STATE_CORRUPT
        self._command_error = _application_error(
            command_code,
            "The service did not complete startup validation.",
        )
        self._query_error = _application_error(
            query_code,
            "The service state is unavailable.",
        )

    def execute(self, command: Any) -> Any:
        del command
        raise ApplicationPortError(self._command_error.model_copy(deep=True))

    def get_case(
        self,
        case_id: str,
        wait_for_job_id: str | None = None,
        wait_seconds: int = 0,
    ) -> Any:
        del case_id, wait_for_job_id, wait_seconds
        raise ApplicationPortError(self._query_error.model_copy(deep=True))

    def list_artifacts(self, case_id: str, include_internal: bool = False) -> Any:
        del case_id, include_internal
        raise ApplicationPortError(self._query_error.model_copy(deep=True))

    def open_artifact(self, case_id: str, artifact_id: str) -> Any:
        del case_id, artifact_id
        raise ApplicationPortError(self._query_error.model_copy(deep=True))


class _CompositionFailure(Exception):
    def __init__(self, owner: _StartupFailureOwner) -> None:
        super().__init__(owner.error.code.value)
        self.owner = owner


def _failure_owner(
    code: ErrorCode,
    message: str,
    *,
    checks_passed: dict[str, bool],
    validation: ValidationReport | None = None,
    instance_lock: FileInstanceLock | None = None,
) -> _StartupFailureOwner:
    error = _application_error(code, message)
    return _StartupFailureOwner(
        error=error,
        checks_passed=checks_passed,
        validation=validation or _invalid_report(error),
        instance_lock=instance_lock,
    )


def _assemble(
    settings: Settings,
    *,
    allow_test_skills: bool = False,
) -> ServiceComposition:
    if not isinstance(settings, Settings):
        raise TypeError("settings must be immutable Settings")
    if type(allow_test_skills) is not bool:
        raise TypeError("allow_test_skills must be a boolean")

    try:
        logparse_asset, broker_factory = build_logparse_runtime(
            settings.logparse_repo,
            settings.logparse_config_path,
            settings.logparse_python,
        )
        asset_catalog = VersionedAssetCatalog(
            skill_dir=settings.skill_dir,
            logparse_tool=logparse_asset,
            logparse_broker_factory=broker_factory,
            allow_test_skills=allow_test_skills,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _CompositionFailure(
            _failure_owner(
                ErrorCode.CONFIG_INVALID,
                "Configured runtime assets are invalid.",
                checks_passed={name: False for name in _READINESS_NAMES},
            )
        ) from exc

    try:
        layout = StorageLayout.at(settings.data_root)
        file_sync = PlatformFileSync()
        layout.initialize_v2_data_root(file_sync)
    except UnsupportedDataFormatError as exc:
        raise _CompositionFailure(
            _failure_owner(
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
                "The DATA_ROOT data format is unsupported; configure a fresh DATA_ROOT.",
                checks_passed={
                    "CONFIG": True,
                    "INSTANCE_LOCK": False,
                    "STATE": False,
                    "DATA_DIRECTORIES": False,
                    "RECOVERY": False,
                },
            )
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _CompositionFailure(
            _failure_owner(
                ErrorCode.STATE_CORRUPT,
                "The DATA_ROOT storage layout is invalid.",
                checks_passed={
                    "CONFIG": True,
                    "INSTANCE_LOCK": False,
                    "STATE": False,
                    "DATA_DIRECTORIES": False,
                    "RECOVERY": False,
                },
            )
        ) from exc

    instance_lock = FileInstanceLock(layout.instance_lock)
    try:
        instance_lock.acquire()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _CompositionFailure(
            _failure_owner(
                ErrorCode.INSTANCE_LOCKED,
                "The installation is already open by another process.",
                checks_passed={
                    "CONFIG": True,
                    "INSTANCE_LOCK": False,
                    "STATE": False,
                    "DATA_DIRECTORIES": True,
                    "RECOVERY": False,
                },
            )
        ) from exc

    clock = ProductionClock()
    ids = UuidIdGenerator()
    notifier = ThreadStateChangeNotifier()
    coordination_lock = StorageCoordinationLock()
    publication_guard = InProcessPublicationCommitGuard(coordination_lock)
    attachment_registry = AttachmentUploadRegistry()
    upload_guard = InProcessAttachmentUploadGuard(attachment_registry)
    replacer = PlatformReplaceOperation()

    try:
        execution_records = FileExecutionRecordStore(
            layout.data_root,
            coordination_lock,
            file_sync,
            replacer,
        )
        repository = JsonFileStateRepository(
            layout.data_root,
            coordination_lock,
            clock,
            ids,
            file_sync=file_sync,
            replacer=replacer,
            execution_record_store=execution_records,
        )
    except ApplicationPortError as exc:
        raise _CompositionFailure(
            _failure_owner(
                exc.error.code,
                exc.error.message,
                checks_passed={
                    "CONFIG": True,
                    "INSTANCE_LOCK": True,
                    "STATE": False,
                    "DATA_DIRECTORIES": True,
                    "RECOVERY": False,
                },
                validation=_invalid_report(exc.error),
                instance_lock=instance_lock,
            )
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        error = _application_error(
            ErrorCode.STATE_CORRUPT,
            "The stored state could not be opened safely.",
        )
        raise _CompositionFailure(
            _failure_owner(
                error.code,
                error.message,
                checks_passed={
                    "CONFIG": True,
                    "INSTANCE_LOCK": True,
                    "STATE": False,
                    "DATA_DIRECTORIES": True,
                    "RECOVERY": False,
                },
                validation=_invalid_report(error),
                instance_lock=instance_lock,
            )
        ) from exc

    try:
        resource_store = FileResourceStore(
            layout,
            coordination_lock,
            attachment_registry,
            ids,
            file_sync=file_sync,
            replacer=replacer,
        )
        dispatcher = LateBoundDispatcher()
        application = build_application_service(
            repository=repository,
            resource_store=resource_store,
            publication_guard=publication_guard,
            upload_guard=upload_guard,
            execution_records=execution_records,
            coordinator=DomainCoordinator(),
            projector=PureContextSnapshotProjector(),
            asset_catalog=asset_catalog,
            dispatcher=dispatcher,
            notifier=notifier,
            clock=clock,
            ids=ids,
        )
        runtime = DiagnosisRuntime(
            state_repository=repository,
            resource_store=resource_store,
            asset_catalog=asset_catalog,
            logparse_broker_factory=broker_factory,
            execution_records=execution_records,
            clock=clock,
            id_generator=ids,
            workspace_manager=WorkspaceManager(layout.data_root),
            backend=AgentBackend(settings.claude_command),
        )
        scheduler = SchedulerService(
            repository,
            execution_records,
            application,
            runtime,
            ids,
        )
        dispatcher.bind(scheduler)
        retention = RetentionService(
            layout=layout,
            coordination_lock=coordination_lock,
            repository=repository,
            resource_store=resource_store,
            execution_records=execution_records,
            ids=ids,
            clock=clock,
            file_sync=file_sync,
            replacer=replacer,
        )
        state_admin = ServiceStateAdmin(
            layout=layout,
            instance_lock=instance_lock,
            coordination_lock=coordination_lock,
            repository=repository,
            scheduler=scheduler,
        )
        return ServiceComposition(
            settings=settings,
            clock=clock,
            ids=ids,
            notifier=notifier,
            layout=layout,
            instance_lock=instance_lock,
            coordination_lock=coordination_lock,
            publication_guard=publication_guard,
            attachment_registry=attachment_registry,
            upload_guard=upload_guard,
            execution_records=execution_records,
            repository=repository,
            resource_store=resource_store,
            file_sync=file_sync,
            replacer=replacer,
            logparse_asset=logparse_asset,
            logparse_broker_factory=broker_factory,
            asset_catalog=asset_catalog,
            dispatcher=dispatcher,
            application=application,
            runtime=runtime,
            scheduler=scheduler,
            retention=retention,
            state_admin=state_admin,
        )
    except Exception as exc:
        raise _CompositionFailure(
            _failure_owner(
                ErrorCode.CONFIG_INVALID,
                "The service composition is invalid.",
                checks_passed={
                    "CONFIG": False,
                    "INSTANCE_LOCK": True,
                    "STATE": True,
                    "DATA_DIRECTORIES": True,
                    "RECOVERY": False,
                },
                instance_lock=instance_lock,
            )
        ) from exc


def build_service(settings: Settings) -> ServiceComposition:
    """Build the production graph, raising only a typed safe startup error."""

    try:
        return _assemble(settings)
    except _CompositionFailure as exc:
        exc.owner.close()
        raise ApplicationPortError(exc.owner.error.model_copy(deep=True)) from None


def _install_lifespan(app: Any, owner: ServiceComposition | _StartupFailureOwner) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(asgi_app: Any):
        try:
            if isinstance(owner, ServiceComposition):
                log_event("service.starting")
                try:
                    recovery = await asyncio.to_thread(owner.start)
                except Exception as exc:
                    log_event(
                        "service.startup_failed",
                        level=logging.ERROR,
                        error=exc,
                    )
                    raise
                log_event(
                    "service.started",
                    recovery_completed=recovery.completed,
                    runtime_epoch=recovery.runtime_epoch,
                    replayed_job_ids=recovery.replayed_job_ids,
                    interrupted_job_ids=recovery.interrupted_job_ids,
                    pending_job_ids=recovery.pending_job_ids,
                    recovery_failure_type=recovery.failure_type,
                    recovery_failure_code=recovery.failure_code,
                )
            async with original_lifespan(asgi_app):
                yield
        finally:
            log_event("service.shutdown_started")
            try:
                await asyncio.to_thread(owner.close)
            except Exception as exc:
                log_event(
                    "service.shutdown_failed",
                    level=logging.ERROR,
                    error=exc,
                )
                raise
            else:
                log_event("service.shutdown_completed")

    app.router.lifespan_context = lifespan


def _create_app(settings: Settings, *, allow_test_skills: bool) -> Any:
    """Internal ASGI composition seam with an explicit Skill policy."""

    try:
        composition = _assemble(settings, allow_test_skills=allow_test_skills)
    except _CompositionFailure as exc:
        log_event(
            "service.assembly_failed",
            level=logging.ERROR,
            application_error=exc.owner.error,
            error_code=exc.owner.error.code,
            error=exc,
        )
        owner = exc.owner
        unavailable = _UnavailableApplication(owner.error)
        app = create_asgi_app(
            InterfaceDependencies(
                command_port=unavailable,
                query_port=unavailable,
                state_admin=_FailedStateAdmin(owner),
                public_base_url=settings.public_base_url,
            )
        )
        app.state.problem_locator_composition = None
        app.state.problem_locator_owner = owner
        app.state.problem_locator_startup_error = owner.error.model_copy(deep=True)
        _install_lifespan(app, owner)
        return app

    log_event("service.assembly_completed")

    app = create_asgi_app(
        InterfaceDependencies(
            command_port=composition.application,
            query_port=composition.application,
            state_admin=composition.state_admin,
            public_base_url=settings.public_base_url,
        )
    )
    app.state.problem_locator_composition = composition
    app.state.problem_locator_owner = composition
    app.state.problem_locator_startup_error = None
    _install_lifespan(app, composition)
    return app


def create_app(settings: Settings) -> Any:
    """Create the production ASGI application with TEST_ONLY Skills forbidden."""

    return _create_app(settings, allow_test_skills=False)


def _create_test_app(settings: Settings) -> Any:
    """Create the E2E harness application with TEST_ONLY Skills enabled."""

    return _create_app(settings, allow_test_skills=True)


def create_state_admin(data_root: Path) -> StandaloneStateAdmin:
    """Create the lazy offline admin facade; filesystem access starts on call."""

    return StandaloneStateAdmin(data_root)


def run_replay_job(request: ReplayRequest, settings: Settings) -> ReplayResult:
    """Build and run the isolated replay graph without service lifecycle start."""

    return execute_replay_job(
        request,
        settings,
        service_factory=build_service,
    )


def _server_runner(app: Any, host: str, port: int, workers: int) -> None:
    try:
        run_uvicorn(app, host, port, workers)
    finally:
        owner = getattr(getattr(app, "state", None), "problem_locator_owner", None)
        if owner is not None:
            owner.close()


def cli_hooks() -> CliHooks:
    """Return fresh explicit hooks without mutating S06 module globals."""

    return CliHooks(
        state_admin_factory=create_state_admin,
        app_factory=create_app,
        server_runner=_server_runner,
        replay_runner=run_replay_job,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
) -> int:
    """Run the S06 CLI through the explicit production composition hooks."""

    return cli_main(
        argv,
        hooks=cli_hooks(),
        stdout=stdout,
        stderr=stderr,
    )


__all__ = [
    "LateBoundDispatcher",
    "ProductionClock",
    "RetentionService",
    "ServiceComposition",
    "ServiceStateAdmin",
    "StandaloneStateAdmin",
    "ThreadStateChangeNotifier",
    "UuidIdGenerator",
    "build_service",
    "cli_hooks",
    "create_app",
    "create_state_admin",
    "main",
    "run_replay_job",
]
