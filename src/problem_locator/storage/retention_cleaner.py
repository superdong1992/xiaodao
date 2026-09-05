"""Metadata-only retention with indexed references and a publication barrier."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    ERROR_SPECS,
    ErrorCode,
    ExecutionRecordStore,
    IdGenerator,
    JobStatus,
    OpaqueId,
    ResourceStore,
    StateFile,
    StateRepository,
)

from .atomic import is_reparse_point, require_real_directory
from .coordination import AttachmentUploadRegistry
from .layout import StorageLayout
from .quarantine import QuarantineMover
from .resource_store import StagePathRegistry
from .retention import RetentionScanner, _RetentionCandidate


_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }
)


class _CoordinationLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class CleanupRunResult:
    """Observable result of one pass; failed deletion paths remain retryable."""

    quarantined: tuple[Path, ...]
    deleted: tuple[Path, ...]
    failed_deletions: tuple[Path, ...]
    skipped: tuple[Path, ...]
    interrupted: bool


@dataclass(frozen=True, slots=True)
class _CandidateObservation:
    candidate_metadata: tuple[int, int, int, int, int]
    anchor_path: Path
    anchor_metadata: tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ProtectionSnapshot:
    state_resource_keys: frozenset[str]
    state_job_ids: frozenset[str]
    terminal_job_ids: frozenset[str]
    processed_job_ids: frozenset[str]
    outbox_stage_paths: frozenset[Path]
    outbox_resource_keys: frozenset[str]
    outbox_job_ids: frozenset[str]


def _execution_record_error() -> ApplicationPortError:
    code = ErrorCode.EXECUTION_RECORD_FAILED
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message="The published Outcome execution record is invalid.",
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _metadata_identity(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.lstat()
    if is_reparse_point(metadata) or not (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError("retention candidate must be a real file or directory")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


class StorageRetentionCleaner:
    """Run one fail-closed, two-phase S02 retention cycle.

    ``stage_registry`` and ``attachment_registry`` are the exact registries
    injected into ``FileResourceStore`` and the upload guard.  The latter
    covers the short post-stage publish/commit span in which no stage writer
    claim is active.
    """

    def __init__(
        self,
        layout: StorageLayout,
        coordination_lock: _CoordinationLock,
        state_repository: StateRepository,
        resource_store: ResourceStore,
        execution_record_store: ExecutionRecordStore,
        id_generator: IdGenerator,
        retention_scanner: RetentionScanner,
        quarantine_mover: QuarantineMover,
        stage_registry: StagePathRegistry,
        attachment_registry: AttachmentUploadRegistry,
        *,
        is_interrupted: Callable[[], bool] | None = None,
        on_delete_failure: Callable[[Path, BaseException], None] | None = None,
    ) -> None:
        self._layout = layout
        self._coordination_lock = coordination_lock
        self._state_repository = state_repository
        self._resource_store = resource_store
        self._execution_record_store = execution_record_store
        self._id_generator = id_generator
        self._retention_scanner = retention_scanner
        self._quarantine_mover = quarantine_mover
        self._stage_registry = stage_registry
        self._attachment_registry = attachment_registry
        self._is_interrupted = is_interrupted or (lambda: False)
        self._on_delete_failure = on_delete_failure or (
            lambda _path, _error: None
        )

    @staticmethod
    def _formal_resource_keys(state: StateFile) -> set[str]:
        keys: set[str] = set()
        for aggregate in state.cases.values():
            for attachment in aggregate.attachments.values():
                if attachment.storage_key is not None:
                    keys.add(attachment.storage_key)
            for evidence in aggregate.evidence.values():
                if evidence.resource_ref is not None:
                    keys.add(evidence.resource_ref.storage_key)
            for artifact in aggregate.artifacts.values():
                keys.add(artifact.storage_key)
        return keys


    @staticmethod
    def _anchor_path(candidate: _RetentionCandidate) -> Path:
        if candidate.kind in {"UPLOAD", "PROPOSAL"}:
            marker = candidate.path / "staged.json"
            if marker.exists() or marker.is_symlink():
                return marker
        if candidate.kind == "FORMAL_RESOURCE":
            return candidate.path.parent
        return candidate.path

    def _observe_candidate(
        self,
        candidate: _RetentionCandidate,
    ) -> _CandidateObservation:
        anchor = self._anchor_path(candidate)
        return _CandidateObservation(
            candidate_metadata=_metadata_identity(candidate.path),
            anchor_path=anchor,
            anchor_metadata=_metadata_identity(anchor),
        )

    def _candidate_is_unchanged(
        self,
        candidate: _RetentionCandidate,
        observation: _CandidateObservation,
    ) -> bool:
        try:
            if self._anchor_path(candidate) != observation.anchor_path:
                return False
            return (
                _metadata_identity(candidate.path)
                == observation.candidate_metadata
                and _metadata_identity(observation.anchor_path)
                == observation.anchor_metadata
            )
        except FileNotFoundError:
            return False

    def _eligible(
        self,
        candidate: _RetentionCandidate,
        observation: _CandidateObservation,
    ) -> bool:
        if not self._candidate_is_unchanged(candidate, observation):
            return False
        path = Path(os.path.abspath(candidate.path))

        if candidate.kind == "STATE_TEMP":
            return True
        if candidate.kind == "UPLOAD":
            attachment_id = _OPAQUE_ID_ADAPTER.validate_python(path.name)
            return (
                attachment_id
                not in self._attachment_registry.active_attachment_ids()
            )
        if candidate.kind == "PROPOSAL":
            owner_job_id = _OPAQUE_ID_ADAPTER.validate_python(path.parent.name)
            return not self._state_repository.retention_in_use('active_job', owner_job_id)
        if candidate.kind == "WORKSPACE":
            job_id = _OPAQUE_ID_ADAPTER.validate_python(path.name)
            return not self._state_repository.retention_in_use('active_job', job_id)
        if candidate.kind == "FORMAL_RESOURCE":
            storage_key = path.relative_to(self._layout.data_root).as_posix()
            return self._state_repository.retention_in_use('resource', storage_key) is False
        if candidate.kind == "JOB":
            job_id = _OPAQUE_ID_ADAPTER.validate_python(path.name)
            return not self._state_repository.retention_in_use('job', job_id)
        raise AssertionError(f"unknown retention candidate kind: {candidate.kind}")

    def _delete_quarantine(
        self,
        paths: tuple[Path, ...],
    ) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        deleted: list[Path] = []
        failed: list[Path] = []
        for path in paths:
            if self._is_interrupted():
                break
            try:
                self._quarantine_mover.delete(path)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                failed.append(path)
                try:
                    self._on_delete_failure(path, error)
                except BaseException:
                    # Cleanup logging must never convert a retryable recursive
                    # deletion into a business-state mutation or lost receipt.
                    pass
            else:
                deleted.append(path)
        return tuple(deleted), tuple(failed)

    def run_once(self) -> CleanupRunResult:
        """Recheck each reference under the publication barrier before quarantine."""

        with self._coordination_lock:
            if not self._state_repository.health().valid:
                raise _execution_record_error()

        candidates = self._retention_scanner.discover()
        observations = {
            candidate.path: self._observe_candidate(candidate)
            for candidate in candidates
        }
        # Validate retry receipts before introducing any new quarantine entry.
        self._quarantine_mover.discover()

        cleanup_id: OpaqueId | None = None
        quarantined: list[Path] = []
        skipped: list[Path] = []
        interrupted = False

        for candidate in candidates:
            if self._is_interrupted():
                interrupted = True
                break
            stage_lease = None
            if candidate.kind in {"UPLOAD", "PROPOSAL"}:
                stage_lease = self._stage_registry.try_acquire_cleanup(candidate.path)
                if stage_lease is None:
                    skipped.append(candidate.path)
                    continue
            try:
                if cleanup_id is None:
                    cleanup_id = self._id_generator.new("cleanup")
                isolated = self._quarantine_mover.move_if(
                    cleanup_id,
                    candidate.path,
                    lambda candidate=candidate: self._eligible(
                        candidate,
                        observations[candidate.path],
                    ),
                )
            finally:
                if stage_lease is not None:
                    stage_lease.release()
            if isolated is None:
                skipped.append(candidate.path)
            else:
                quarantined.append(isolated)

        if interrupted:
            return CleanupRunResult(
                quarantined=tuple(quarantined),
                deleted=(),
                failed_deletions=(),
                skipped=tuple(skipped),
                interrupted=True,
            )

        # The second health scan prevents an error discovered while moving a
        # later candidate from being followed by recursive quarantine deletion.
        with self._coordination_lock:
            if not self._state_repository.health().valid:
                raise _execution_record_error()
            pending_deletions = self._quarantine_mover.discover()

        deleted, failed = self._delete_quarantine(pending_deletions)
        return CleanupRunResult(
            quarantined=tuple(quarantined),
            deleted=deleted,
            failed_deletions=failed,
            skipped=tuple(skipped),
            interrupted=self._is_interrupted(),
        )


__all__ = ["CleanupRunResult", "StorageRetentionCleaner"]
