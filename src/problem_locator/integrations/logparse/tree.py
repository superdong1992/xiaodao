"""Strict tree hashing for controlled logparse output directories."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

from problem_locator.contracts import TreeManifest, TreeManifestEntry, canonical_json_bytes

_CHUNK_BYTES = 1024 * 1024


def _invalid() -> ValueError:
    return ValueError("controlled logparse output tree is invalid")


def _file_sha256(path: Path, *, check_abort: Callable[[], None] | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    before = path.stat(follow_symlinks=False)
    with path.open("rb") as stream:
        while True:
            if check_abort is not None:
                check_abort()
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise _invalid()
    return size, digest.hexdigest()


def build_tree_manifest(
    root: Path, *, check_abort: Callable[[], None] | None = None,
) -> tuple[TreeManifest, int, str]:
    """Hash a directory using the exact S00 ``TreeManifest`` semantics."""

    if check_abort is not None:
        check_abort()
    supplied_root = Path(root)
    try:
        supplied_metadata = supplied_root.lstat()
        root = supplied_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _invalid() from exc
    if stat.S_ISLNK(supplied_metadata.st_mode) or not root.is_dir():
        raise _invalid()

    entries: list[TreeManifestEntry] = []
    seen_inodes: set[tuple[int, int]] = set()
    pending = [root]
    try:
        while pending:
            if check_abort is not None:
                check_abort()
            directory = pending.pop()
            children = []
            with os.scandir(directory) as scanned:
                for child in scanned:
                    if check_abort is not None:
                        check_abort()
                    children.append(child)
            children.sort(key=lambda item: item.name)
            for child in children:
                if check_abort is not None:
                    check_abort()
                child_path = Path(child.path)
                if child.is_symlink():
                    raise _invalid()
                # Windows DirEntry metadata can report zeroed identity and
                # link-count fields.  A direct stat provides the stable
                # values needed for hard-link rejection and tree hashing.
                metadata = (
                    child_path.stat(follow_symlinks=False)
                    if os.name == "nt"
                    else child.stat(follow_symlinks=False)
                )
                mode = metadata.st_mode
                if stat.S_ISDIR(mode):
                    pending.append(child_path)
                    continue
                if not stat.S_ISREG(mode) or metadata.st_nlink != 1:
                    raise _invalid()
                inode = (metadata.st_dev, metadata.st_ino)
                if inode in seen_inodes:
                    raise _invalid()
                seen_inodes.add(inode)
                relative = child_path.relative_to(root).as_posix()
                size, digest = _file_sha256(child_path, check_abort=check_abort)
                if size != metadata.st_size:
                    raise _invalid()
                entries.append(
                    TreeManifestEntry(path=relative, size=size, sha256=digest)
                )
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _invalid() from exc

    entries.sort(key=lambda entry: entry.path)
    if check_abort is not None:
        check_abort()
    manifest = TreeManifest(version=1, entries=entries)
    size = sum(entry.size for entry in entries)
    digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return manifest, size, digest


__all__ = ["build_tree_manifest"]
