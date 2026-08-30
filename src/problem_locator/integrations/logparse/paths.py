"""Workspace-relative path validation shared by broker and Agent stub."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path, PurePosixPath

from pydantic import TypeAdapter

from problem_locator.contracts import RelativePosixPath


_RELATIVE_PATH = TypeAdapter(RelativePosixPath)


def validate_relative_path(value: str) -> PurePosixPath:
    _RELATIVE_PATH.validate_python(value)
    if (
        value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("workspace path is not canonical")
    return PurePosixPath(value)


def resolve_workspace_path(
    workspace_root: Path,
    relative_path: str,
    *,
    must_exist: bool,
) -> Path:
    supplied_root = Path(workspace_root)
    try:
        lexical_root = Path(os.path.abspath(supplied_root))
        if any(path.is_symlink() for path in (lexical_root, *lexical_root.parents)):
            raise ValueError("workspace root path cannot contain symbolic links")
        supplied_metadata = supplied_root.lstat()
        root = supplied_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace root is invalid") from exc
    if stat.S_ISLNK(supplied_metadata.st_mode) or not root.is_dir():
        raise ValueError("workspace root is invalid")
    relative = validate_relative_path(relative_path)
    candidate = root.joinpath(*relative.parts)
    current = root
    try:
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if must_exist:
                    raise
                break
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("workspace paths cannot contain symbolic links")
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace path is unavailable") from exc
    if os.path.commonpath((os.fspath(root), os.fspath(resolved))) != os.fspath(root):
        raise ValueError("workspace path escapes the current root")
    return resolved


def validate_proposal_io_paths(
    request_path: str,
    result_path: str,
) -> str:
    request = validate_relative_path(request_path)
    result = validate_relative_path(result_path)
    request_parts = request.parts
    result_parts = result.parts
    if (
        len(request_parts) != 4
        or request_parts[:2] != ("output", "proposals")
        or request_parts[3] != "request.json"
        or len(result_parts) != 4
        or result_parts[:2] != ("output", "proposals")
        or result_parts[3] != "target_logs.json"
        or request_parts[2] != result_parts[2]
    ):
        raise ValueError("broker I/O must use one output/proposals/<key> directory")
    return request_parts[2]


def atomic_write_broker_result(target: Path, payload: bytes) -> None:
    """Publish one broker result with the same semantics for both callers."""

    parent = target.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("broker result parent is invalid")
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        target_metadata = None
    if target_metadata is not None and not stat.S_ISREG(target_metadata.st_mode):
        raise ValueError("broker result target is invalid")

    temporary: Path | None = None
    try:
        for _attempt in range(16):
            candidate = parent / f".target_logs.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            temporary = candidate
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            return
        raise OSError("cannot reserve a broker result temporary file")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "atomic_write_broker_result",
    "resolve_workspace_path",
    "validate_proposal_io_paths",
    "validate_relative_path",
]
