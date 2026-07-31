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
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

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


_READ_CHUNK_BYTES = 64 * 1024
_Draft: TypeAlias = AgentEvidenceProposalDraft | AgentArtifactProposalDraft


class _MissingOutcome(Exception):
    pass


class _InvalidOutput(Exception):
    pass


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


def _read_regular_file(
    path: Path,
    *,
    patterns: tuple[bytes, ...] = (),
    capture: bool = False,
    missing_outcome: bool = False,
) -> tuple[int, str, bytes | None]:
    """Open an ordinary single-link file without following the leaf link."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_outcome:
            raise _MissingOutcome from None
        raise _InvalidOutput from None
    except OSError:
        raise _InvalidOutput from None

    size = 0
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    scanner = _ExactSecretScanner(patterns)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _InvalidOutput
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while chunk := stream.read(_READ_CHUNK_BYTES):
                if scanner.feed(chunk):
                    raise _InvalidOutput
                size += len(chunk)
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
    except _InvalidOutput:
        raise
    except OSError:
        raise _InvalidOutput from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return size, digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def _scan_relative_path(relative_path: str, patterns: tuple[bytes, ...]) -> None:
    try:
        encoded = relative_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _InvalidOutput from None
    _scan_bytes(encoded, patterns)


def _inspect_tree(
    root: Path,
    *,
    workspace_root: Path,
    patterns: tuple[bytes, ...],
) -> tuple[int, str, TreeManifest]:
    metadata = _lstat(root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _InvalidOutput

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
            if stat.S_ISLNK(entry_metadata.st_mode):
                raise _InvalidOutput
            tree_relative = f"{tree_prefix}/{entry.name}" if tree_prefix else entry.name
            workspace_relative = (Path(root).relative_to(workspace_root) / tree_relative).as_posix()
            _scan_relative_path(workspace_relative, patterns)
            if stat.S_ISDIR(entry_metadata.st_mode):
                child_directories.append((Path(entry.path), tree_relative))
            elif stat.S_ISREG(entry_metadata.st_mode) and entry_metadata.st_nlink == 1:
                files.append((tree_relative, Path(entry.path)))
            else:
                raise _InvalidOutput
        pending.extend(reversed(child_directories))

    manifest_entries: list[TreeManifestEntry] = []
    for relative_path, path in sorted(files, key=lambda item: item[0]):
        size, sha256, _ = _read_regular_file(path, patterns=patterns)
        manifest_entries.append(
            TreeManifestEntry(path=relative_path, size=size, sha256=sha256)
        )
    manifest = TreeManifest(version=1, entries=manifest_entries)
    size = sum(entry.size for entry in manifest.entries)
    return size, bytes_sha256(canonical_json_bytes(manifest)), manifest


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
) -> ValidatedAgentOutput:
    outcome_relative_path = "output/job_outcome.json"
    outcome_path = _validate_parent_directories(
        workspace_root,
        outcome_relative_path,
        missing_outcome=True,
    )
    _, _, raw_outcome_bytes = _read_regular_file(
        outcome_path,
        capture=True,
        missing_outcome=True,
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
            size, sha256, content = _read_regular_file(
                path,
                patterns=patterns,
                capture=capture,
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
    workspace_root: Path,
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
        validated = _read_validated_output(
            Path(workspace_root),
            job,
            workspace_manifest,
            patterns,
        )
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
