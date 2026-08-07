"""Deterministic, disposable per-Job workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.enums import (
    AttachmentStatus,
    ErrorCode,
    ExecutionStage,
    ResourceKind,
)
from problem_locator.contracts.limits import MAX_USER_TEXT_UTF8_BYTES
from problem_locator.contracts.models import (
    Artifact,
    Attachment,
    CaseAggregate,
    Evidence,
    Job,
    JobOutcome,
    LogparseParseClaim,
    MaterializedPath,
    ResourceRef,
    TreeManifest,
    TreeManifestEntry,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceEvidenceInput,
    WorkspaceInputManifest,
    WorkspacePreviousOutcomeInput,
    derive_attachment_filename_suffix,
    validate_workspace_manifest_for_job,
    workspace_attachment_relative_path,
)
from problem_locator.contracts.ports import ResourceStore
from problem_locator.contracts.serialization import (
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .failures import RuntimeExecutionError, runtime_failure
from .outcome_finalizer import FINALIZATION_MARKER_NAME


_READ_CHUNK_BYTES = 1024 * 1024
# The claim has three NonEmptyText values.  A one-byte JSON control character
# can expand to a six-byte ``\u00xx`` escape, so this remains a complete bound
# for every contract-valid claim rather than an accidental semantic limit.
_MAX_PARSE_CLAIM_BYTES = (18 * MAX_USER_TEXT_UTF8_BYTES) + 4096


class _UnsafeWorkspaceError(Exception):
    """An fd-anchored workspace invariant could not be established."""


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """The immutable input view handed to Context Builder and Backend."""

    root: Path
    root_device: int
    root_inode: int
    inputs_device: int
    inputs_inode: int
    runtime_device: int
    runtime_inode: int
    tool_state_device: int
    tool_state_inode: int
    output_device: int
    output_inode: int
    manifest: WorkspaceInputManifest
    manifest_bytes: bytes
    attachments: tuple[Attachment, ...]
    evidence: tuple[Evidence, ...]
    artifacts: tuple[Artifact, ...]
    previous_outcomes: tuple[JobOutcome, ...]

    @property
    def context_path(self) -> Path:
        return self.root / "runtime" / "context.txt"

    @property
    def outcome_path(self) -> Path:
        return self.root / "output" / "job_outcome.json"

    @property
    def tool_state_root(self) -> Path:
        return self.root / "runtime" / "tool-state"


def _safe_dir_fd_operations_supported() -> bool:
    """Return whether this platform can keep untrusted traversal fd-anchored."""

    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and hasattr(os, "O_CLOEXEC")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.listdir in os.supports_fd
    )


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("workspace path is not a directory")
    return _identity(metadata)


def _open_workspace_root(workspace: PreparedWorkspace) -> int:
    if not _safe_dir_fd_operations_supported():
        raise _UnsafeWorkspaceError("safe directory descriptors are unavailable")
    try:
        descriptor = os.open(workspace.root, _directory_flags())
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace root could not be opened") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _identity(metadata)
            != (workspace.root_device, workspace.root_inode)
        ):
            raise _UnsafeWorkspaceError("workspace root identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _assert_workspace_root_path(
    workspace: PreparedWorkspace,
    descriptor: int,
) -> None:
    descriptor_metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(descriptor_metadata.st_mode)
        or _identity(descriptor_metadata)
        != (workspace.root_device, workspace.root_inode)
    ):
        raise _UnsafeWorkspaceError("workspace root descriptor changed")
    try:
        path_descriptor = os.open(workspace.root, _directory_flags())
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace root path changed") from exc
    try:
        path_metadata = os.fstat(path_descriptor)
        if (
            not stat.S_ISDIR(path_metadata.st_mode)
            or _identity(path_metadata)
            != (workspace.root_device, workspace.root_inode)
        ):
            raise _UnsafeWorkspaceError("workspace root path identity changed")
    finally:
        os.close(path_descriptor)


def _open_expected_directory(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace directory could not be opened") from exc
    try:
        metadata = os.fstat(descriptor)
        named_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or not stat.S_ISDIR(named_metadata.st_mode)
            or _identity(metadata) != expected_identity
            or _identity(named_metadata) != expected_identity
        ):
            raise _UnsafeWorkspaceError("workspace directory identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _assert_expected_directory(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        named_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace directory identity changed") from exc
    if (
        not stat.S_ISDIR(descriptor_metadata.st_mode)
        or not stat.S_ISDIR(named_metadata.st_mode)
        or _identity(descriptor_metadata) != expected_identity
        or _identity(named_metadata) != expected_identity
    ):
        raise _UnsafeWorkspaceError("workspace directory identity changed")


def _listed_names(descriptor: int) -> list[str]:
    try:
        names = sorted(os.listdir(descriptor))
        for name in names:
            name.encode("utf-8", errors="strict")
    except (OSError, UnicodeEncodeError) as exc:
        raise _UnsafeWorkspaceError("workspace directory could not be listed") from exc
    return names


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _measure_untrusted_directory(
    descriptor: int,
    *,
    workspace_device: int,
) -> int:
    names = _listed_names(descriptor)
    total = 0
    for name in names:
        try:
            named_metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace node changed") from exc
        if named_metadata.st_dev != workspace_device:
            raise _UnsafeWorkspaceError("workspace node crossed a filesystem")
        if stat.S_ISDIR(named_metadata.st_mode):
            try:
                child_descriptor = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise _UnsafeWorkspaceError("workspace directory is unsafe") from exc
            try:
                opened_metadata = os.fstat(child_descriptor)
                if (
                    not stat.S_ISDIR(opened_metadata.st_mode)
                    or _identity(opened_metadata) != _identity(named_metadata)
                    or opened_metadata.st_dev != workspace_device
                ):
                    raise _UnsafeWorkspaceError("workspace directory changed")
                total += _measure_untrusted_directory(
                    child_descriptor,
                    workspace_device=workspace_device,
                )
                final_metadata = os.fstat(child_descriptor)
                final_named_metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    _identity(final_metadata) != _identity(opened_metadata)
                    or _identity(final_named_metadata) != _identity(opened_metadata)
                    or not stat.S_ISDIR(final_named_metadata.st_mode)
                ):
                    raise _UnsafeWorkspaceError("workspace directory changed")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(named_metadata.st_mode) or named_metadata.st_nlink != 1:
            raise _UnsafeWorkspaceError("workspace contains a non-ordinary file")
        try:
            file_descriptor = os.open(name, _file_flags(), dir_fd=descriptor)
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace file is unsafe") from exc
        try:
            opened_metadata = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or opened_metadata.st_dev != workspace_device
                or _identity(opened_metadata) != _identity(named_metadata)
            ):
                raise _UnsafeWorkspaceError("workspace file changed")
            final_metadata = os.fstat(file_descriptor)
            if _metadata_fingerprint(final_metadata) != _metadata_fingerprint(
                opened_metadata
            ):
                raise _UnsafeWorkspaceError("workspace file changed")
            total += final_metadata.st_size
        finally:
            os.close(file_descriptor)
        try:
            final_named_metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace file changed") from exc
        if _metadata_fingerprint(final_named_metadata) != _metadata_fingerprint(
            named_metadata
        ):
            raise _UnsafeWorkspaceError("workspace file changed")
    if _listed_names(descriptor) != names:
        raise _UnsafeWorkspaceError("workspace directory changed during inspection")
    return total


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _fallback_file_flags(*, write_new: bool = False) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if write_new else os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
        flags |= getattr(os, name, 0)
    return flags


def _fallback_root(workspace: PreparedWorkspace) -> Path:
    """Validate and resolve the frozen root without relying on ``dir_fd``."""

    try:
        metadata = workspace.root.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or _identity(metadata)
            != (workspace.root_device, workspace.root_inode)
        ):
            raise _UnsafeWorkspaceError("workspace root identity changed")
        resolved = workspace.root.resolve(strict=True)
        resolved_metadata = resolved.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise _UnsafeWorkspaceError("workspace root could not be resolved") from exc
    if (
        _is_link_or_reparse(resolved_metadata)
        or not stat.S_ISDIR(resolved_metadata.st_mode)
        or _identity(resolved_metadata)
        != (workspace.root_device, workspace.root_inode)
    ):
        raise _UnsafeWorkspaceError("workspace root resolution changed")
    return resolved


def _fallback_expected_directory(
    workspace: PreparedWorkspace,
    parts: tuple[str, ...],
    identities: tuple[tuple[int, int], ...],
) -> tuple[Path, os.stat_result]:
    if len(parts) != len(identities):
        raise ValueError("each workspace directory requires a frozen identity")
    root_resolved = _fallback_root(workspace)
    path = workspace.root
    metadata: os.stat_result | None = None
    for part, expected_identity in zip(parts, identities, strict=True):
        path = path / part
        try:
            metadata = path.stat(follow_symlinks=False)
            resolved = path.resolve(strict=True)
            resolved.relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _UnsafeWorkspaceError("workspace directory escaped its root") from exc
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or _identity(metadata) != expected_identity
        ):
            raise _UnsafeWorkspaceError("workspace directory identity changed")
    assert metadata is not None
    return path, metadata


def _fallback_names(path: Path) -> list[str]:
    try:
        names = sorted(os.listdir(path))
        for name in names:
            name.encode("utf-8", errors="strict")
    except (OSError, UnicodeEncodeError) as exc:
        raise _UnsafeWorkspaceError("workspace directory could not be listed") from exc
    return names


def _fallback_assert_beneath(path: Path, root_resolved: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _UnsafeWorkspaceError("workspace node escaped its root") from exc


def _fallback_measure_directory(
    workspace: PreparedWorkspace,
    path: Path,
    *,
    root_resolved: Path,
) -> int:
    try:
        directory_metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace directory changed") from exc
    if (
        _is_link_or_reparse(directory_metadata)
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_dev != workspace.root_device
    ):
        raise _UnsafeWorkspaceError("workspace directory is unsafe")
    _fallback_assert_beneath(path, root_resolved)
    names = _fallback_names(path)
    total = 0
    for name in names:
        candidate = path / name
        try:
            named_metadata = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace node changed") from exc
        if (
            _is_link_or_reparse(named_metadata)
            or named_metadata.st_dev != workspace.root_device
        ):
            raise _UnsafeWorkspaceError("workspace node is unsafe")
        _fallback_assert_beneath(candidate, root_resolved)
        if stat.S_ISDIR(named_metadata.st_mode):
            total += _fallback_measure_directory(
                workspace,
                candidate,
                root_resolved=root_resolved,
            )
            try:
                final_named_metadata = candidate.stat(follow_symlinks=False)
            except OSError as exc:
                raise _UnsafeWorkspaceError("workspace directory changed") from exc
            if (
                _is_link_or_reparse(final_named_metadata)
                or _identity(final_named_metadata) != _identity(named_metadata)
                or not stat.S_ISDIR(final_named_metadata.st_mode)
            ):
                raise _UnsafeWorkspaceError("workspace directory changed")
            continue
        if not stat.S_ISREG(named_metadata.st_mode) or named_metadata.st_nlink != 1:
            raise _UnsafeWorkspaceError("workspace contains a non-ordinary file")
        try:
            descriptor = os.open(candidate, _fallback_file_flags())
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace file could not be opened") from exc
        try:
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or opened_metadata.st_dev != workspace.root_device
                or _identity(opened_metadata) != _identity(named_metadata)
            ):
                raise _UnsafeWorkspaceError("workspace file changed")
            final_metadata = os.fstat(descriptor)
            if _metadata_fingerprint(final_metadata) != _metadata_fingerprint(
                opened_metadata
            ):
                raise _UnsafeWorkspaceError("workspace file changed")
            total += final_metadata.st_size
        finally:
            os.close(descriptor)
        try:
            final_named_metadata = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise _UnsafeWorkspaceError("workspace file changed") from exc
        if (
            _is_link_or_reparse(final_named_metadata)
            or _metadata_fingerprint(final_named_metadata)
            != _metadata_fingerprint(named_metadata)
        ):
            raise _UnsafeWorkspaceError("workspace file changed")
        _fallback_assert_beneath(candidate, root_resolved)
    try:
        final_directory_metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _UnsafeWorkspaceError("workspace directory changed") from exc
    if (
        _is_link_or_reparse(final_directory_metadata)
        or _metadata_fingerprint(final_directory_metadata)
        != _metadata_fingerprint(directory_metadata)
        or _fallback_names(path) != names
    ):
        raise _UnsafeWorkspaceError("workspace directory changed during inspection")
    _fallback_assert_beneath(path, root_resolved)
    return total


def _fallback_write_context(workspace: PreparedWorkspace, body: str) -> None:
    runtime_path, runtime_metadata = _fallback_expected_directory(
        workspace,
        ("runtime",),
        ((workspace.runtime_device, workspace.runtime_inode),),
    )
    runtime_names = _fallback_names(runtime_path)
    if "context.txt" in runtime_names:
        raise _UnsafeWorkspaceError("runtime context already exists")
    data = body.encode("utf-8")
    context_path = runtime_path / "context.txt"
    descriptor = -1
    try:
        descriptor = os.open(context_path, _fallback_file_flags(write_new=True), 0o600)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or opened_metadata.st_dev != workspace.root_device
        ):
            raise _UnsafeWorkspaceError("runtime context file is unsafe")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("runtime context write made no progress")
            offset += written
        os.fsync(descriptor)
        final_metadata = os.fstat(descriptor)
        named_metadata = context_path.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(named_metadata)
            or not stat.S_ISREG(final_metadata.st_mode)
            or final_metadata.st_nlink != 1
            or final_metadata.st_size != len(data)
            or _identity(named_metadata) != _identity(final_metadata)
            or named_metadata.st_nlink != 1
            or _metadata_fingerprint(named_metadata)
            != _metadata_fingerprint(final_metadata)
        ):
            raise _UnsafeWorkspaceError("runtime context file changed")
        root_resolved = _fallback_root(workspace)
        _fallback_assert_beneath(context_path, root_resolved)
        final_runtime_path, final_runtime_metadata = _fallback_expected_directory(
            workspace,
            ("runtime",),
            ((workspace.runtime_device, workspace.runtime_inode),),
        )
        if (
            final_runtime_path != runtime_path
            or _identity(final_runtime_metadata) != _identity(runtime_metadata)
            or _fallback_names(final_runtime_path)
            != sorted([*runtime_names, "context.txt"])
        ):
            raise _UnsafeWorkspaceError("runtime directory changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fallback_temporary_output_bytes(workspace: PreparedWorkspace) -> int:
    root_resolved = _fallback_root(workspace)
    if set(_fallback_names(workspace.root)) != {"inputs", "runtime", "output"}:
        raise _UnsafeWorkspaceError("workspace root shape changed")
    _fallback_expected_directory(
        workspace,
        ("inputs",),
        ((workspace.inputs_device, workspace.inputs_inode),),
    )
    runtime_path, _ = _fallback_expected_directory(
        workspace,
        ("runtime",),
        ((workspace.runtime_device, workspace.runtime_inode),),
    )
    output_path, _ = _fallback_expected_directory(
        workspace,
        ("output",),
        ((workspace.output_device, workspace.output_inode),),
    )
    total = _fallback_measure_directory(
        workspace,
        runtime_path,
        root_resolved=root_resolved,
    )
    total += _fallback_measure_directory(
        workspace,
        output_path,
        root_resolved=root_resolved,
    )
    if set(_fallback_names(workspace.root)) != {"inputs", "runtime", "output"}:
        raise _UnsafeWorkspaceError("workspace root shape changed")
    _fallback_root(workspace)
    return total


def _fallback_read_claim(workspace: PreparedWorkspace) -> LogparseParseClaim | None:
    tool_state_path, tool_state_metadata = _fallback_expected_directory(
        workspace,
        ("runtime", "tool-state"),
        (
            (workspace.runtime_device, workspace.runtime_inode),
            (workspace.tool_state_device, workspace.tool_state_inode),
        ),
    )
    names = _fallback_names(tool_state_path)
    if not names or names == [FINALIZATION_MARKER_NAME]:
        final_tool_state_path, final_tool_state_metadata = (
            _fallback_expected_directory(
                workspace,
                ("runtime", "tool-state"),
                (
                    (workspace.runtime_device, workspace.runtime_inode),
                    (workspace.tool_state_device, workspace.tool_state_inode),
                ),
            )
        )
        if (
            final_tool_state_path != tool_state_path
            or _identity(final_tool_state_metadata) != _identity(tool_state_metadata)
            or _fallback_names(final_tool_state_path) != names
        ):
            raise _UnsafeWorkspaceError("tool state changed during inspection")
        _fallback_root(workspace)
        return None
    if set(names) not in (
        {"logparse-parse.claim"},
        {"logparse-parse.claim", FINALIZATION_MARKER_NAME},
    ):
        raise _UnsafeWorkspaceError("tool state contains an unexpected node")
    claim_path = tool_state_path / "logparse-parse.claim"
    try:
        named_metadata = claim_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _UnsafeWorkspaceError("parse claim changed") from exc
    if (
        _is_link_or_reparse(named_metadata)
        or not stat.S_ISREG(named_metadata.st_mode)
        or named_metadata.st_nlink != 1
        or named_metadata.st_dev != workspace.root_device
        or named_metadata.st_size > _MAX_PARSE_CLAIM_BYTES
    ):
        raise _UnsafeWorkspaceError("parse claim is not a bounded ordinary file")
    root_resolved = _fallback_root(workspace)
    _fallback_assert_beneath(claim_path, root_resolved)
    descriptor = -1
    try:
        descriptor = os.open(claim_path, _fallback_file_flags())
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or opened_metadata.st_dev != workspace.root_device
            or _identity(opened_metadata) != _identity(named_metadata)
        ):
            raise _UnsafeWorkspaceError("parse claim changed")
        data = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(
                    _READ_CHUNK_BYTES,
                    (_MAX_PARSE_CLAIM_BYTES + 1) - len(data),
                ),
            )
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_PARSE_CLAIM_BYTES:
                raise _UnsafeWorkspaceError("parse claim is too large")
        final_metadata = os.fstat(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        final_named_metadata = claim_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _UnsafeWorkspaceError("parse claim changed") from exc
    final_tool_state_path, final_tool_state_metadata = _fallback_expected_directory(
        workspace,
        ("runtime", "tool-state"),
        (
            (workspace.runtime_device, workspace.runtime_inode),
            (workspace.tool_state_device, workspace.tool_state_inode),
        ),
    )
    if (
        _is_link_or_reparse(final_named_metadata)
        or _metadata_fingerprint(final_metadata)
        != _metadata_fingerprint(opened_metadata)
        or _metadata_fingerprint(final_named_metadata)
        != _metadata_fingerprint(named_metadata)
        or len(data) != final_metadata.st_size
        or final_tool_state_path != tool_state_path
        or _identity(final_tool_state_metadata) != _identity(tool_state_metadata)
        or _fallback_names(tool_state_path) != names
    ):
        raise _UnsafeWorkspaceError("parse claim changed during inspection")
    _fallback_assert_beneath(claim_path, _fallback_root(workspace))
    return parse_canonical_json_bytes(bytes(data), model_type=LogparseParseClaim)


def _safe_destination(root: Path, relative_path: str) -> Path:
    """Resolve one fixed relative path without permitting an escape."""

    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace path validation failed.",
        )
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace path validation failed.",
        ) from exc
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def inspect_file(
    path: Path,
    *,
    allow_hardlinks: bool = False,
) -> tuple[int, str]:
    """Return verified ordinary-file size/hash without following links."""

    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed workspace resource is unavailable.",
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (not allow_hardlinks and metadata.st_nlink != 1)
    ):
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace input must be an ordinary unlinked file.",
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_READ_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace input could not be read safely.",
            retryable=True,
        ) from exc
    return size, digest.hexdigest()


def inspect_tree(root: Path) -> tuple[int, str, TreeManifest]:
    """Build the frozen TreeManifest for an ordinary, link-free directory."""

    try:
        root_metadata = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed workspace resource is unavailable.",
        ) from exc
    if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace directory input is not a safe directory.",
        )
    entries: list[TreeManifestEntry] = []
    try:
        candidates = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace directory could not be enumerated safely.",
            retryable=True,
        ) from exc
    for candidate in candidates:
        relative = candidate.relative_to(root).as_posix()
        try:
            relative.encode("utf-8", errors="strict")
            metadata = candidate.stat(follow_symlinks=False)
        except (OSError, UnicodeEncodeError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory contains an invalid node.",
            ) from exc
        if candidate.is_symlink():
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory links are forbidden.",
            )
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory contains a non-ordinary file.",
            )
        size, sha256 = inspect_file(candidate)
        entries.append(TreeManifestEntry(path=relative, size=size, sha256=sha256))
    manifest = TreeManifest(version=1, entries=entries)
    size = sum(entry.size for entry in entries)
    return size, bytes_sha256(canonical_json_bytes(manifest)), manifest


def _verify_materialized(
    resource_store: ResourceStore,
    resource_ref: ResourceRef,
    destination: Path,
) -> None:
    try:
        materialized = resource_store.materialize_read_only(resource_ref, destination)
    except ApplicationPortError as exc:
        error = exc.error
        messages = {
            ErrorCode.RESOURCE_NOT_FOUND: "A fixed resource is unavailable.",
            ErrorCode.RESOURCE_SIZE_MISMATCH: (
                "A fixed resource size does not match its metadata."
            ),
            ErrorCode.RESOURCE_HASH_MISMATCH: (
                "A fixed resource hash does not match its metadata."
            ),
            ErrorCode.PATH_VIOLATION: "A fixed resource path is unsafe.",
        }
        code = error.code
        if code not in messages:
            code = ErrorCode.WORKSPACE_PREPARE_FAILED
            message = "A fixed resource could not be materialized."
            retryable = True
        else:
            message = messages[code]
            retryable = False
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=code,
            message=message,
            retryable=retryable,
            details=error.details,
        ) from None
    except Exception as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="A fixed resource could not be materialized.",
            retryable=True,
        ) from exc
    if not isinstance(materialized, MaterializedPath) or not materialized.read_only:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="ResourceStore returned an invalid materialization receipt.",
            retryable=True,
        )
    try:
        actual_path = Path(materialized.path).resolve(strict=True)
        expected_path = destination.resolve(strict=True)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A materialized resource is missing.",
        ) from exc
    if actual_path != expected_path:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="ResourceStore materialized outside the requested path.",
        )
    if resource_ref.resource_kind is ResourceKind.FILE:
        # S02 may use a hard link for trusted immutable input materialization.
        # Agent-created outputs are inspected through a separate strict path
        # that continues to require a single-link ordinary file.
        actual_size, actual_sha256 = inspect_file(
            destination,
            allow_hardlinks=True,
        )
    else:
        actual_size, actual_sha256, _ = inspect_tree(destination)
    if actual_size != resource_ref.size:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_SIZE_MISMATCH,
            message="A fixed resource size does not match its metadata.",
        )
    if actual_sha256 != resource_ref.sha256:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_HASH_MISMATCH,
            message="A fixed resource hash does not match its metadata.",
        )


def _attachment_ref(attachment: Attachment) -> ResourceRef:
    if (
        attachment.status is not AttachmentStatus.READY
        or attachment.size is None
        or attachment.sha256 is None
        or attachment.storage_key is None
    ):
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed Attachment is not READY.",
        )
    return ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=attachment.storage_key,
        size=attachment.size,
        sha256=attachment.sha256,
    )


def _artifact_ref(artifact: Artifact) -> ResourceRef:
    return ResourceRef(
        resource_kind=artifact.resource_kind,
        storage_key=artifact.storage_key,
        size=artifact.size,
        sha256=artifact.sha256,
    )


def _set_inputs_read_only(inputs_root: Path) -> None:
    try:
        for path in sorted(inputs_root.rglob("*"), reverse=True):
            if path.is_symlink():
                raise ValueError("links are forbidden")
            if path.is_dir():
                path.chmod(0o555)
            else:
                path.chmod(0o444)
        inputs_root.chmod(0o555)
    except (OSError, ValueError) as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace inputs could not be made read-only.",
            retryable=True,
        ) from exc


class WorkspaceManager:
    """Create and verify the fixed workspace tree for exactly one Job."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)

    def prepare(
        self,
        job: Job,
        aggregate: CaseAggregate,
        resource_store: ResourceStore,
    ) -> PreparedWorkspace:
        if aggregate.case.case_id != job.case_id or aggregate.jobs.get(job.job_id) != job:
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="The executing Job does not match its stored immutable record.",
            )
        attachments = self._resolve_ordered(
            job.attachment_refs, aggregate.attachments, "Attachment"
        )
        evidence = self._resolve_ordered(job.evidence_refs, aggregate.evidence, "Evidence")
        artifacts = self._resolve_ordered(job.artifact_refs, aggregate.artifacts, "Artifact")
        outcomes = self._resolve_ordered(
            job.previous_outcome_refs, aggregate.outcomes, "previous Outcome"
        )
        if any(item.case_id != job.case_id for item in attachments):
            raise self._invalid_fixed_binding("Attachment")
        if any(item.case_id != job.case_id for item in evidence):
            raise self._invalid_fixed_binding("Evidence")
        if any(item.case_id != job.case_id for item in artifacts):
            raise self._invalid_fixed_binding("Artifact")
        if any(item.case_id != job.case_id for item in outcomes):
            raise self._invalid_fixed_binding("previous Outcome")

        root = self._data_root / "tmp" / "workspaces" / job.job_id
        try:
            root.mkdir(parents=True, exist_ok=False)
            (root / "inputs").mkdir()
            (root / "runtime" / "tool-state").mkdir(parents=True)
            (root / "output" / "proposals").mkdir(parents=True)
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Job Workspace could not be created safely.",
                retryable=True,
            ) from exc

        entries: list[
            WorkspaceAttachmentInput
            | WorkspaceEvidenceInput
            | WorkspaceArtifactInput
            | WorkspacePreviousOutcomeInput
        ] = []
        for attachment in attachments:
            reference = _attachment_ref(attachment)
            filename_suffix = derive_attachment_filename_suffix(
                attachment.name,
                attachment.content_type,
            )
            relative = workspace_attachment_relative_path(
                attachment.attachment_id,
                filename_suffix,
            )
            destination = _safe_destination(root, relative)
            _verify_materialized(resource_store, reference, destination)
            entries.append(
                WorkspaceAttachmentInput(
                    input_kind="ATTACHMENT",
                    resource_id=attachment.attachment_id,
                    relative_path=relative,
                    resource_kind=ResourceKind.FILE,
                    size=reference.size,
                    sha256=reference.sha256,
                    content_type=attachment.content_type,
                    filename_suffix=filename_suffix,
                )
            )
        for item in evidence:
            relative: str | None = None
            resource_kind: ResourceKind | None = None
            size: int | None = None
            sha256: str | None = None
            if item.resource_ref is not None:
                resource_kind = item.resource_ref.resource_kind
                leaf = "payload" if resource_kind is ResourceKind.FILE else "tree"
                relative = f"inputs/evidence/{item.evidence_id}/{leaf}"
                _verify_materialized(
                    resource_store,
                    item.resource_ref,
                    _safe_destination(root, relative),
                )
                size = item.resource_ref.size
                sha256 = item.resource_ref.sha256
            entries.append(
                WorkspaceEvidenceInput(
                    input_kind="EVIDENCE",
                    resource_id=item.evidence_id,
                    relative_path=relative,
                    resource_kind=resource_kind,
                    size=size,
                    sha256=sha256,
                    source_type=item.source_type,
                    source_ref=item.source_ref,
                    locator=item.locator,
                    summary=item.summary,
                    content_hash=item.content_hash,
                )
            )
        for artifact in artifacts:
            reference = _artifact_ref(artifact)
            leaf = "payload" if artifact.resource_kind is ResourceKind.FILE else "tree"
            relative = f"inputs/artifacts/{artifact.artifact_id}/{leaf}"
            _verify_materialized(
                resource_store,
                reference,
                _safe_destination(root, relative),
            )
            entries.append(
                WorkspaceArtifactInput(
                    input_kind="ARTIFACT",
                    resource_id=artifact.artifact_id,
                    relative_path=relative,
                    resource_kind=artifact.resource_kind,
                    size=artifact.size,
                    sha256=artifact.sha256,
                    artifact_kind=artifact.kind,
                    name=artifact.name,
                    content_type=artifact.content_type,
                    metadata=artifact.metadata,
                )
            )
        for outcome in outcomes:
            data = canonical_json_bytes(outcome)
            relative = f"inputs/outcomes/{outcome.outcome_id}/job_outcome.json"
            path = _safe_destination(root, relative)
            try:
                _atomic_write(path, data)
            except OSError as exc:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                    message="A previous Outcome could not be materialized.",
                    retryable=True,
                ) from exc
            entries.append(
                WorkspacePreviousOutcomeInput(
                    input_kind="PREVIOUS_OUTCOME",
                    resource_id=outcome.outcome_id,
                    relative_path=relative,
                    resource_kind=ResourceKind.FILE,
                    size=len(data),
                    sha256=bytes_sha256(data),
                    source_job_id=outcome.job_id,
                    result_type=outcome.result_type,
                )
            )

        manifest = WorkspaceInputManifest(
            schema_version=1,
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            logparse_tool_ref=job.logparse_tool_ref,
            logparse_product=job.logparse_product,
            entries=entries,
        )
        validate_workspace_manifest_for_job(manifest, job)
        manifest_bytes = canonical_json_bytes(manifest)
        try:
            _atomic_write(root / "inputs" / "manifest.json", manifest_bytes)
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Workspace manifest could not be published.",
                retryable=True,
            ) from exc
        _set_inputs_read_only(root / "inputs")
        try:
            root_device, root_inode = _directory_identity(root)
            inputs_device, inputs_inode = _directory_identity(root / "inputs")
            runtime_device, runtime_inode = _directory_identity(root / "runtime")
            tool_state_device, tool_state_inode = _directory_identity(
                root / "runtime" / "tool-state"
            )
            output_device, output_inode = _directory_identity(root / "output")
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Workspace identity could not be frozen safely.",
                retryable=True,
            ) from exc
        return PreparedWorkspace(
            root=root,
            root_device=root_device,
            root_inode=root_inode,
            inputs_device=inputs_device,
            inputs_inode=inputs_inode,
            runtime_device=runtime_device,
            runtime_inode=runtime_inode,
            tool_state_device=tool_state_device,
            tool_state_inode=tool_state_inode,
            output_device=output_device,
            output_inode=output_inode,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            attachments=tuple(attachments),
            evidence=tuple(evidence),
            artifacts=tuple(artifacts),
            previous_outcomes=tuple(outcomes),
        )

    @staticmethod
    def _resolve_ordered(
        references: list[str], mapping: dict[str, object], label: str
    ) -> list[object]:
        resolved: list[object] = []
        for reference in references:
            value = mapping.get(reference)
            if value is None:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"A fixed {label} is unavailable.",
                )
            resolved.append(value)
        return resolved

    @staticmethod
    def _invalid_fixed_binding(label: str) -> RuntimeExecutionError:
        return runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_INVALID,
            message=f"A fixed {label} does not belong to the executing Job Case.",
        )

    @staticmethod
    def write_context(workspace: PreparedWorkspace, body: str) -> None:
        if not _safe_dir_fd_operations_supported():
            try:
                _fallback_write_context(workspace, body)
            except (OSError, UnicodeEncodeError, ValueError, _UnsafeWorkspaceError) as exc:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                    message="Runtime context could not be written.",
                    retryable=True,
                ) from exc
            return
        root_descriptor = -1
        runtime_descriptor = -1
        context_descriptor = -1
        try:
            root_descriptor = _open_workspace_root(workspace)
            runtime_descriptor = _open_expected_directory(
                root_descriptor,
                "runtime",
                (workspace.runtime_device, workspace.runtime_inode),
            )
            context_descriptor = os.open(
                "context.txt",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=runtime_descriptor,
            )
            metadata = os.fstat(context_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != workspace.root_device
            ):
                raise _UnsafeWorkspaceError("runtime context file is unsafe")
            data = body.encode("utf-8")
            offset = 0
            while offset < len(data):
                written = os.write(context_descriptor, data[offset:])
                if written <= 0:
                    raise OSError("runtime context write made no progress")
                offset += written
            os.fsync(context_descriptor)
            final_metadata = os.fstat(context_descriptor)
            named_metadata = os.stat(
                "context.txt",
                dir_fd=runtime_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or final_metadata.st_nlink != 1
                or final_metadata.st_size != len(data)
                or _identity(named_metadata) != _identity(final_metadata)
                or not stat.S_ISREG(named_metadata.st_mode)
                or named_metadata.st_nlink != 1
            ):
                raise _UnsafeWorkspaceError("runtime context file changed")
            _assert_expected_directory(
                root_descriptor,
                "runtime",
                runtime_descriptor,
                (workspace.runtime_device, workspace.runtime_inode),
            )
            _assert_workspace_root_path(workspace, root_descriptor)
        except (OSError, UnicodeEncodeError, _UnsafeWorkspaceError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Runtime context could not be written.",
                retryable=True,
            ) from exc
        finally:
            if context_descriptor >= 0:
                os.close(context_descriptor)
            if runtime_descriptor >= 0:
                os.close(runtime_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    @staticmethod
    def temporary_output_bytes(workspace: PreparedWorkspace) -> int:
        """Count service/Agent-created runtime and output ordinary files."""

        if not _safe_dir_fd_operations_supported():
            try:
                return _fallback_temporary_output_bytes(workspace)
            except (
                OSError,
                UnicodeEncodeError,
                ValueError,
                _UnsafeWorkspaceError,
            ) as exc:
                raise runtime_failure(
                    stage=ExecutionStage.BACKEND_EXECUTE,
                    code=ErrorCode.WORKSPACE_LIMIT,
                    message="Workspace output could not be measured safely.",
                ) from exc
        root_descriptor = -1
        inputs_descriptor = -1
        runtime_descriptor = -1
        output_descriptor = -1
        try:
            root_descriptor = _open_workspace_root(workspace)
            if set(_listed_names(root_descriptor)) != {"inputs", "runtime", "output"}:
                raise _UnsafeWorkspaceError("workspace root shape changed")
            inputs_descriptor = _open_expected_directory(
                root_descriptor,
                "inputs",
                (workspace.inputs_device, workspace.inputs_inode),
            )
            runtime_descriptor = _open_expected_directory(
                root_descriptor,
                "runtime",
                (workspace.runtime_device, workspace.runtime_inode),
            )
            output_descriptor = _open_expected_directory(
                root_descriptor,
                "output",
                (workspace.output_device, workspace.output_inode),
            )
            total = _measure_untrusted_directory(
                runtime_descriptor,
                workspace_device=workspace.root_device,
            )
            total += _measure_untrusted_directory(
                output_descriptor,
                workspace_device=workspace.root_device,
            )
            _assert_expected_directory(
                root_descriptor,
                "inputs",
                inputs_descriptor,
                (workspace.inputs_device, workspace.inputs_inode),
            )
            _assert_expected_directory(
                root_descriptor,
                "runtime",
                runtime_descriptor,
                (workspace.runtime_device, workspace.runtime_inode),
            )
            _assert_expected_directory(
                root_descriptor,
                "output",
                output_descriptor,
                (workspace.output_device, workspace.output_inode),
            )
            if set(_listed_names(root_descriptor)) != {"inputs", "runtime", "output"}:
                raise _UnsafeWorkspaceError("workspace root shape changed")
            _assert_workspace_root_path(workspace, root_descriptor)
            return total
        except (OSError, UnicodeEncodeError, ValueError, _UnsafeWorkspaceError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.WORKSPACE_LIMIT,
                message="Workspace output could not be measured safely.",
            ) from exc
        finally:
            if output_descriptor >= 0:
                os.close(output_descriptor)
            if runtime_descriptor >= 0:
                os.close(runtime_descriptor)
            if inputs_descriptor >= 0:
                os.close(inputs_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    @staticmethod
    def read_claim(workspace: PreparedWorkspace) -> LogparseParseClaim | None:
        if not _safe_dir_fd_operations_supported():
            try:
                return _fallback_read_claim(workspace)
            except (OSError, TypeError, ValueError, _UnsafeWorkspaceError) as exc:
                raise runtime_failure(
                    stage=ExecutionStage.TOOL_EXECUTE,
                    code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                    message="Logparse parse claim is invalid.",
                ) from exc
        root_descriptor = -1
        runtime_descriptor = -1
        tool_state_descriptor = -1
        claim_descriptor = -1
        try:
            root_descriptor = _open_workspace_root(workspace)
            runtime_descriptor = _open_expected_directory(
                root_descriptor,
                "runtime",
                (workspace.runtime_device, workspace.runtime_inode),
            )
            tool_state_descriptor = _open_expected_directory(
                runtime_descriptor,
                "tool-state",
                (workspace.tool_state_device, workspace.tool_state_inode),
            )
            nodes = _listed_names(tool_state_descriptor)
            if not nodes or nodes == [FINALIZATION_MARKER_NAME]:
                _assert_expected_directory(
                    runtime_descriptor,
                    "tool-state",
                    tool_state_descriptor,
                    (workspace.tool_state_device, workspace.tool_state_inode),
                )
                _assert_expected_directory(
                    root_descriptor,
                    "runtime",
                    runtime_descriptor,
                    (workspace.runtime_device, workspace.runtime_inode),
                )
                _assert_workspace_root_path(workspace, root_descriptor)
                return None
            if set(nodes) not in (
                {"logparse-parse.claim"},
                {"logparse-parse.claim", FINALIZATION_MARKER_NAME},
            ):
                raise _UnsafeWorkspaceError("tool state contains an unexpected node")
            named_metadata = os.stat(
                "logparse-parse.claim",
                dir_fd=tool_state_descriptor,
                follow_symlinks=False,
            )
            claim_descriptor = os.open(
                "logparse-parse.claim",
                _file_flags(),
                dir_fd=tool_state_descriptor,
            )
            opened_metadata = os.fstat(claim_descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or opened_metadata.st_dev != workspace.root_device
                or _identity(opened_metadata) != _identity(named_metadata)
                or opened_metadata.st_size > _MAX_PARSE_CLAIM_BYTES
            ):
                raise _UnsafeWorkspaceError("parse claim is not a bounded ordinary file")
            data = bytearray()
            while True:
                chunk = os.read(
                    claim_descriptor,
                    min(
                        _READ_CHUNK_BYTES,
                        (_MAX_PARSE_CLAIM_BYTES + 1) - len(data),
                    ),
                )
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_PARSE_CLAIM_BYTES:
                    raise _UnsafeWorkspaceError("parse claim is too large")
            final_metadata = os.fstat(claim_descriptor)
            final_named_metadata = os.stat(
                "logparse-parse.claim",
                dir_fd=tool_state_descriptor,
                follow_symlinks=False,
            )
            if (
                _metadata_fingerprint(final_metadata)
                != _metadata_fingerprint(opened_metadata)
                or _metadata_fingerprint(final_named_metadata)
                != _metadata_fingerprint(named_metadata)
                or len(data) != final_metadata.st_size
                or _listed_names(tool_state_descriptor) != nodes
            ):
                raise _UnsafeWorkspaceError("parse claim changed during inspection")
            _assert_expected_directory(
                runtime_descriptor,
                "tool-state",
                tool_state_descriptor,
                (workspace.tool_state_device, workspace.tool_state_inode),
            )
            _assert_expected_directory(
                root_descriptor,
                "runtime",
                runtime_descriptor,
                (workspace.runtime_device, workspace.runtime_inode),
            )
            _assert_workspace_root_path(workspace, root_descriptor)
            return parse_canonical_json_bytes(
                bytes(data),
                model_type=LogparseParseClaim,
            )
        except (OSError, TypeError, ValueError, _UnsafeWorkspaceError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse parse claim is invalid.",
            ) from exc
        finally:
            if claim_descriptor >= 0:
                os.close(claim_descriptor)
            if tool_state_descriptor >= 0:
                os.close(tool_state_descriptor)
            if runtime_descriptor >= 0:
                os.close(runtime_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

__all__ = [
    "PreparedWorkspace",
    "WorkspaceManager",
    "inspect_file",
    "inspect_tree",
]
