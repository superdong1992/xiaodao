"""Atomic creation of the Runtime-reserved one-shot parse claim."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

from problem_locator.contracts import LogparseParseClaim, canonical_json_bytes


FaultPoint = Callable[[str], None]


def _no_fault(_name: str) -> None:
    return


def _plain_directory(path: Path, *, create: bool) -> Path:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError("logparse claim directory is unavailable") from exc
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("logparse claim directory is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("logparse claim directory must be a plain directory")
    return resolved


def create_parse_claim(
    workspace_root: Path,
    claim: LogparseParseClaim,
    *,
    fault_point: FaultPoint = _no_fault,
) -> Path:
    """Create the sole S00 claim with create-new semantics before process start."""

    supplied_root = Path(workspace_root)
    try:
        lexical_root = Path(os.path.abspath(supplied_root))
        if any(path.is_symlink() for path in (lexical_root, *lexical_root.parents)):
            raise ValueError("Job Workspace path cannot contain symbolic links")
        supplied_metadata = supplied_root.lstat()
        root = supplied_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Job Workspace is unavailable") from exc
    if stat.S_ISLNK(supplied_metadata.st_mode) or not root.is_dir():
        raise ValueError("Job Workspace must be a plain directory")
    runtime_root = _plain_directory(root / "runtime", create=True)
    state_root = _plain_directory(runtime_root / "tool-state", create=True)
    target = state_root / "logparse-parse.claim"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ELOOP}:
            raise FileExistsError("a logparse parse attempt already exists for this Job") from exc
        raise ValueError("the logparse parse claim cannot be reserved") from exc

    payload = canonical_json_bytes(claim)
    try:
        fault_point("claim_reserved")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("parse claim write made no progress")
            written += count
        os.fsync(descriptor)
        fault_point("claim_written")
    finally:
        os.close(descriptor)
    return target


__all__ = ["FaultPoint", "create_parse_claim"]
