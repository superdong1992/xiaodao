"""Manifest-only binding of broker requests to one immutable Job Workspace."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts import (
    ArtifactKind,
    LogparseRunMetadata,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .outputs import ControlledRun, inspect_existing_run
from .paths import resolve_workspace_path


_MAX_MANIFEST_BYTES = 2_000_000
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BoundAttachment:
    entry: WorkspaceAttachmentInput
    path: Path


@dataclass(frozen=True, slots=True)
class BoundLogparseRun:
    entry: WorkspaceArtifactInput
    metadata: LogparseRunMetadata
    run: ControlledRun


def _plain_read_only_file(
    path: Path,
    *,
    maximum_bytes: int | None = None,
    allow_hardlinks: bool = False,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("Workspace input file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (not allow_hardlinks and metadata.st_nlink != 1)
        or metadata.st_mode & 0o222
        or (maximum_bytes is not None and metadata.st_size > maximum_bytes)
    ):
        raise ValueError("Workspace input must be a bounded read-only plain file")
    return metadata


def _sha256_file(path: Path, *, allow_hardlinks: bool = False) -> tuple[int, str]:
    before = _plain_read_only_file(path, allow_hardlinks=allow_hardlinks)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    after = _plain_read_only_file(path, allow_hardlinks=allow_hardlinks)
    stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        raise ValueError("Workspace input changed while it was verified")
    return size, digest.hexdigest()


def _require_read_only_tree(root: Path) -> None:
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names.sort()
            file_names.sort()
            directory_path = Path(directory)
            if directory_path.lstat().st_mode & 0o222:
                raise ValueError("materialized LOGPARSE_RUN directories must be read-only")
            for name in directory_names:
                path = directory_path / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o222:
                    raise ValueError("materialized LOGPARSE_RUN directories must be read-only")
            for name in file_names:
                path = directory_path / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o222:
                    raise ValueError("materialized LOGPARSE_RUN files must be read-only")
    except OSError as exc:
        raise ValueError("materialized LOGPARSE_RUN cannot be verified as read-only") from exc


def load_workspace_manifest(
    workspace_root: Path,
    expected: WorkspaceInputManifest,
) -> WorkspaceInputManifest:
    """Read only the fixed manifest path and prove it matches Runtime's object."""

    path = resolve_workspace_path(
        workspace_root,
        "inputs/manifest.json",
        must_exist=True,
    )
    metadata = _plain_read_only_file(path, maximum_bytes=_MAX_MANIFEST_BYTES)
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError("Workspace input manifest changed while it was read")
    manifest = parse_canonical_json_bytes(payload, WorkspaceInputManifest)
    if manifest != expected or payload != canonical_json_bytes(expected):
        raise ValueError("Workspace input manifest differs from the Runtime binding")
    return manifest


def has_logparse_run(manifest: WorkspaceInputManifest) -> bool:
    return any(
        isinstance(entry, WorkspaceArtifactInput)
        and entry.artifact_kind is ArtifactKind.LOGPARSE_RUN
        for entry in manifest.entries
    )


def bind_attachment(
    workspace_root: Path,
    manifest: WorkspaceInputManifest,
    attachment_id: str,
) -> BoundAttachment:
    matches = [
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceAttachmentInput)
        and entry.resource_id == attachment_id
    ]
    if len(matches) != 1:
        raise ValueError("parse request attachment is not fixed by this Workspace manifest")
    entry = matches[0]
    path = resolve_workspace_path(workspace_root, entry.relative_path, must_exist=True)
    # Workspace inputs are isolated copies, so the Logparse boundary requires
    # a single-link file as well as the frozen mode, size, hash, and bytes.
    size, digest = _sha256_file(path)
    if size != entry.size or digest != entry.sha256:
        raise ValueError("materialized Attachment differs from its frozen manifest entry")
    return BoundAttachment(entry=entry, path=path)


def bind_logparse_run(
    workspace_root: Path,
    manifest: WorkspaceInputManifest,
    artifact_id: str,
) -> BoundLogparseRun:
    matches = [
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceArtifactInput)
        and entry.resource_id == artifact_id
        and entry.artifact_kind is ArtifactKind.LOGPARSE_RUN
    ]
    if len(matches) != 1:
        raise ValueError("target request LOGPARSE_RUN is not fixed by this Workspace manifest")
    entry = matches[0]
    metadata = entry.metadata
    if not isinstance(metadata, LogparseRunMetadata):
        raise ValueError("LOGPARSE_RUN metadata does not use the S00 discriminated branch")
    if (
        manifest.logparse_tool_ref is None
        or manifest.logparse_product is None
        or metadata.logparse_version_ref != manifest.logparse_tool_ref
        or metadata.parse_parameters.product != manifest.logparse_product
    ):
        raise ValueError("LOGPARSE_RUN does not match the Job's fixed logparse bindings")
    source_attachments = [
        candidate
        for candidate in manifest.entries
        if isinstance(candidate, WorkspaceAttachmentInput)
        and candidate.resource_id == metadata.source_attachment_id
    ]
    if (
        len(source_attachments) != 1
        or source_attachments[0].sha256 != metadata.source_attachment_sha256
    ):
        raise ValueError("LOGPARSE_RUN source Attachment is not fixed by this Workspace")
    bind_attachment(workspace_root, manifest, metadata.source_attachment_id)
    root = resolve_workspace_path(workspace_root, entry.relative_path, must_exist=True)
    _require_read_only_tree(root)
    run = inspect_existing_run(
        root,
        product=manifest.logparse_product,
        expected_parse_manifest_relative_path=metadata.parse_manifest_relative_path,
        expected_size=entry.size,
        expected_sha256=entry.sha256,
    )
    return BoundLogparseRun(entry=entry, metadata=metadata, run=run)


__all__ = [
    "BoundAttachment",
    "BoundLogparseRun",
    "bind_attachment",
    "bind_logparse_run",
    "has_logparse_run",
    "load_workspace_manifest",
]
