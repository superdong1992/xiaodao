"""Frozen r2 ``ResourceStore`` backed by the S02 filesystem layout.

The adapter is deliberately thin around the content-only staging and formal
resource primitives.  This module owns the public DTO boundary, the sole
``ApplicationPortError`` channel, attachment-upload capability checks, and a
small path-claim registry shared with the retention cleaner.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    AttachmentStagedRef,
    AttachmentUploadLease,
    BinaryStream,
    CaseResourceUsage,
    ERROR_SPECS,
    ErrorCode,
    IdGenerator,
    MaterializedPath,
    OpaqueId,
    PlannedResourceTarget,
    ResourceKind,
    ResourceRef,
    ResourceType,
    Sha256,
    StagedResourceRef,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.limits import (
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
)

from .atomic import FileSync, Replacer, require_ordinary_file, require_real_directory
from .coordination import AttachmentUploadRegistry, StorageCoordinationLock
from .layout import StorageLayout
from .paths import (
    attachment_stage_path,
    ensure_no_symlink_ancestors,
    formal_storage_key,
    parse_storage_key,
    proposal_stage_path,
)
from .platform import PlatformFileSync, PlatformReplaceOperation
from .resource_files import (
    FormalResourcePublisher,
    FormalResourceReader,
    calculate_case_usage,
)
from .staging import StagedObjectWriter
from .streams import hash_file
from .tree import verify_tree


_StageClaimPurpose = Literal["stage", "cleanup"]
_SHA256_ADAPTER = TypeAdapter(Sha256)


def _port_error(
    code: ErrorCode,
    message: str,
    *,
    details: Sequence[ApplicationErrorDetail] = (),
) -> ApplicationPortError:
    """Construct the one frozen modeled-failure channel."""

    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=list(details),
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _absolute_lexical(path: Path) -> Path:
    """Normalize a claim key without resolving potentially hostile links."""

    return Path(os.path.abspath(path))


class StagePathLease:
    """Opaque storage-internal capability for one staging directory.

    A cleaner receives a ``cleanup`` lease only when no writer owns the exact
    path.  A writer receives a ``stage`` lease before creating any node.  The
    two purposes are mutually exclusive, so a markerless in-progress write
    cannot be selected and deleted between a cleaner observation and removal.
    """

    __slots__ = (
        "_registry",
        "_directory",
        "_purpose",
        "_owner_thread_id",
        "_released",
    )

    def __init__(
        self,
        registry: StagePathRegistry,
        directory: Path,
        purpose: _StageClaimPurpose,
    ) -> None:
        self._registry = registry
        self._directory = directory
        self._purpose = purpose
        self._owner_thread_id = threading.get_ident()
        self._released = False

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def purpose(self) -> _StageClaimPurpose:
        return self._purpose

    def is_released(self) -> bool:
        with self._registry._lock:
            return self._released

    def release(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("stage path lease cannot cross threads")
        self._registry._release(self)

    def __enter__(self) -> Self:
        if self.is_released():
            raise RuntimeError("stage path lease is released")
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("stage path lease cannot cross threads")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class StagePathRegistry:
    """Mutually exclusive writer/cleaner claims for exact staging paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[Path, StagePathLease] = {}

    def acquire_stage(self, directory: Path) -> StagePathLease:
        key = _absolute_lexical(directory)
        with self._lock:
            if key in self._claims:
                raise RuntimeError("staging directory already has an active claim")
            lease = StagePathLease(self, key, "stage")
            self._claims[key] = lease
            return lease

    def try_acquire_cleanup(self, directory: Path) -> StagePathLease | None:
        """Claim a candidate for deletion, or return ``None`` if it is active."""

        key = _absolute_lexical(directory)
        with self._lock:
            if key in self._claims:
                return None
            lease = StagePathLease(self, key, "cleanup")
            self._claims[key] = lease
            return lease

    def active_stage_directories(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(
                sorted(
                    path
                    for path, lease in self._claims.items()
                    if lease.purpose == "stage"
                )
            )

    def is_stage_active(self, directory: Path) -> bool:
        key = _absolute_lexical(directory)
        with self._lock:
            lease = self._claims.get(key)
            return lease is not None and lease.purpose == "stage"

    def _release(self, lease: StagePathLease) -> None:
        with self._lock:
            if lease._released:
                return
            if self._claims.get(lease.directory) is not lease:
                raise RuntimeError("stage path lease is not active")
            lease._released = True
            self._claims.pop(lease.directory)


class FileResourceStore:
    """Filesystem implementation of the frozen r2 ``ResourceStore`` Port."""

    def __init__(
        self,
        layout: StorageLayout,
        coordination_lock: StorageCoordinationLock,
        attachment_registry: AttachmentUploadRegistry,
        id_generator: IdGenerator,
        *,
        file_sync: FileSync | None = None,
        replacer: Replacer | None = None,
        stage_registry: StagePathRegistry | None = None,
    ) -> None:
        if not isinstance(layout, StorageLayout):
            raise TypeError("layout must be a StorageLayout")
        self.layout = layout
        self.coordination_lock = coordination_lock
        self.attachment_registry = attachment_registry
        self.stage_registry = stage_registry or StagePathRegistry()
        self._file_sync = file_sync or PlatformFileSync()
        self._replacer = replacer or PlatformReplaceOperation()
        self._id_generator = id_generator
        self._writer = StagedObjectWriter(
            layout,
            self._file_sync,
            self._replacer,
            id_generator,
        )
        self._publisher = FormalResourcePublisher(
            layout,
            coordination_lock,
            self._file_sync,
            self._replacer,
        )
        self._reader = FormalResourceReader(
            layout,
            self._file_sync,
            self._replacer,
            temp_token_factory=lambda: self._id_generator.new("materialization"),
        )

    def active_stage_directories(self) -> tuple[Path, ...]:
        """Storage-internal cleaner observation; not part of the public Port."""

        return self.stage_registry.active_stage_directories()

    @staticmethod
    def _proposal_directory(
        layout: StorageLayout,
        staged_ref: StagedResourceRef,
    ) -> Path:
        return proposal_stage_path(
            layout.data_root,
            staged_ref.owner_job_id,
            staged_ref.proposal_key,
        )

    @staticmethod
    def _stage_directory(
        layout: StorageLayout,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> Path:
        if isinstance(staged_ref, StagedResourceRef):
            return FileResourceStore._proposal_directory(layout, staged_ref)
        if isinstance(staged_ref, AttachmentStagedRef):
            return attachment_stage_path(layout.data_root, staged_ref.attachment_id)
        raise TypeError("staged_ref must be a frozen staged-resource DTO")

    @staticmethod
    def _content_path(
        directory: Path,
        resource_kind: ResourceKind,
    ) -> Path:
        return directory / (
            "tree" if resource_kind is ResourceKind.DIRECTORY else "payload"
        )

    @staticmethod
    def _expected_marker_bytes(
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> bytes:
        return canonical_json_bytes(staged_ref)

    def _read_and_match_marker(
        self,
        directory: Path,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> None:
        marker = self._writer.read_marker(directory)
        if marker is None:
            raise FileNotFoundError("staged completion marker is missing")
        model_type = (
            StagedResourceRef
            if isinstance(staged_ref, StagedResourceRef)
            else AttachmentStagedRef
        )
        parsed = parse_canonical_json_bytes(marker, model_type)
        if parsed != staged_ref or marker != self._expected_marker_bytes(staged_ref):
            raise ValueError("staged completion marker does not match its receipt")

    def _validate_staged_content(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> Path:
        directory = self._stage_directory(self.layout, staged_ref)
        self._read_and_match_marker(directory, staged_ref)
        content = self._content_path(directory, staged_ref.resource_kind)
        if isinstance(staged_ref, StagedResourceRef) and staged_ref.tree_manifest is not None:
            verify_tree(
                content,
                expected_manifest=staged_ref.tree_manifest,
                expected_size=staged_ref.size,
                expected_sha256=staged_ref.sha256,
            )
        else:
            observed = hash_file(content)
            if observed.size != staged_ref.size:
                raise ValueError("staged resource size has drifted")
            if observed.sha256 != staged_ref.sha256:
                raise ValueError("staged resource hash has drifted")
        return content

    def _validate_stage_for_publish(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> Path:
        """Validate one complete stage and project failures to publish codes."""

        directory = self._stage_directory(self.layout, staged_ref)
        try:
            ensure_no_symlink_ancestors(self.layout.temporary, directory)
            require_real_directory(directory)
        except FileNotFoundError:
            raise _port_error(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The staged resource does not exist or is incomplete.",
            ) from None
        except (OSError, TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The staged resource path is invalid.",
            ) from None
        try:
            return self._validate_staged_content(staged_ref)
        except FileNotFoundError:
            raise _port_error(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The staged resource does not exist or is incomplete.",
            ) from None
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The staged resource receipt or bytes have drifted.",
            ) from None
        except OSError:
            raise _port_error(
                ErrorCode.RESOURCE_PUBLISH_FAILED,
                "The staged resource could not be revalidated.",
            ) from None

    @staticmethod
    def _stage_value_error_code(error: ValueError) -> ErrorCode:
        text = str(error).lower()
        if "byte limit" in text or "exceeds" in text:
            return ErrorCode.RESOURCE_LIMIT_EXCEEDED
        if "size" in text:
            return ErrorCode.RESOURCE_SIZE_MISMATCH
        if "hash" in text or "digest" in text or "manifest" in text:
            return ErrorCode.RESOURCE_HASH_MISMATCH
        if any(
            token in text
            for token in (
                "path",
                "directory",
                "link",
                "ordinary",
                "canonical",
                "tree root",
                "identifier",
            )
        ):
            return ErrorCode.PATH_VIOLATION
        return ErrorCode.RESOURCE_STAGE_FAILED

    @staticmethod
    def _validate_expected_metadata(
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> None:
        if expected_size is not None and (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise _port_error(
                ErrorCode.RESOURCE_SIZE_MISMATCH,
                "The expected resource size is invalid.",
            )
        if expected_sha256 is not None:
            try:
                _SHA256_ADAPTER.validate_python(expected_sha256)
            except (TypeError, ValueError, ValidationError):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The expected resource digest is invalid.",
                ) from None

    def stage_file(
        self,
        owner_job_id: OpaqueId,
        proposal_key: str,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedResourceRef:
        self._validate_expected_metadata(expected_size, expected_sha256)
        try:
            directory = proposal_stage_path(
                self.layout.data_root,
                owner_job_id,
                proposal_key,
            )
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The proposal staging identity is invalid.",
            ) from None
        try:
            with self.stage_registry.acquire_stage(directory):
                observation = self._writer.stage_file_content(
                    directory,
                    stream,
                    byte_limit=MAX_CASE_RESOURCE_BYTES,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
                staged_ref = StagedResourceRef(
                    staging_id=self._id_generator.new("resource_staging"),
                    owner_job_id=owner_job_id,
                    proposal_key=proposal_key,
                    resource_kind=ResourceKind.FILE,
                    size=observation.size,
                    sha256=observation.sha256,
                    tree_manifest=None,
                )
                self._writer.publish_marker(
                    directory,
                    self._expected_marker_bytes(staged_ref),
                )
                return staged_ref
        except ApplicationPortError:
            raise
        except ValueError as error:
            code = self._stage_value_error_code(error)
            raise _port_error(code, "The proposed file could not be staged.") from None
        except (FileExistsError, OSError, TypeError, ValidationError, RuntimeError):
            raise _port_error(
                ErrorCode.RESOURCE_STAGE_FAILED,
                "The proposed file could not be staged.",
            ) from None

    def stage_tree(
        self,
        owner_job_id: OpaqueId,
        proposal_key: str,
        root: Path,
        expected_manifest_hash: str | None = None,
    ) -> StagedResourceRef:
        if expected_manifest_hash is not None:
            try:
                _SHA256_ADAPTER.validate_python(expected_manifest_hash)
            except (TypeError, ValueError, ValidationError):
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The expected tree manifest digest is invalid.",
                ) from None
        try:
            directory = proposal_stage_path(
                self.layout.data_root,
                owner_job_id,
                proposal_key,
            )
            source_root = Path(root)
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The proposal tree path or identity is invalid.",
            ) from None
        try:
            with self.stage_registry.acquire_stage(directory):
                inspection = self._writer.stage_tree_content(
                    directory,
                    source_root,
                    byte_limit=MAX_CASE_RESOURCE_BYTES,
                    expected_manifest_hash=expected_manifest_hash,
                )
                staged_ref = StagedResourceRef(
                    staging_id=self._id_generator.new("resource_staging"),
                    owner_job_id=owner_job_id,
                    proposal_key=proposal_key,
                    resource_kind=ResourceKind.DIRECTORY,
                    size=inspection.size,
                    sha256=inspection.sha256,
                    tree_manifest=inspection.manifest,
                )
                self._writer.publish_marker(
                    directory,
                    self._expected_marker_bytes(staged_ref),
                )
                return staged_ref
        except ApplicationPortError:
            raise
        except ValueError as error:
            code = self._stage_value_error_code(error)
            raise _port_error(code, "The proposed tree could not be staged.") from None
        except FileNotFoundError:
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The proposed tree root does not exist.",
            ) from None
        except (FileExistsError, OSError, TypeError, ValidationError, RuntimeError):
            raise _port_error(
                ErrorCode.RESOURCE_STAGE_FAILED,
                "The proposed tree could not be staged.",
            ) from None

    def stage_attachment(
        self,
        attachment_id: OpaqueId,
        upload_lease: AttachmentUploadLease,
        stream: BinaryStream,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> AttachmentStagedRef:
        self._validate_expected_metadata(expected_size, expected_sha256)
        try:
            self.attachment_registry.validate_lease(attachment_id, upload_lease)
        except (TypeError, ValueError, RuntimeError):
            raise _port_error(
                ErrorCode.UPLOAD_INCOMPLETE,
                "The attachment upload lease is no longer valid.",
            ) from None

        try:
            directory = attachment_stage_path(self.layout.data_root, attachment_id)
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.VALIDATION_ERROR,
                "The attachment identifier is invalid.",
            ) from None

        try:
            with self.stage_registry.acquire_stage(directory):
                observation = self._writer.stage_file_content(
                    directory,
                    stream,
                    byte_limit=MAX_ATTACHMENT_BYTES,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
                staged_ref = AttachmentStagedRef(
                    attachment_id=attachment_id,
                    resource_kind=ResourceKind.FILE,
                    size=observation.size,
                    sha256=observation.sha256,
                )
                self._writer.publish_marker(
                    directory,
                    self._expected_marker_bytes(staged_ref),
                )
                return staged_ref
        except ValueError as error:
            text = str(error).lower()
            if "byte limit" in text or "exceeds" in text:
                code = ErrorCode.RESOURCE_LIMIT_EXCEEDED
            elif "size" in text:
                code = ErrorCode.RESOURCE_SIZE_MISMATCH
            elif "hash" in text or "digest" in text:
                code = ErrorCode.RESOURCE_HASH_MISMATCH
            else:
                code = ErrorCode.UPLOAD_INCOMPLETE
            raise _port_error(code, "The attachment upload is incomplete.") from None
        except (FileExistsError, OSError, TypeError, ValidationError, RuntimeError):
            raise _port_error(
                ErrorCode.UPLOAD_INCOMPLETE,
                "The attachment upload is incomplete.",
            ) from None

    def validate_staged(self, staged_ref: StagedResourceRef) -> None:
        if not isinstance(staged_ref, StagedResourceRef):
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The staged resource receipt has an invalid shape.",
            )
        try:
            self._validate_staged_content(staged_ref)
        except FileNotFoundError:
            raise _port_error(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The staged resource does not exist or is incomplete.",
            ) from None
        except (OSError, TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The staged resource receipt or bytes have drifted.",
            ) from None

    def plan_target(
        self,
        case_id: OpaqueId,
        resource_type: ResourceType,
        resource_id: OpaqueId,
        resource_kind: ResourceKind,
        size: int,
        sha256: Sha256,
    ) -> PlannedResourceTarget:
        try:
            if not isinstance(resource_type, ResourceType):
                raise TypeError("resource_type must be ResourceType")
            if not isinstance(resource_kind, ResourceKind):
                raise TypeError("resource_kind must be ResourceKind")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("size must be a non-negative integer")
            if (
                resource_type is ResourceType.ATTACHMENT
                and resource_kind is not ResourceKind.FILE
            ):
                raise ValueError("Attachment targets must be files")
            limit = (
                MAX_ATTACHMENT_BYTES
                if resource_type is ResourceType.ATTACHMENT
                else MAX_CASE_RESOURCE_BYTES
            )
            if size > limit:
                raise ValueError("target size exceeds its resource limit")
            category = {
                ResourceType.ATTACHMENT: "attachments",
                ResourceType.EVIDENCE: "evidence",
                ResourceType.ARTIFACT: "artifacts",
            }[resource_type]
            final_storage_key = formal_storage_key(
                case_id,
                category,
                resource_id,
                resource_kind,
            )
            return PlannedResourceTarget(
                case_id=case_id,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_kind=resource_kind,
                size=size,
                sha256=sha256,
                final_storage_key=final_storage_key,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.VALIDATION_ERROR,
                "The requested formal resource target is invalid.",
            ) from None

    @staticmethod
    def _validate_publish_identity(
        staged_ref: StagedResourceRef | AttachmentStagedRef,
        final_storage_key: str,
    ) -> None:
        address = parse_storage_key(final_storage_key)
        if address.resource_kind is not staged_ref.resource_kind:
            raise ValueError("formal target kind differs from staged receipt")
        if isinstance(staged_ref, AttachmentStagedRef):
            if (
                address.category != "attachments"
                or address.resource_id != staged_ref.attachment_id
            ):
                raise ValueError("attachment receipt target identity does not match")
        elif address.category == "attachments":
            raise ValueError("Agent proposals cannot publish Attachment targets")

    def publish(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
        final_storage_key: str,
    ) -> ResourceRef:
        if not isinstance(staged_ref, (StagedResourceRef, AttachmentStagedRef)):
            raise _port_error(
                ErrorCode.RESOURCE_HASH_MISMATCH,
                "The staged resource receipt has an invalid shape.",
            )
        if isinstance(staged_ref, AttachmentStagedRef):
            try:
                self.attachment_registry.require_current_thread_lease(
                    staged_ref.attachment_id
                )
            except (TypeError, ValueError, RuntimeError):
                raise RuntimeError(
                    "Attachment publication requires its active upload lease"
                ) from None
        try:
            self._validate_publish_identity(staged_ref, final_storage_key)
        except (TypeError, ValueError, ValidationError):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The formal resource target is outside the allowed resource root.",
            ) from None

        directory = self._stage_directory(self.layout, staged_ref)
        content_path = self._content_path(directory, staged_ref.resource_kind)
        final_path = self.layout.data_root / Path(final_storage_key)
        try:
            final_exists = final_path.exists() or final_path.is_symlink()
            if not final_exists:
                content_path = self._validate_stage_for_publish(staged_ref)
            else:
                try:
                    marker = self._writer.read_marker(directory)
                except FileNotFoundError:
                    marker = None
                if marker is not None:
                    try:
                        self._read_and_match_marker(directory, staged_ref)
                    except (TypeError, ValueError, ValidationError):
                        raise _port_error(
                            ErrorCode.RESOURCE_HASH_MISMATCH,
                            "The staged resource receipt has drifted.",
                        ) from None
                    if content_path.exists() or content_path.is_symlink():
                        content_path = self._validate_stage_for_publish(staged_ref)
            observed = self._publisher.publish(
                content_path,
                final_storage_key,
                expected_kind=staged_ref.resource_kind,
                expected_size=staged_ref.size,
                expected_sha256=staged_ref.sha256,
                expected_tree_manifest=(
                    staged_ref.tree_manifest
                    if isinstance(staged_ref, StagedResourceRef)
                    else None
                ),
            )
            return ResourceRef(
                resource_kind=observed.resource_kind,
                storage_key=observed.storage_key,
                size=observed.size,
                sha256=observed.sha256,
            )
        except ApplicationPortError:
            raise
        except FileNotFoundError:
            raise _port_error(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The staged resource does not exist or is incomplete.",
            ) from None
        except ValueError as error:
            text = str(error).lower()
            if any(
                token in text
                for token in (
                    "hash",
                    "size",
                    "manifest",
                    "content",
                    "receipt",
                    "canonical",
                    "ordinary file",
                    "real directory",
                    "resource kind",
                )
            ):
                code = ErrorCode.RESOURCE_HASH_MISMATCH
            elif any(token in text for token in ("path", "root", "link", "target kind")):
                code = ErrorCode.PATH_VIOLATION
            else:
                code = ErrorCode.RESOURCE_PUBLISH_FAILED
            raise _port_error(code, "The formal resource could not be published.") from None
        except (OSError, TypeError, ValidationError):
            raise _port_error(
                ErrorCode.RESOURCE_PUBLISH_FAILED,
                "The formal resource could not be published.",
            ) from None

    def validate_case_capacity(
        self,
        case_id: OpaqueId,
        planned_final_targets: Sequence[PlannedResourceTarget],
    ) -> CaseResourceUsage:
        if not self.coordination_lock.publication_held_by_current_thread():
            raise RuntimeError("Case capacity validation requires a publication lease")
        try:
            targets = tuple(planned_final_targets)
            if any(not isinstance(target, PlannedResourceTarget) for target in targets):
                raise ValueError("planned targets must be frozen DTOs")
            keys = [target.final_storage_key for target in targets]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError("planned targets must be uniquely sorted")
            if any(target.case_id != case_id for target in targets):
                raise ValueError("planned target belongs to another Case")
            tuples = [
                (
                    target.final_storage_key,
                    target.resource_kind,
                    target.size,
                    target.sha256,
                )
                for target in targets
            ]
            with self.coordination_lock:
                current, new, total = calculate_case_usage(
                    self.layout,
                    case_id,
                    tuples,
                )
            return CaseResourceUsage(
                current_bytes=current,
                new_bytes=new,
                total_bytes=total,
                limit_bytes=MAX_CASE_RESOURCE_BYTES,
            )
        except OverflowError:
            raise _port_error(
                ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "The Case resource capacity is exceeded.",
            ) from None
        except ValueError as error:
            text = str(error).lower()
            if "different content" in text or "conflict" in text:
                code = ErrorCode.RESOURCE_HASH_MISMATCH
            else:
                code = ErrorCode.PATH_VIOLATION
            raise _port_error(code, "The Case capacity plan is invalid.") from None
        except (OSError, TypeError, ValidationError):
            raise _port_error(
                ErrorCode.PATH_VIOLATION,
                "The Case resource hierarchy is invalid.",
            ) from None

    @staticmethod
    def _read_error_code(error: BaseException) -> ErrorCode:
        if isinstance(error, FileNotFoundError):
            return ErrorCode.RESOURCE_NOT_FOUND
        text = str(error).lower()
        if "size" in text:
            return ErrorCode.RESOURCE_SIZE_MISMATCH
        if any(
            token in text
            for token in ("hash", "manifest", "bytes", "read-only", "changed")
        ):
            return ErrorCode.RESOURCE_HASH_MISMATCH
        if any(
            token in text
            for token in ("path", "destination", "root", "link", "directory resources")
        ):
            return ErrorCode.PATH_VIOLATION
        return ErrorCode.RESOURCE_NOT_FOUND

    def open_read(self, resource_ref: ResourceRef) -> BinaryStream:
        try:
            return self._reader.open_file(resource_ref)
        except (FileNotFoundError, OSError, TypeError, ValueError, ValidationError) as error:
            code = self._read_error_code(error)
            raise _port_error(code, "The formal resource could not be opened.") from None

    def materialize_read_only(
        self,
        resource_ref: ResourceRef,
        destination: Path,
    ) -> MaterializedPath:
        try:
            path = self._reader.materialize(resource_ref, Path(destination))
            return MaterializedPath(path=str(path), read_only=True)
        except (FileNotFoundError, OSError, TypeError, ValueError, ValidationError) as error:
            code = self._read_error_code(error)
            raise _port_error(code, "The formal resource could not be materialized.") from None

    def discard(
        self,
        staged_ref: StagedResourceRef | AttachmentStagedRef,
    ) -> None:
        if not isinstance(staged_ref, (StagedResourceRef, AttachmentStagedRef)):
            raise TypeError("staged_ref must be a frozen staged-resource DTO")
        if isinstance(staged_ref, AttachmentStagedRef):
            self.attachment_registry.require_current_thread_lease(
                staged_ref.attachment_id
            )
        directory = self._stage_directory(self.layout, staged_ref)
        cleanup_lease = self.stage_registry.try_acquire_cleanup(directory)
        if cleanup_lease is None:
            raise RuntimeError("refusing to discard an active staging directory")
        with cleanup_lease:
            # Claim first, then re-observe the exact node.  A cooperative stage
            # writer can therefore never appear between our validation and
            # removal, including for a markerless interrupted stage.
            try:
                ensure_no_symlink_ancestors(self.layout.temporary, directory)
                require_real_directory(directory)
            except FileNotFoundError:
                # A previous attempt may have removed the directory and then
                # failed its parent fsync.  Reapply that durability boundary.
                self._file_sync.sync_directory(directory.parent)
                return
            marker = self._writer.read_marker(directory)
            if marker is not None and marker != self._expected_marker_bytes(staged_ref):
                raise RuntimeError("refusing to discard a different completed stage")

            for current, directory_names, filenames in os.walk(
                directory,
                topdown=True,
                followlinks=False,
            ):
                current_path = Path(current)
                require_real_directory(current_path)
                for name in directory_names:
                    require_real_directory(current_path / name)
                for name in filenames:
                    require_ordinary_file(current_path / name)
            shutil.rmtree(directory)
            self._file_sync.sync_directory(directory.parent)


__all__ = [
    "FileResourceStore",
    "StagePathLease",
    "StagePathRegistry",
]
