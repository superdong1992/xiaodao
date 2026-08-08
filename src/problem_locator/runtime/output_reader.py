"""Validate the only business output accepted from an Agent workspace.

The reader is deliberately a pre-staging boundary.  It returns proposal paths
only after the complete Agent outcome, every declared proposal resource, and a
possible USER_RESULT have passed the frozen S00 validators.  Callers therefore
cannot accidentally stage a prefix of an otherwise invalid Agent response.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Iterator, TypeAlias

from pydantic import ValidationError

from problem_locator.contracts.enums import (
    ArtifactKind,
    ErrorCode,
    EvidenceSourceType,
    ExecutionStage,
    ResourceKind,
)
from problem_locator.contracts.models import (
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    ExecutionFailure,
    Job,
    LogparseEvidenceLocator,
    TreeManifest,
    TreeManifestEntry,
    UserResultArchiveMetadata,
    UserResultPayload,
    WorkspaceArtifactInput,
    WorkspaceEvidenceInput,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import (
    InvalidJsonBytesError,
    bytes_sha256,
    canonical_json_bytes,
)
from problem_locator.diagnostics import log_event
from problem_locator.integrations.agent_json import (
    parse_agent_json_bytes,
    read_agent_json_file,
)
from problem_locator.integrations.result_archive import validate_result_archive_bytes

from .failures import RuntimeExecutionError, runtime_failure
from .outcome_finalizer import (
    DRAFT_FINALIZATION_MARKER_NAME,
    SealedAgentOutcomeDraftMarker,
)
from .workspace import PreparedWorkspace


_READ_CHUNK_BYTES = 64 * 1024
_MAX_FINALIZATION_MARKER_BYTES = 4096
_Draft: TypeAlias = AgentEvidenceProposalDraft | AgentArtifactProposalDraft


class _MissingOutcome(Exception):
    pass


class _InvalidOutput(Exception):
    pass


class _ClassifiedInvalidOutput(_InvalidOutput):
    def __init__(
        self,
        category: str,
        *,
        diagnostic_reason: str | None = None,
        schema_errors: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(category)
        self.category = category
        self.diagnostic_reason = diagnostic_reason
        self.schema_errors = schema_errors


class RejectedAgentOutputError(RuntimeExecutionError):
    """Carry the exact rejected outcome bytes to the Runtime archive boundary."""

    def __init__(
        self,
        failure: ExecutionFailure,
        *,
        failure_category: str,
        raw_outcome_bytes: bytes | None,
    ) -> None:
        self.failure_category = failure_category
        self.raw_outcome_bytes = raw_outcome_bytes
        super().__init__(failure)


@contextmanager
def _classify_invalid_output(category: str) -> Iterator[None]:
    """Attach a content-free diagnostic category to an unsafe failure."""

    try:
        yield
    except _MissingOutcome:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise _ClassifiedInvalidOutput(category) from None


def _diagnostic_path_state(path: Path) -> str:
    """Return only a safe filesystem kind for one fixed protocol path."""

    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISREG(metadata.st_mode):
        return "regular_file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


def _validate_finalization_marker(
    workspace: PreparedWorkspace | Path,
    raw_outcome_bytes: bytes,
) -> None:
    """Require a canonical marker matching the exact Outcome bytes just read."""

    workspace_root = (
        workspace.root
        if isinstance(workspace, PreparedWorkspace)
        else Path(workspace)
    )
    tool_state_root = workspace_root / "runtime" / "tool-state"
    try:
        tool_state_metadata = tool_state_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_missing",
            diagnostic_reason="Agent outcome finalization marker is missing",
        ) from None
    except OSError as exc:
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_invalid",
            diagnostic_reason=str(exc),
        ) from None
    expected_identity: tuple[int, int] | None = None
    if isinstance(workspace, PreparedWorkspace):
        expected_identity = (workspace.tool_state_device, workspace.tool_state_inode)
    if (
        not stat.S_ISDIR(tool_state_metadata.st_mode)
        or (
            expected_identity is not None
            and _identity(tool_state_metadata) != expected_identity
        )
    ):
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_invalid",
            diagnostic_reason="Agent tool-state directory identity is invalid",
        )
    try:
        names = sorted(node.name for node in tool_state_root.iterdir())
    except OSError as exc:
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_invalid",
            diagnostic_reason=str(exc),
        ) from None
    allowed = {DRAFT_FINALIZATION_MARKER_NAME}
    # A parse claim is meaningful only when this exact Workspace was given a
    # server-resolved Logparse plan.  In particular, an initial DIAGNOSE that
    # is still missing problem_time or its attachment must not be able to
    # smuggle broker state into an otherwise ordinary Agent execution.
    if (
        not isinstance(workspace, PreparedWorkspace)
        or workspace.manifest.resolved_logparse_plan is not None
    ):
        allowed.add("logparse-parse.claim")
    if any(name not in allowed for name in names):
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_invalid",
            diagnostic_reason="Agent tool-state contains an unexpected node",
        )
    if DRAFT_FINALIZATION_MARKER_NAME not in names:
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_missing",
            diagnostic_reason="Agent outcome finalization marker is missing",
        )
    marker_path = tool_state_root / DRAFT_FINALIZATION_MARKER_NAME
    try:
        raw_marker, marker_document = read_agent_json_file(
            marker_path,
            max_bytes=_MAX_FINALIZATION_MARKER_BYTES,
        )
        if raw_marker != marker_document.canonical_bytes:
            raise ValueError("Agent outcome finalization marker is not canonical")
        marker = SealedAgentOutcomeDraftMarker.model_validate(marker_document.value)
        final_tool_state_metadata = tool_state_root.stat(follow_symlinks=False)
        final_names = sorted(node.name for node in tool_state_root.iterdir())
        if (
            _identity(final_tool_state_metadata) != _identity(tool_state_metadata)
            or final_names != names
        ):
            raise ValueError("Agent tool-state changed during marker validation")
    except (OSError, TypeError, ValueError) as exc:
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_invalid",
            diagnostic_reason=str(exc),
        ) from None
    if (
        marker.size != len(raw_outcome_bytes)
        or marker.sha256 != bytes_sha256(raw_outcome_bytes)
    ):
        raise _ClassifiedInvalidOutput(
            "outcome_finalizer_marker_mismatch",
            diagnostic_reason=(
                "Agent outcome bytes do not match the finalization marker"
            ),
        )


def _log_output_rejection(
    workspace_root: Path,
    job: Job,
    *,
    code: ErrorCode,
    failure_category: str,
    final_outcome_state: str,
    final_outcome_bytes: int | None,
    diagnostic_reason: str | None = None,
    schema_errors: tuple[dict[str, Any], ...] = (),
) -> None:
    """Record protocol facts and validation diagnostics without raw bytes."""

    output = workspace_root / "output"
    try:
        fields: dict[str, Any] = {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "code": code,
            "failure_category": failure_category,
            "final_outcome_state": final_outcome_state,
            "final_outcome_bytes": final_outcome_bytes,
            "job_outcome_part_state": _diagnostic_path_state(
                output / "job_outcome.draft.json.part"
            ),
            "dot_job_outcome_part_state": _diagnostic_path_state(
                output / ".job_outcome.draft.json.part"
            ),
            "job_outcome_tmp_state": _diagnostic_path_state(
                output / "job_outcome.draft.json.tmp"
            ),
        }
        if diagnostic_reason is not None:
            fields["diagnostic_reason"] = diagnostic_reason
        if schema_errors:
            fields["schema_errors"] = schema_errors
        log_event(
            "runtime.agent_output.rejected",
            level=logging.WARNING,
            **fields,
        )
    except Exception:
        # Observability must never replace the frozen Runtime failure.
        pass


class _WorkspaceTopLevel(StrEnum):
    INPUTS = "inputs"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class _FrozenReadBoundary:
    top_level: _WorkspaceTopLevel
    identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    root: Path
    root_identity: tuple[int, int]
    boundary: _FrozenReadBoundary
    parent_identities: tuple[tuple[str, int, int], ...]
    leaf_identity: tuple[int, int]
    leaf_fingerprint: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ArchiveTargetLog:
    relative_path: str
    source: _WorkspaceTopLevel
    expected_tree_entry: TreeManifestEntry | None


@dataclass(frozen=True, slots=True)
class ValidatedProposalResource:
    """One fully inspected proposal resource, ready for persistent staging."""

    draft: _Draft
    proposal_key: str
    workspace_relative_path: str
    path: Path
    resource_kind: ResourceKind
    size: int
    sha256: str
    tree_manifest: TreeManifest | None
    source_snapshot: _SourceSnapshot | None = None

    def verify_unchanged(self) -> None:
        """Re-audit the frozen Agent path immediately around synchronous staging."""

        _verify_resource_unchanged(self)

    @contextmanager
    def open_verified_file(self) -> Iterator[BinaryIO]:
        """Yield an fd-anchored, pre-hashed stream for a frozen FILE proposal."""

        if self.resource_kind is not ResourceKind.FILE:
            raise _InvalidOutput
        descriptor = _open_snapshot_file(self)
        stream_descriptor = -1
        try:
            actual_size, actual_sha256, _ = _read_descriptor(
                descriptor,
                capture=False,
                max_bytes=self.size,
            )
            if actual_size != self.size or actual_sha256 != self.sha256:
                raise _InvalidOutput
            os.lseek(descriptor, 0, os.SEEK_SET)
            stream_descriptor = os.dup(descriptor)
            with os.fdopen(stream_descriptor, "rb", closefd=True) as stream:
                stream_descriptor = -1
                yield stream
            _assert_snapshot_file(self, descriptor)
        finally:
            if stream_descriptor >= 0:
                os.close(stream_descriptor)
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ValidatedAgentOutput:
    """An all-or-nothing validated view of an Agent's workspace output."""

    outcome: AgentJobOutcome
    canonical_bytes: bytes
    proposal_resources: tuple[ValidatedProposalResource, ...]
    user_result: UserResultPayload | None


@dataclass(frozen=True, slots=True)
class ValidatedAgentDraft:
    """Frozen, sealed Agent draft and all proposal bytes, before server decision."""

    draft: AgentJobOutcomeDraftV2
    canonical_bytes: bytes
    proposal_resources: tuple[ValidatedProposalResource, ...]
    user_result_bytes: bytes | None


class _ExactSecretScanner:
    """Exact binary matcher retaining only the possible cross-chunk suffix."""

    __slots__ = ("_patterns", "_retained", "_tail")

    def __init__(self, patterns: tuple[bytes, ...]) -> None:
        self._patterns = patterns
        self._retained = max((len(pattern) for pattern in patterns), default=0) - 1
        self._tail = b""

    def feed(self, chunk: bytes) -> bool:
        if not self._patterns or not chunk:
            return False
        combined = self._tail + chunk
        if any(pattern in combined for pattern in self._patterns):
            return True
        if self._retained > 0:
            self._tail = combined[-self._retained :]
        else:
            self._tail = b""
        return False


def _normalize_secrets(secrets: Iterable[bytes | str]) -> tuple[bytes, ...]:
    patterns: list[bytes] = []
    seen: set[bytes] = set()
    for secret in secrets:
        if isinstance(secret, str):
            encoded = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            encoded = secret
        else:
            raise TypeError("secret patterns must be bytes or strings")
        if not encoded:
            raise ValueError("secret patterns must be non-empty")
        if encoded not in seen:
            seen.add(encoded)
            patterns.append(encoded)
    return tuple(patterns)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    stable = (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    # Windows exposes creation/change timestamps inconsistently between a
    # named path and an already-open file handle.  The remaining fields are
    # stable across both views and are rechecked before and after the read.
    return stable if os.name == "nt" else stable + (metadata.st_ctime_ns,)


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(metadata, "st_file_attributes", 0) & flag)


def _safe_relative_parts(
    relative_path: str,
    *,
    top_level: str = "output",
) -> tuple[str, ...]:
    parts = tuple(relative_path.split("/"))
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != top_level
    ):
        raise _InvalidOutput
    return parts


def _assert_directory(metadata: os.stat_result, *, device: int) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != device
    ):
        raise _InvalidOutput


def _assert_regular(metadata: os.stat_result, *, device: int) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_dev != device
    ):
        raise _InvalidOutput


def _snapshot_source(
    root: Path,
    relative_path: str,
    *,
    root_identity: tuple[int, int] | None = None,
    boundary: _FrozenReadBoundary | None = None,
) -> _SourceSnapshot:
    selected_boundary = boundary or _FrozenReadBoundary(
        top_level=_WorkspaceTopLevel.OUTPUT,
        identity=None,
    )
    top_level = selected_boundary.top_level.value
    parts = _safe_relative_parts(relative_path, top_level=top_level)
    root_metadata = _lstat(root)
    _assert_directory(root_metadata, device=root_metadata.st_dev)
    if root_identity is not None and _identity(root_metadata) != root_identity:
        raise _InvalidOutput
    device = root_metadata.st_dev
    parents: list[tuple[str, int, int]] = []
    current = root
    for index, part in enumerate(parts[:-1]):
        current = current / part
        metadata = _lstat(current)
        _assert_directory(metadata, device=device)
        relative = "/".join(parts[: index + 1])
        parents.append((relative, metadata.st_dev, metadata.st_ino))
    if not parents or parents[0][0] != top_level:
        raise _InvalidOutput
    if (
        selected_boundary.identity is not None
        and parents[0][1:] != selected_boundary.identity
    ):
        raise _InvalidOutput
    leaf = root.joinpath(*parts)
    leaf_metadata = _lstat(leaf)
    if stat.S_ISDIR(leaf_metadata.st_mode):
        _assert_directory(leaf_metadata, device=device)
    else:
        _assert_regular(leaf_metadata, device=device)
    return _SourceSnapshot(
        root=root,
        root_identity=_identity(root_metadata),
        boundary=_FrozenReadBoundary(
            top_level=selected_boundary.top_level,
            identity=parents[0][1:],
        ),
        parent_identities=tuple(parents),
        leaf_identity=_identity(leaf_metadata),
        leaf_fingerprint=_fingerprint(leaf_metadata),
    )


def _supports_anchored_open() -> bool:
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _supports_anchored_tree() -> bool:
    return _supports_anchored_open() and os.scandir in os.supports_fd


def _file_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )


def _assert_snapshot_paths(snapshot: _SourceSnapshot) -> None:
    root_metadata = _lstat(snapshot.root)
    _assert_directory(root_metadata, device=snapshot.root_identity[0])
    if _identity(root_metadata) != snapshot.root_identity:
        raise _InvalidOutput
    for relative, device, inode in snapshot.parent_identities:
        metadata = _lstat(snapshot.root / relative)
        _assert_directory(metadata, device=snapshot.root_identity[0])
        if _identity(metadata) != (device, inode):
            raise _InvalidOutput


def _open_snapshot_path(snapshot: _SourceSnapshot, relative_path: str) -> int:
    if not snapshot.parent_identities:
        raise _InvalidOutput
    parts = _safe_relative_parts(
        relative_path,
        top_level=snapshot.parent_identities[0][0],
    )
    _assert_snapshot_paths(snapshot)
    descriptor = -1
    directory_descriptors: list[int] = []
    try:
        if _supports_anchored_open():
            root_descriptor = os.open(snapshot.root, _directory_open_flags())
            directory_descriptors.append(root_descriptor)
            root_metadata = os.fstat(root_descriptor)
            if _identity(root_metadata) != snapshot.root_identity:
                raise _InvalidOutput
            parent_descriptor = root_descriptor
            for index, part in enumerate(parts[:-1]):
                relative = "/".join(parts[: index + 1])
                expected = next(
                    (value[1:] for value in snapshot.parent_identities if value[0] == relative),
                    None,
                )
                if expected is None:
                    raise _InvalidOutput
                child = os.open(part, _directory_open_flags(), dir_fd=parent_descriptor)
                directory_descriptors.append(child)
                metadata = os.fstat(child)
                _assert_directory(metadata, device=snapshot.root_identity[0])
                if _identity(metadata) != expected:
                    raise _InvalidOutput
                parent_descriptor = child
            descriptor = os.open(parts[-1], _file_open_flags(), dir_fd=parent_descriptor)
        else:
            # Windows/no-dirfd conservative fallback: every ancestor is frozen
            # before open; O_NONBLOCK prevents a raced FIFO from hanging, and
            # fstat below binds the handle to the frozen leaf identity.
            descriptor = os.open(snapshot.root.joinpath(*parts), _file_open_flags())
        metadata = os.fstat(descriptor)
        _assert_regular(metadata, device=snapshot.root_identity[0])
        if (
            _identity(metadata) != snapshot.leaf_identity
            or _fingerprint(metadata) != snapshot.leaf_fingerprint
        ):
            raise _InvalidOutput
        _assert_snapshot_paths(snapshot)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _open_snapshot_directory(
    snapshot: _SourceSnapshot,
    relative_path: str,
) -> int:
    """Open a frozen directory through its complete anchored parent chain."""

    if not _supports_anchored_tree():
        raise _InvalidOutput
    if not snapshot.parent_identities:
        raise _InvalidOutput
    parts = _safe_relative_parts(
        relative_path,
        top_level=snapshot.parent_identities[0][0],
    )
    _assert_snapshot_paths(snapshot)
    result = -1
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(snapshot.root, _directory_open_flags())
        descriptors.append(root_descriptor)
        root_metadata = os.fstat(root_descriptor)
        _assert_directory(root_metadata, device=snapshot.root_identity[0])
        if _identity(root_metadata) != snapshot.root_identity:
            raise _InvalidOutput
        parent_descriptor = root_descriptor
        for index, part in enumerate(parts):
            child = os.open(
                part,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            descriptors.append(child)
            metadata = os.fstat(child)
            _assert_directory(metadata, device=snapshot.root_identity[0])
            relative = "/".join(parts[: index + 1])
            if index == len(parts) - 1:
                expected = snapshot.leaf_identity
                expected_fingerprint = snapshot.leaf_fingerprint
            else:
                expected = next(
                    (
                        value[1:]
                        for value in snapshot.parent_identities
                        if value[0] == relative
                    ),
                    None,
                )
                expected_fingerprint = None
            if expected is None or _identity(metadata) != expected:
                raise _InvalidOutput
            if (
                expected_fingerprint is not None
                and _fingerprint(metadata) != expected_fingerprint
            ):
                raise _InvalidOutput
            parent_descriptor = child
        result = descriptors.pop()
        _assert_snapshot_paths(snapshot)
        return result
    except BaseException:
        if result >= 0:
            os.close(result)
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_snapshot_file(resource: ValidatedProposalResource) -> int:
    snapshot = resource.source_snapshot
    if snapshot is None:
        raise _InvalidOutput
    return _open_snapshot_path(snapshot, resource.workspace_relative_path)


def _assert_snapshot_file(resource: ValidatedProposalResource, descriptor: int) -> None:
    snapshot = resource.source_snapshot
    if snapshot is None:
        raise _InvalidOutput
    metadata = os.fstat(descriptor)
    _assert_regular(metadata, device=snapshot.root_identity[0])
    if (
        _identity(metadata) != snapshot.leaf_identity
        or _fingerprint(metadata) != snapshot.leaf_fingerprint
    ):
        raise _InvalidOutput
    named = _lstat(resource.path)
    if _fingerprint(named) != snapshot.leaf_fingerprint:
        raise _InvalidOutput
    _assert_snapshot_paths(snapshot)


def _read_descriptor(
    descriptor: int,
    *,
    patterns: tuple[bytes, ...] = (),
    capture: bool,
    max_bytes: int | None = None,
) -> tuple[int, str, bytes | None]:
    size = 0
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    scanner = _ExactSecretScanner(patterns)
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if max_bytes is not None and size > max_bytes:
            raise _InvalidOutput
        if scanner.feed(chunk):
            raise _InvalidOutput
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return size, digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def _read_frozen_relative_file(
    root: Path,
    relative_path: str,
    *,
    patterns: tuple[bytes, ...] = (),
    capture: bool = False,
    root_identity: tuple[int, int] | None = None,
    boundary: _FrozenReadBoundary | None = None,
    expected_parent_identities: dict[str, tuple[int, int]] | None = None,
    max_bytes: int | None = None,
) -> tuple[int, str, bytes | None, _SourceSnapshot]:
    snapshot = _snapshot_source(
        root,
        relative_path,
        root_identity=root_identity,
        boundary=boundary,
    )
    if expected_parent_identities is not None:
        actual_parents = {
            relative: (device, inode)
            for relative, device, inode in snapshot.parent_identities
        }
        for relative, expected in expected_parent_identities.items():
            if (
                relative_path.startswith(f"{relative}/")
                and actual_parents.get(relative) != expected
            ):
                raise _InvalidOutput
    descriptor = _open_snapshot_path(snapshot, relative_path)
    try:
        expected_size = os.fstat(descriptor).st_size
        if max_bytes is not None and expected_size > max_bytes:
            raise _InvalidOutput
        size, sha256, content = _read_descriptor(
            descriptor,
            patterns=patterns,
            capture=capture,
            max_bytes=expected_size,
        )
        if size != expected_size:
            raise _InvalidOutput
        metadata = os.fstat(descriptor)
        named = _lstat(root / relative_path)
        if (
            _fingerprint(metadata) != snapshot.leaf_fingerprint
            or _fingerprint(named) != snapshot.leaf_fingerprint
        ):
            raise _InvalidOutput
        _assert_snapshot_paths(snapshot)
        return size, sha256, content, snapshot
    finally:
        os.close(descriptor)


def _scan_bytes(data: bytes, patterns: tuple[bytes, ...]) -> None:
    scanner = _ExactSecretScanner(patterns)
    for offset in range(0, len(data), _READ_CHUNK_BYTES):
        if scanner.feed(data[offset : offset + _READ_CHUNK_BYTES]):
            raise _InvalidOutput


def _lstat(path: Path, *, missing_outcome: bool = False) -> os.stat_result:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if missing_outcome:
            raise _MissingOutcome from None
        raise _InvalidOutput from None
    except OSError:
        raise _InvalidOutput from None


def _validate_parent_directories(
    workspace_root: Path,
    relative_path: str,
    *,
    missing_outcome: bool = False,
) -> Path:
    """Validate every existing ancestor without resolving or following links."""

    if not isinstance(workspace_root, Path):
        workspace_root = Path(workspace_root)
    parts = relative_path.split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise _InvalidOutput

    current = workspace_root
    for part in parts[:-1]:
        metadata = _lstat(current, missing_outcome=missing_outcome)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _InvalidOutput
        current = current / part
    metadata = _lstat(current, missing_outcome=missing_outcome)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _InvalidOutput
    return current / parts[-1]


def _scan_relative_path(relative_path: str, patterns: tuple[bytes, ...]) -> None:
    try:
        encoded = relative_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _InvalidOutput from None
    _scan_bytes(encoded, patterns)


def _scandir_names(directory_descriptor: int) -> tuple[str, ...]:
    scan_descriptor = -1
    try:
        # fdopendir(dup(fd)) would share the original directory offset.  A new
        # open-file-description for "." lets every stability pass start at 0.
        scan_descriptor = os.open(
            ".",
            _directory_open_flags(),
            dir_fd=directory_descriptor,
        )
        if _identity(os.fstat(scan_descriptor)) != _identity(
            os.fstat(directory_descriptor)
        ):
            raise _InvalidOutput
        with os.scandir(scan_descriptor) as iterator:
            names = tuple(sorted(entry.name for entry in iterator))
        for name in names:
            name.encode("utf-8", errors="strict")
        return names
    except (OSError, UnicodeEncodeError):
        raise _InvalidOutput from None
    finally:
        if scan_descriptor >= 0:
            os.close(scan_descriptor)


def _named_metadata(directory_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        raise _InvalidOutput from None


def _inspect_tree_descriptor(
    directory_descriptor: int,
    *,
    tree_prefix: str,
    workspace_relative_root: str,
    workspace_device: int,
    patterns: tuple[bytes, ...],
    max_bytes: int,
) -> tuple[list[TreeManifestEntry], int]:
    """Inspect one directory without resolving a child through ambient paths."""

    try:
        directory_metadata = os.fstat(directory_descriptor)
    except OSError:
        raise _InvalidOutput from None
    _assert_directory(directory_metadata, device=workspace_device)
    directory_fingerprint = _fingerprint(directory_metadata)
    names = _scandir_names(directory_descriptor)
    manifest_entries: list[TreeManifestEntry] = []
    consumed_bytes = 0
    for name in names:
        named_before = _named_metadata(directory_descriptor, name)
        if (
            stat.S_ISLNK(named_before.st_mode)
            or _is_reparse(named_before)
            or named_before.st_dev != workspace_device
        ):
            raise _InvalidOutput
        tree_relative = f"{tree_prefix}/{name}" if tree_prefix else name
        workspace_relative = f"{workspace_relative_root}/{tree_relative}"
        _scan_relative_path(workspace_relative, patterns)
        if stat.S_ISDIR(named_before.st_mode):
            child_descriptor = -1
            try:
                child_descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=directory_descriptor,
                )
                child_metadata = os.fstat(child_descriptor)
                _assert_directory(child_metadata, device=workspace_device)
                if _fingerprint(child_metadata) != _fingerprint(named_before):
                    raise _InvalidOutput
                child_entries, child_bytes = _inspect_tree_descriptor(
                    child_descriptor,
                    tree_prefix=tree_relative,
                    workspace_relative_root=workspace_relative_root,
                    workspace_device=workspace_device,
                    patterns=patterns,
                    max_bytes=max_bytes - consumed_bytes,
                )
                manifest_entries.extend(child_entries)
                consumed_bytes += child_bytes
                if _fingerprint(os.fstat(child_descriptor)) != _fingerprint(named_before):
                    raise _InvalidOutput
                if _fingerprint(
                    _named_metadata(directory_descriptor, name)
                ) != _fingerprint(named_before):
                    raise _InvalidOutput
            except OSError:
                raise _InvalidOutput from None
            finally:
                if child_descriptor >= 0:
                    os.close(child_descriptor)
            continue
        if not stat.S_ISREG(named_before.st_mode) or named_before.st_nlink != 1:
            raise _InvalidOutput
        if named_before.st_size > max_bytes - consumed_bytes:
            raise _InvalidOutput
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                name,
                _file_open_flags(),
                dir_fd=directory_descriptor,
            )
            file_metadata = os.fstat(file_descriptor)
            _assert_regular(file_metadata, device=workspace_device)
            if _fingerprint(file_metadata) != _fingerprint(named_before):
                raise _InvalidOutput
            size, sha256, _ = _read_descriptor(
                file_descriptor,
                patterns=patterns,
                capture=False,
                max_bytes=named_before.st_size,
            )
            if size != named_before.st_size:
                raise _InvalidOutput
            if _fingerprint(os.fstat(file_descriptor)) != _fingerprint(named_before):
                raise _InvalidOutput
            if _fingerprint(
                _named_metadata(directory_descriptor, name)
            ) != _fingerprint(named_before):
                raise _InvalidOutput
        except OSError:
            raise _InvalidOutput from None
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
        manifest_entries.append(
            TreeManifestEntry(path=tree_relative, size=size, sha256=sha256)
        )
        consumed_bytes += size
    if _scandir_names(directory_descriptor) != names:
        raise _InvalidOutput
    try:
        if _fingerprint(os.fstat(directory_descriptor)) != directory_fingerprint:
            raise _InvalidOutput
    except OSError:
        raise _InvalidOutput from None
    return manifest_entries, consumed_bytes


def _inspect_tree(
    root: Path,
    *,
    workspace_root: Path,
    patterns: tuple[bytes, ...],
    max_bytes: int,
    root_identity: tuple[int, int] | None = None,
    boundary: _FrozenReadBoundary | None = None,
) -> tuple[int, str, TreeManifest]:
    workspace_metadata = _lstat(workspace_root)
    workspace_device = workspace_metadata.st_dev
    tree_relative_root = root.relative_to(workspace_root).as_posix()
    tree_snapshot = _snapshot_source(
        workspace_root,
        tree_relative_root,
        root_identity=root_identity,
        boundary=boundary,
    )
    metadata = _lstat(root)
    _assert_directory(metadata, device=workspace_device)
    if (
        _identity(metadata) != tree_snapshot.leaf_identity
        or _fingerprint(metadata) != tree_snapshot.leaf_fingerprint
    ):
        raise _InvalidOutput

    if _supports_anchored_tree():
        tree_descriptor = _open_snapshot_directory(
            tree_snapshot,
            tree_relative_root,
        )
        try:
            manifest_entries, consumed_bytes = _inspect_tree_descriptor(
                tree_descriptor,
                tree_prefix="",
                workspace_relative_root=tree_relative_root,
                workspace_device=workspace_device,
                patterns=patterns,
                max_bytes=max_bytes,
            )
            if _fingerprint(os.fstat(tree_descriptor)) != tree_snapshot.leaf_fingerprint:
                raise _InvalidOutput
        except OSError:
            raise _InvalidOutput from None
        finally:
            os.close(tree_descriptor)
    else:
        # Windows/no-dirfd fallback.  Agent execution has ended before this
        # trusted-workspace traversal; every discovered leaf is still reopened
        # and verified against its frozen identity before content is consumed.
        directory_identities: dict[str, tuple[int, int]] = {
            relative: (device, inode)
            for relative, device, inode in tree_snapshot.parent_identities
        }
        directory_identities[tree_relative_root] = _identity(metadata)
        directory_fingerprints: dict[str, tuple[int, ...]] = {
            tree_relative_root: _fingerprint(metadata)
        }
        pending: list[tuple[Path, str]] = [(root, "")]
        files: list[tuple[str, Path, int]] = []
        while pending:
            directory, tree_prefix = pending.pop()
            workspace_relative_directory = (
                f"{tree_relative_root}/{tree_prefix}"
                if tree_prefix
                else tree_relative_root
            )
            expected_directory = directory_fingerprints.get(
                workspace_relative_directory
            )
            directory_before = _lstat(directory)
            _assert_directory(directory_before, device=workspace_device)
            if (
                expected_directory is None
                or _fingerprint(directory_before) != expected_directory
            ):
                raise _InvalidOutput
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError:
                raise _InvalidOutput from None
            child_directories: list[tuple[Path, str]] = []
            for entry in entries:
                try:
                    entry.name.encode("utf-8", errors="strict")
                    entry_metadata = _lstat(Path(entry.path))
                except (OSError, UnicodeEncodeError):
                    raise _InvalidOutput from None
                if (
                    stat.S_ISLNK(entry_metadata.st_mode)
                    or _is_reparse(entry_metadata)
                    or entry_metadata.st_dev != workspace_device
                ):
                    raise _InvalidOutput
                tree_relative = (
                    f"{tree_prefix}/{entry.name}" if tree_prefix else entry.name
                )
                workspace_relative = (
                    Path(root).relative_to(workspace_root) / tree_relative
                ).as_posix()
                _scan_relative_path(workspace_relative, patterns)
                if stat.S_ISDIR(entry_metadata.st_mode):
                    directory_identities[workspace_relative] = _identity(entry_metadata)
                    directory_fingerprints[workspace_relative] = _fingerprint(
                        entry_metadata
                    )
                    child_directories.append((Path(entry.path), tree_relative))
                elif stat.S_ISREG(entry_metadata.st_mode) and entry_metadata.st_nlink == 1:
                    files.append((tree_relative, Path(entry.path), entry_metadata.st_size))
                else:
                    raise _InvalidOutput
            try:
                with os.scandir(directory) as iterator:
                    final_names = tuple(sorted(entry.name for entry in iterator))
            except OSError:
                raise _InvalidOutput from None
            if final_names != tuple(entry.name for entry in entries):
                raise _InvalidOutput
            directory_after = _lstat(directory)
            _assert_directory(directory_after, device=workspace_device)
            if _fingerprint(directory_after) != expected_directory:
                raise _InvalidOutput
            pending.extend(reversed(child_directories))

        manifest_entries = []
        consumed_bytes = 0
        for relative_path, path, expected_size in sorted(
            files,
            key=lambda item: item[0],
        ):
            if expected_size > max_bytes - consumed_bytes:
                raise _InvalidOutput
            workspace_relative = path.relative_to(workspace_root).as_posix()
            size, sha256, _, _ = _read_frozen_relative_file(
                workspace_root,
                workspace_relative,
                patterns=patterns,
                root_identity=tree_snapshot.root_identity,
                boundary=tree_snapshot.boundary,
                expected_parent_identities=directory_identities,
                max_bytes=max_bytes - consumed_bytes,
            )
            consumed_bytes += size
            manifest_entries.append(
                TreeManifestEntry(path=relative_path, size=size, sha256=sha256)
            )
        for relative, expected in directory_fingerprints.items():
            if _fingerprint(_lstat(workspace_root / relative)) != expected:
                raise _InvalidOutput
    manifest_entries.sort(key=lambda entry: entry.path)
    manifest = TreeManifest(version=1, entries=manifest_entries)
    size = sum(entry.size for entry in manifest.entries)
    if size != consumed_bytes or size > max_bytes:
        raise _InvalidOutput
    _assert_snapshot_paths(tree_snapshot)
    named_root = _lstat(root)
    if (
        _identity(named_root) != tree_snapshot.leaf_identity
        or _fingerprint(named_root) != tree_snapshot.leaf_fingerprint
    ):
        raise _InvalidOutput
    return size, bytes_sha256(canonical_json_bytes(manifest)), manifest


def _verify_resource_unchanged(resource: ValidatedProposalResource) -> None:
    snapshot = resource.source_snapshot
    if snapshot is None:
        raise _InvalidOutput
    _assert_snapshot_paths(snapshot)
    leaf = _lstat(resource.path)
    if (
        _identity(leaf) != snapshot.leaf_identity
        or _fingerprint(leaf) != snapshot.leaf_fingerprint
    ):
        raise _InvalidOutput
    if resource.resource_kind is ResourceKind.FILE:
        descriptor = _open_snapshot_file(resource)
        try:
            size, sha256, _ = _read_descriptor(
                descriptor,
                capture=False,
                max_bytes=resource.size,
            )
            if size != resource.size or sha256 != resource.sha256:
                raise _InvalidOutput
            _assert_snapshot_file(resource, descriptor)
        finally:
            os.close(descriptor)
        return
    size, sha256, manifest = _inspect_tree(
        resource.path,
        workspace_root=snapshot.root,
        patterns=(),
        max_bytes=resource.size,
        root_identity=snapshot.root_identity,
        boundary=snapshot.boundary,
    )
    if (
        size != resource.size
        or sha256 != resource.sha256
        or manifest != resource.tree_manifest
    ):
        raise _InvalidOutput
    _assert_snapshot_paths(snapshot)
    leaf = _lstat(resource.path)
    if _fingerprint(leaf) != snapshot.leaf_fingerprint:
        raise _InvalidOutput


def _proposal_drafts(
    outcome: AgentJobOutcome | AgentJobOutcomeDraftV2,
) -> tuple[_Draft, ...]:
    return tuple(outcome.proposed_evidence_drafts) + tuple(
        outcome.proposed_artifact_drafts
    )


def _validate_declared_values(
    draft: _Draft,
    *,
    actual_size: int,
    actual_sha256: str,
) -> None:
    if draft.declared_size is not None and draft.declared_size != actual_size:
        raise _InvalidOutput
    if draft.declared_sha256 is not None and draft.declared_sha256 != actual_sha256:
        raise _InvalidOutput


def _candidate_evidence_bindings(
    outcome: AgentJobOutcome | AgentJobOutcomeDraftV2,
) -> tuple[tuple[str, str], ...]:
    payload = outcome.payload
    candidate = getattr(payload, "candidate_conclusion_draft", None)
    if candidate is None:
        return ()
    result: list[tuple[str, str]] = []
    bindings = list(candidate.supporting_evidence_bindings)
    for mapping in candidate.completion_criteria_mapping:
        bindings.extend(mapping.evidence_bindings)
    for binding in bindings:
        key = (
            "existing",
            binding.existing_evidence_id,
        ) if binding.existing_evidence_id is not None else (
            "proposal",
            binding.evidence_proposal_key,
        )
        assert key[1] is not None
        typed_key = (key[0], key[1])
        if typed_key not in result:
            result.append(typed_key)
    return tuple(result)


def _archive_target_logs(
    outcome: AgentJobOutcome | AgentJobOutcomeDraftV2,
    workspace_manifest: WorkspaceInputManifest,
    resources: dict[str, ValidatedProposalResource],
) -> tuple[_ArchiveTargetLog, ...]:
    proposed_evidence = {
        draft.proposal_key: draft for draft in outcome.proposed_evidence_drafts
    }
    existing_evidence = {
        entry.resource_id: entry
        for entry in workspace_manifest.entries
        if isinstance(entry, WorkspaceEvidenceInput)
    }
    existing_artifacts = {
        entry.resource_id: entry
        for entry in workspace_manifest.entries
        if isinstance(entry, WorkspaceArtifactInput)
    }
    result: list[_ArchiveTargetLog] = []
    seen_paths: set[str] = set()
    for binding_kind, binding_id in _candidate_evidence_bindings(outcome):
        source = _WorkspaceTopLevel.INPUTS
        expected_tree_entry = None
        if binding_kind == "proposal":
            evidence = proposed_evidence.get(binding_id)
            if evidence is None or evidence.source_type is not EvidenceSourceType.LOGPARSE:
                continue
            if not isinstance(evidence.locator, LogparseEvidenceLocator):
                raise _InvalidOutput
            artifact_key = evidence.source_binding.artifact_proposal_key
            if artifact_key is not None:
                resource = resources.get(artifact_key)
                if (
                    resource is None
                    or not isinstance(resource.draft, AgentArtifactProposalDraft)
                    or resource.draft.artifact_kind is not ArtifactKind.LOGPARSE_RUN
                    or resource.resource_kind is not ResourceKind.DIRECTORY
                ):
                    raise _InvalidOutput
                root = resource.workspace_relative_path
                manifest = resource.tree_manifest
                if manifest is None:
                    raise _InvalidOutput
                matching_entries = [
                    entry
                    for entry in manifest.entries
                    if entry.path == evidence.locator.relative_path
                ]
                if len(matching_entries) != 1:
                    raise _InvalidOutput
                source = _WorkspaceTopLevel.OUTPUT
                expected_tree_entry = matching_entries[0]
            else:
                artifact_id = evidence.source_binding.existing_source_ref
                artifact = existing_artifacts.get(artifact_id or "")
                if artifact is None or artifact.artifact_kind is not ArtifactKind.LOGPARSE_RUN:
                    raise _InvalidOutput
                root = artifact.relative_path
            relative_path = f"{root}/{evidence.locator.relative_path}"
        else:
            evidence = existing_evidence.get(binding_id)
            if evidence is None or evidence.source_type is not EvidenceSourceType.LOGPARSE:
                continue
            if not isinstance(evidence.locator, LogparseEvidenceLocator):
                raise _InvalidOutput
            artifact = existing_artifacts.get(evidence.source_ref)
            if artifact is None or artifact.artifact_kind is not ArtifactKind.LOGPARSE_RUN:
                raise _InvalidOutput
            relative_path = f"{artifact.relative_path}/{evidence.locator.relative_path}"
        if relative_path not in seen_paths:
            seen_paths.add(relative_path)
            result.append(
                _ArchiveTargetLog(
                    relative_path=relative_path,
                    source=source,
                    expected_tree_entry=expected_tree_entry,
                )
            )
    return tuple(result)


def _validate_user_result_archive(
    workspace_root: Path,
    outcome: AgentJobOutcome | AgentJobOutcomeDraftV2,
    workspace_manifest: WorkspaceInputManifest,
    resources: dict[str, ValidatedProposalResource],
    archive_bytes: bytes,
    patterns: tuple[bytes, ...],
    *,
    root_identity: tuple[int, int],
    output_boundary: _FrozenReadBoundary,
    inputs_boundary: _FrozenReadBoundary,
    max_bytes: int,
) -> None:
    archives = [
        draft
        for draft in outcome.proposed_artifact_drafts
        if draft.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE
    ]
    if not archives:
        return
    if len(archives) != 1 or not isinstance(
        archives[0].metadata,
        UserResultArchiveMetadata,
    ):
        raise _InvalidOutput
    targets = _archive_target_logs(outcome, workspace_manifest, resources)
    if archives[0].metadata.target_log_count != len(targets):
        raise _InvalidOutput
    target_logs: list[bytes] = []
    remaining = max_bytes
    boundaries = {
        _WorkspaceTopLevel.INPUTS: inputs_boundary,
        _WorkspaceTopLevel.OUTPUT: output_boundary,
    }
    for target in targets:
        size, sha256, content, _ = _read_frozen_relative_file(
            workspace_root,
            target.relative_path,
            patterns=patterns,
            capture=True,
            root_identity=root_identity,
            boundary=boundaries[target.source],
            max_bytes=remaining,
        )
        if target.expected_tree_entry is not None and (
            size != target.expected_tree_entry.size
            or sha256 != target.expected_tree_entry.sha256
        ):
            raise _InvalidOutput
        remaining -= size
        assert content is not None
        target_logs.append(content)
    result_text = validate_result_archive_bytes(
        archive_bytes,
        target_logs=tuple(target_logs),
    )
    candidate = outcome.payload.candidate_conclusion_draft
    if candidate is None or result_text != candidate.statement + "\n":
        raise _InvalidOutput


def _read_validated_output(
    workspace: PreparedWorkspace | Path,
    workspace_root: Path,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    patterns: tuple[bytes, ...],
    *,
    root_identity: tuple[int, int],
    output_boundary: _FrozenReadBoundary,
    inputs_boundary: _FrozenReadBoundary,
    capture_outcome_bytes: Callable[[bytes], None] | None = None,
) -> ValidatedAgentDraft:
    outcome_relative_path = "output/job_outcome.draft.json"
    with _classify_invalid_output("sealed_draft_read"):
        outcome_path = _validate_parent_directories(
            workspace_root,
            outcome_relative_path,
            missing_outcome=True,
        )
        remaining_bytes = job.resource_limits.workspace_bytes
        outcome_size, _, raw_outcome_bytes, _ = _read_frozen_relative_file(
            workspace_root,
            outcome_relative_path,
            capture=True,
            root_identity=root_identity,
            boundary=output_boundary,
            max_bytes=remaining_bytes,
        )
    remaining_bytes -= outcome_size
    assert raw_outcome_bytes is not None
    if capture_outcome_bytes is not None:
        capture_outcome_bytes(raw_outcome_bytes)
    try:
        outcome_document = parse_agent_json_bytes(raw_outcome_bytes)
    except InvalidJsonBytesError as exc:
        raise _ClassifiedInvalidOutput(
            "outcome_json_invalid",
            diagnostic_reason=str(exc),
        ) from None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise _ClassifiedInvalidOutput(
            "outcome_json_invalid",
            diagnostic_reason=str(exc),
        ) from None
    if raw_outcome_bytes != outcome_document.canonical_bytes:
        raise _ClassifiedInvalidOutput(
            "outcome_non_canonical",
            diagnostic_reason="Agent outcome bytes are not Canonical JSON",
        )
    parsed_outcome = outcome_document.value
    try:
        outcome = AgentJobOutcomeDraftV2.model_validate(parsed_outcome)
    except ValidationError as exc:
        schema_errors = tuple(
            {
                "location": [str(part) for part in error["loc"]],
                "type": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise _ClassifiedInvalidOutput(
            "outcome_schema",
            diagnostic_reason=(
                "AgentJobOutcomeDraftV2 validation produced "
                f"{len(schema_errors)} error(s)"
            ),
            schema_errors=schema_errors,
        ) from None
    _validate_finalization_marker(workspace, raw_outcome_bytes)
    with _classify_invalid_output("draft_job_binding"):
        if (
            outcome.job_id != job.job_id
            or outcome.case_id != job.case_id
            or outcome.job_type is not job.job_type
            or outcome.base_state_revision != job.base_state_revision
            or any(
                item not in set(job.evidence_refs)
                for item in outcome.consumed_evidence_refs
            )
        ):
            raise _InvalidOutput
    with _classify_invalid_output("outcome_security_scan"):
        outcome_bytes = canonical_json_bytes(outcome)
        _scan_bytes(outcome_bytes, patterns)

    with _classify_invalid_output("proposal_manifest"):
        drafts = _proposal_drafts(outcome)
        declared_paths = [
            draft.workspace_relative_path
            for draft in drafts
            if draft.workspace_relative_path is not None
        ]
        if len(declared_paths) != len(set(declared_paths)):
            raise _InvalidOutput

    resources: list[ValidatedProposalResource] = []
    user_result_bytes: bytes | None = None
    user_result_archive_bytes: bytes | None = None
    for draft in drafts:
        relative_path = draft.workspace_relative_path
        if relative_path is None:
            continue
        with _classify_invalid_output("proposal_path_validation"):
            required_prefix = f"output/proposals/{draft.proposal_key}/"
            if not relative_path.startswith(required_prefix):
                raise _InvalidOutput
            _scan_relative_path(relative_path, patterns)
            path = _validate_parent_directories(workspace_root, relative_path)

        if isinstance(draft, AgentArtifactProposalDraft):
            resource_kind = draft.resource_kind
            capture = draft.artifact_kind in {
                ArtifactKind.USER_RESULT,
                ArtifactKind.USER_RESULT_ARCHIVE,
            }
        else:
            resource_kind = ResourceKind.FILE
            capture = False

        with _classify_invalid_output("proposal_resource_read"):
            if resource_kind is ResourceKind.FILE:
                size, sha256, content, source_snapshot = _read_frozen_relative_file(
                    workspace_root,
                    relative_path,
                    patterns=patterns,
                    capture=capture,
                    root_identity=root_identity,
                    boundary=output_boundary,
                    max_bytes=remaining_bytes,
                )
                tree_manifest = None
                if capture:
                    assert content is not None
                    if (
                        isinstance(draft, AgentArtifactProposalDraft)
                        and draft.artifact_kind is ArtifactKind.USER_RESULT
                    ):
                        user_result_bytes = content
                    else:
                        user_result_archive_bytes = content
            else:
                size, sha256, tree_manifest = _inspect_tree(
                    path,
                    workspace_root=workspace_root,
                    patterns=patterns,
                    max_bytes=remaining_bytes,
                    root_identity=root_identity,
                    boundary=output_boundary,
                )
                source_snapshot = _snapshot_source(
                    workspace_root,
                    relative_path,
                    root_identity=root_identity,
                    boundary=output_boundary,
                )
            remaining_bytes -= size
        with _classify_invalid_output("proposal_declared_values"):
            _validate_declared_values(
                draft,
                actual_size=size,
                actual_sha256=sha256,
            )
        resources.append(
            ValidatedProposalResource(
                draft=draft,
                proposal_key=draft.proposal_key,
                workspace_relative_path=relative_path,
                path=path,
                resource_kind=resource_kind,
                size=size,
                sha256=sha256,
                tree_manifest=tree_manifest,
                source_snapshot=source_snapshot,
            )
        )

    if user_result_archive_bytes is not None:
        with _classify_invalid_output("user_result_archive_validation"):
            _validate_user_result_archive(
                workspace_root,
                outcome,
                workspace_manifest,
                {resource.proposal_key: resource for resource in resources},
                user_result_archive_bytes,
                patterns,
                root_identity=root_identity,
                output_boundary=output_boundary,
                inputs_boundary=inputs_boundary,
                max_bytes=job.resource_limits.workspace_bytes,
            )
    return ValidatedAgentDraft(
        draft=outcome,
        canonical_bytes=outcome_bytes,
        proposal_resources=tuple(resources),
        user_result_bytes=user_result_bytes,
    )


def read_agent_output(
    workspace: PreparedWorkspace | Path,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    *,
    secrets: Iterable[bytes | str] = (),
) -> ValidatedAgentDraft:
    """Read the sealed V2 Agent draft and freeze every proposal resource.

    ``.part`` files are never considered.  Any Agent-controlled invalidity is
    collapsed to the frozen OUTCOME_INVALID failure without retaining an
    exception cause, path, content, endpoint, or token in the error surface.
    """

    patterns = _normalize_secrets(secrets)
    workspace_root = (
        workspace.root
        if isinstance(workspace, PreparedWorkspace)
        else Path(workspace)
    )
    missing = False
    invalid = False
    failure_category = "workspace_or_proposal_validation"
    diagnostic_reason: str | None = None
    schema_errors: tuple[dict[str, Any], ...] = ()
    final_outcome_state = "not_checked"
    final_outcome_bytes: int | None = None
    raw_outcome_bytes: bytes | None = None
    validated: ValidatedAgentDraft | None = None

    def capture_outcome_bytes(value: bytes) -> None:
        nonlocal raw_outcome_bytes
        raw_outcome_bytes = value

    try:
        if isinstance(workspace, PreparedWorkspace):
            root_identity = (workspace.root_device, workspace.root_inode)
            output_identity = (workspace.output_device, workspace.output_inode)
            inputs_identity = (workspace.inputs_device, workspace.inputs_inode)
            if workspace.manifest != workspace_manifest:
                raise _InvalidOutput
        else:
            root_metadata = _lstat(workspace_root)
            _assert_directory(root_metadata, device=root_metadata.st_dev)
            root_identity = _identity(root_metadata)
            output_metadata = _lstat(workspace_root / "output", missing_outcome=True)
            _assert_directory(output_metadata, device=root_metadata.st_dev)
            output_identity = _identity(output_metadata)
            inputs_identity = None
            try:
                inputs_metadata = (workspace_root / "inputs").stat(
                    follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            except OSError:
                raise _InvalidOutput from None
            else:
                _assert_directory(inputs_metadata, device=root_metadata.st_dev)
                inputs_identity = _identity(inputs_metadata)
        output_boundary = _FrozenReadBoundary(
            top_level=_WorkspaceTopLevel.OUTPUT,
            identity=output_identity,
        )
        inputs_boundary = _FrozenReadBoundary(
            top_level=_WorkspaceTopLevel.INPUTS,
            identity=inputs_identity,
        )
        final_metadata = _lstat(
            workspace_root / "output/job_outcome.draft.json",
            missing_outcome=True,
        )
        final_outcome_state = "present"
        final_outcome_bytes = final_metadata.st_size
        initial = _snapshot_source(
            workspace_root,
            "output/job_outcome.draft.json",
            root_identity=root_identity,
            boundary=output_boundary,
        )
        validated = _read_validated_output(
            workspace,
            workspace_root,
            job,
            workspace_manifest,
            patterns,
            root_identity=root_identity,
            output_boundary=output_boundary,
            inputs_boundary=inputs_boundary,
            capture_outcome_bytes=capture_outcome_bytes,
        )
        with _classify_invalid_output("sealed_draft_stability"):
            _assert_snapshot_paths(initial)
            final_outcome = _lstat(
                workspace_root / "output/job_outcome.draft.json"
            )
            if _fingerprint(final_outcome) != initial.leaf_fingerprint:
                raise _InvalidOutput
    except _MissingOutcome:
        missing = True
        final_outcome_state = "missing"
        failure_category = "final_outcome_missing"
    except _ClassifiedInvalidOutput as exc:
        invalid = True
        failure_category = exc.category
        diagnostic_reason = exc.diagnostic_reason
        schema_errors = exc.schema_errors
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        invalid = True

    # Raise outside the handler so an Agent-controlled parser/filesystem
    # exception is not retained as ``__context__`` on the public failure.
    if missing:
        _log_output_rejection(
            workspace_root,
            job,
            code=ErrorCode.OUTCOME_MISSING,
            failure_category=failure_category,
            final_outcome_state=final_outcome_state,
            final_outcome_bytes=final_outcome_bytes,
        )
        raise runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_MISSING,
            message="Agent outcome file is missing.",
        ) from None
    if invalid:
        _log_output_rejection(
            workspace_root,
            job,
            code=ErrorCode.OUTCOME_INVALID,
            failure_category=failure_category,
            final_outcome_state=final_outcome_state,
            final_outcome_bytes=final_outcome_bytes,
            diagnostic_reason=diagnostic_reason,
            schema_errors=schema_errors,
        )
        failure = runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_INVALID,
            message="Agent outcome validation failed.",
        ).failure
        raise RejectedAgentOutputError(
            failure,
            failure_category=failure_category,
            raw_outcome_bytes=raw_outcome_bytes,
        ) from None
    assert validated is not None
    return validated


__all__ = [
    "ValidatedAgentDraft",
    "ValidatedAgentOutput",
    "ValidatedProposalResource",
    "RejectedAgentOutputError",
    "read_agent_output",
]
