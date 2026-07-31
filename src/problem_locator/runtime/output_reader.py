"""Validate the only business output accepted from an Agent workspace.

The reader is deliberately a pre-staging boundary.  It returns proposal paths
only after the complete Agent outcome, every declared proposal resource, and a
possible USER_RESULT have passed the frozen S00 validators.  Callers therefore
cannot accidentally stage a prefix of an otherwise invalid Agent response.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, TypeAlias

from problem_locator.contracts.enums import ArtifactKind, ErrorCode, ExecutionStage, ResourceKind
from problem_locator.contracts.models import (
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    AgentJobOutcome,
    Job,
    TreeManifest,
    TreeManifestEntry,
    UserResultPayload,
    WorkspaceInputManifest,
)
from problem_locator.contracts.outcomes import (
    validate_outcome_for_job,
    validate_user_result_for_outcome,
)
from problem_locator.contracts.serialization import (
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .failures import runtime_failure
from .workspace import PreparedWorkspace


_READ_CHUNK_BYTES = 64 * 1024
_Draft: TypeAlias = AgentEvidenceProposalDraft | AgentArtifactProposalDraft


class _MissingOutcome(Exception):
    pass


class _InvalidOutput(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    root: Path
    root_identity: tuple[int, int]
    output_identity: tuple[int, int]
    parent_identities: tuple[tuple[str, int, int], ...]
    leaf_identity: tuple[int, int]
    leaf_fingerprint: tuple[int, ...]


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
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(metadata, "st_file_attributes", 0) & flag)


def _safe_relative_parts(relative_path: str) -> tuple[str, ...]:
    parts = tuple(relative_path.split("/"))
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != "output"
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
    output_identity: tuple[int, int] | None = None,
) -> _SourceSnapshot:
    parts = _safe_relative_parts(relative_path)
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
    if not parents or parents[0][0] != "output":
        raise _InvalidOutput
    if output_identity is not None and parents[0][1:] != output_identity:
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
        output_identity=parents[0][1:],
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
    parts = _safe_relative_parts(relative_path)
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
    parts = _safe_relative_parts(relative_path)
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
    output_identity: tuple[int, int] | None = None,
    expected_parent_identities: dict[str, tuple[int, int]] | None = None,
) -> tuple[int, str, bytes | None, _SourceSnapshot]:
    snapshot = _snapshot_source(
        root,
        relative_path,
        root_identity=root_identity,
        output_identity=output_identity,
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
        size, sha256, content = _read_descriptor(
            descriptor,
            patterns=patterns,
            capture=capture,
        )
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
) -> list[TreeManifestEntry]:
    """Inspect one directory without resolving a child through ambient paths."""

    try:
        directory_metadata = os.fstat(directory_descriptor)
    except OSError:
        raise _InvalidOutput from None
    _assert_directory(directory_metadata, device=workspace_device)
    directory_fingerprint = _fingerprint(directory_metadata)
    names = _scandir_names(directory_descriptor)
    manifest_entries: list[TreeManifestEntry] = []
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
                manifest_entries.extend(
                    _inspect_tree_descriptor(
                        child_descriptor,
                        tree_prefix=tree_relative,
                        workspace_relative_root=workspace_relative_root,
                        workspace_device=workspace_device,
                        patterns=patterns,
                    )
                )
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
            )
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
    if _scandir_names(directory_descriptor) != names:
        raise _InvalidOutput
    try:
        if _fingerprint(os.fstat(directory_descriptor)) != directory_fingerprint:
            raise _InvalidOutput
    except OSError:
        raise _InvalidOutput from None
    return manifest_entries


def _inspect_tree(
    root: Path,
    *,
    workspace_root: Path,
    patterns: tuple[bytes, ...],
    root_identity: tuple[int, int] | None = None,
    output_identity: tuple[int, int] | None = None,
) -> tuple[int, str, TreeManifest]:
    workspace_metadata = _lstat(workspace_root)
    workspace_device = workspace_metadata.st_dev
    tree_relative_root = root.relative_to(workspace_root).as_posix()
    tree_snapshot = _snapshot_source(
        workspace_root,
        tree_relative_root,
        root_identity=root_identity,
        output_identity=output_identity,
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
            manifest_entries = _inspect_tree_descriptor(
                tree_descriptor,
                tree_prefix="",
                workspace_relative_root=tree_relative_root,
                workspace_device=workspace_device,
                patterns=patterns,
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
        files: list[tuple[str, Path]] = []
        while pending:
            directory, tree_prefix = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError:
                raise _InvalidOutput from None
            child_directories: list[tuple[Path, str]] = []
            for entry in entries:
                try:
                    entry.name.encode("utf-8", errors="strict")
                    entry_metadata = entry.stat(follow_symlinks=False)
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
                    files.append((tree_relative, Path(entry.path)))
                else:
                    raise _InvalidOutput
            pending.extend(reversed(child_directories))

        manifest_entries = []
        for relative_path, path in sorted(files, key=lambda item: item[0]):
            workspace_relative = path.relative_to(workspace_root).as_posix()
            size, sha256, _, _ = _read_frozen_relative_file(
                workspace_root,
                workspace_relative,
                patterns=patterns,
                root_identity=tree_snapshot.root_identity,
                output_identity=tree_snapshot.output_identity,
                expected_parent_identities=directory_identities,
            )
            manifest_entries.append(
                TreeManifestEntry(path=relative_path, size=size, sha256=sha256)
            )
        for relative, expected in directory_fingerprints.items():
            if _fingerprint(_lstat(workspace_root / relative)) != expected:
                raise _InvalidOutput
    manifest = TreeManifest(version=1, entries=manifest_entries)
    size = sum(entry.size for entry in manifest.entries)
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
        root_identity=snapshot.root_identity,
        output_identity=snapshot.output_identity,
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


def _proposal_drafts(outcome: AgentJobOutcome) -> tuple[_Draft, ...]:
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


def _read_validated_output(
    workspace_root: Path,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    patterns: tuple[bytes, ...],
    *,
    root_identity: tuple[int, int],
    output_identity: tuple[int, int],
) -> ValidatedAgentOutput:
    outcome_relative_path = "output/job_outcome.json"
    outcome_path = _validate_parent_directories(
        workspace_root,
        outcome_relative_path,
        missing_outcome=True,
    )
    _, _, raw_outcome_bytes, _ = _read_frozen_relative_file(
        workspace_root,
        outcome_relative_path,
        capture=True,
        root_identity=root_identity,
        output_identity=output_identity,
    )
    assert raw_outcome_bytes is not None
    outcome = parse_canonical_json_bytes(
        raw_outcome_bytes,
        model_type=AgentJobOutcome,
    )
    validate_outcome_for_job(job, outcome, workspace_manifest)
    outcome_bytes = canonical_json_bytes(outcome)
    _scan_bytes(outcome_bytes, patterns)

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
    for draft in drafts:
        relative_path = draft.workspace_relative_path
        if relative_path is None:
            continue
        required_prefix = f"output/proposals/{draft.proposal_key}/"
        if not relative_path.startswith(required_prefix):
            raise _InvalidOutput
        _scan_relative_path(relative_path, patterns)
        path = _validate_parent_directories(workspace_root, relative_path)

        if isinstance(draft, AgentArtifactProposalDraft):
            resource_kind = draft.resource_kind
            capture = draft.artifact_kind is ArtifactKind.USER_RESULT
        else:
            resource_kind = ResourceKind.FILE
            capture = False

        if resource_kind is ResourceKind.FILE:
            size, sha256, content, source_snapshot = _read_frozen_relative_file(
                workspace_root,
                relative_path,
                patterns=patterns,
                capture=capture,
                root_identity=root_identity,
                output_identity=output_identity,
            )
            tree_manifest = None
            if capture:
                assert content is not None
                user_result_bytes = content
        else:
            size, sha256, tree_manifest = _inspect_tree(
                path,
                workspace_root=workspace_root,
                patterns=patterns,
                root_identity=root_identity,
                output_identity=output_identity,
            )
            source_snapshot = _snapshot_source(
                workspace_root,
                relative_path,
                root_identity=root_identity,
                output_identity=output_identity,
            )
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

    user_result = None
    if user_result_bytes is not None:
        user_result = validate_user_result_for_outcome(job, outcome, user_result_bytes)
    return ValidatedAgentOutput(
        outcome=outcome,
        canonical_bytes=outcome_bytes,
        proposal_resources=tuple(resources),
        user_result=user_result,
    )


def read_agent_output(
    workspace: PreparedWorkspace | Path,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    *,
    secrets: Iterable[bytes | str] = (),
) -> ValidatedAgentOutput:
    """Read and validate ``output/job_outcome.json`` and all proposal content.

    ``.part`` files are never considered.  Any Agent-controlled invalidity is
    collapsed to the frozen OUTCOME_INVALID failure without retaining an
    exception cause, path, content, endpoint, or token in the error surface.
    """

    patterns = _normalize_secrets(secrets)
    missing = False
    invalid = False
    validated: ValidatedAgentOutput | None = None
    try:
        if isinstance(workspace, PreparedWorkspace):
            workspace_root = workspace.root
            root_identity = (workspace.root_device, workspace.root_inode)
            output_identity = (workspace.output_device, workspace.output_inode)
            if workspace.manifest != workspace_manifest:
                raise _InvalidOutput
        else:
            workspace_root = Path(workspace)
            root_metadata = _lstat(workspace_root)
            _assert_directory(root_metadata, device=root_metadata.st_dev)
            root_identity = _identity(root_metadata)
            output_metadata = _lstat(workspace_root / "output", missing_outcome=True)
            _assert_directory(output_metadata, device=root_metadata.st_dev)
            output_identity = _identity(output_metadata)
        _lstat(
            workspace_root / "output/job_outcome.json",
            missing_outcome=True,
        )
        initial = _snapshot_source(
            workspace_root,
            "output/job_outcome.json",
            root_identity=root_identity,
            output_identity=output_identity,
        )
        validated = _read_validated_output(
            workspace_root,
            job,
            workspace_manifest,
            patterns,
            root_identity=root_identity,
            output_identity=output_identity,
        )
        _assert_snapshot_paths(initial)
        final_outcome = _lstat(workspace_root / "output/job_outcome.json")
        if _fingerprint(final_outcome) != initial.leaf_fingerprint:
            raise _InvalidOutput
    except _MissingOutcome:
        missing = True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        invalid = True

    # Raise outside the handler so an Agent-controlled parser/filesystem
    # exception is not retained as ``__context__`` on the public failure.
    if missing:
        raise runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_MISSING,
            message="Agent outcome file is missing.",
        ) from None
    if invalid:
        raise runtime_failure(
            stage=ExecutionStage.OUTCOME_VALIDATE,
            code=ErrorCode.OUTCOME_INVALID,
            message="Agent outcome validation failed.",
        ) from None
    assert validated is not None
    return validated


__all__ = [
    "ValidatedAgentOutput",
    "ValidatedProposalResource",
    "read_agent_output",
]
