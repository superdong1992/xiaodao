"""Deterministic directory-resource inspection and copying."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts.models import TreeManifest, TreeManifestEntry
from problem_locator.contracts.serialization import canonical_json_bytes


_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TreeInspection:
    manifest: TreeManifest
    size: int
    sha256: str


def _scan_files(
    root: Path,
) -> tuple[list[Path], list[Path], dict[Path, os.stat_result]]:
    """Return ordinary files and directories after a no-links tree walk."""

    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("tree root must be a real directory")
    files: list[Path] = []
    directories: list[Path] = [root]
    scanned_metadata: dict[Path, os.stat_result] = {root: root_stat}
    pending = [root]
    while pending:
        directory = pending.pop()
        children = sorted(os.scandir(directory), key=lambda item: item.name)
        for child in children:
            child_path = Path(child.path)
            child_stat = child.stat(follow_symlinks=False)
            scanned_metadata[child_path] = child_stat
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError("directory resources cannot contain symbolic links")
            if stat.S_ISDIR(child_stat.st_mode):
                directories.append(child_path)
                pending.append(child_path)
            elif stat.S_ISREG(child_stat.st_mode):
                files.append(child_path)
            else:
                raise ValueError("directory resources may contain only regular files and directories")
    files.sort(key=lambda value: value.relative_to(root).as_posix())
    directories.sort(key=lambda value: value.relative_to(root).as_posix())
    file_parents = {parent for file in files for parent in file.parents if parent != root.parent}
    if any(directory != root and directory not in file_parents for directory in directories):
        raise ValueError("unrepresented empty directories are forbidden in directory resources")
    return files, directories, scanned_metadata


def _require_unchanged_metadata(
    expected: os.stat_result,
    observed: os.stat_result,
    *,
    label: str,
) -> None:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if any(getattr(expected, field) != getattr(observed, field) for field in fields):
        raise ValueError(f"{label} changed while the tree was inspected")


def _require_real_ancestors(root: Path, candidate: Path) -> None:
    current = root
    if not stat.S_ISDIR(current.lstat().st_mode):
        raise ValueError("tree root changed while it was inspected")
    relative = candidate.relative_to(root)
    for part in relative.parts:
        current = current / part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("tree directory changed while it was inspected")


def inspect_tree(
    root: Path,
    *,
    copy_to: Path | None = None,
    byte_limit: int | None = None,
    reject_hardlinks: bool = True,
) -> TreeInspection:
    """Build the frozen TreeManifest, optionally performing a controlled copy."""

    root = Path(os.path.abspath(root))
    files, directories, scanned_metadata = _scan_files(root)
    if copy_to is not None:
        copy_to = Path(copy_to)
        copy_to.mkdir(parents=True, exist_ok=False)
        for directory in directories:
            if directory == root:
                continue
            (copy_to / directory.relative_to(root)).mkdir(exist_ok=False)

    total_size = 0
    entries: list[TreeManifestEntry] = []
    for source in files:
        _require_real_ancestors(root, source.parent)
        before = source.stat(follow_symlinks=False)
        _require_unchanged_metadata(
            scanned_metadata[source],
            before,
            label="tree entry",
        )
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("tree entries must remain ordinary files")
        if reject_hardlinks and before.st_nlink != 1:
            raise ValueError("directory resources cannot contain hard-linked files")
        relative = source.relative_to(root).as_posix()
        digest = hashlib.sha256()
        observed = 0
        target_handle = None
        try:
            if copy_to is not None:
                target = copy_to / source.relative_to(root)
                target_handle = target.open("xb")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source, flags)
            try:
                opened = os.fstat(descriptor)
                _require_unchanged_metadata(before, opened, label="tree entry")
                while True:
                    chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    observed += len(chunk)
                    total_size += len(chunk)
                    if byte_limit is not None and total_size > byte_limit:
                        raise ValueError("directory resource exceeds its byte limit")
                    digest.update(chunk)
                    if target_handle is not None:
                        target_handle.write(chunk)
                after_descriptor = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if target_handle is not None:
                target_handle.flush()
        finally:
            if target_handle is not None:
                target_handle.close()
        after = source.stat(follow_symlinks=False)
        _require_unchanged_metadata(before, after_descriptor, label="tree entry")
        _require_unchanged_metadata(before, after, label="tree entry")
        if observed != before.st_size:
            raise ValueError("directory resource changed while it was inspected")
        entries.append(
            TreeManifestEntry(
                path=relative,
                size=observed,
                sha256=digest.hexdigest(),
            )
        )

    for directory in directories:
        _require_unchanged_metadata(
            scanned_metadata[directory],
            directory.lstat(),
            label="tree directory",
        )

    manifest = TreeManifest(version=1, entries=entries)
    manifest_hash = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return TreeInspection(manifest=manifest, size=total_size, sha256=manifest_hash)


def verify_tree(
    root: Path,
    *,
    expected_manifest: TreeManifest,
    expected_size: int,
    expected_sha256: str,
) -> TreeInspection:
    """Rebuild a formal tree and compare its complete immutable description."""

    observed = inspect_tree(root, reject_hardlinks=True)
    if observed.manifest != expected_manifest:
        raise ValueError("directory TreeManifest does not match the staged reference")
    if observed.size != expected_size:
        raise ValueError("directory resource size mismatch")
    if observed.sha256 != expected_sha256:
        raise ValueError("directory resource hash mismatch")
    return observed


__all__ = ["TreeInspection", "inspect_tree", "verify_tree"]
