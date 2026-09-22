"""Stream verified Logparse files into the Generic Skill's read-only input tree."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import CancellationSignal, ErrorCode, ExecutionStage, canonical_json_bytes
from .failures import runtime_failure
from .workspace import PreparedWorkspace, _atomic_write, _set_inputs_read_only


def _ordinary_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (not relative or parts.is_absolute() or "\\" in relative
            or parts.as_posix() != relative or any(part in {".", ".."} for part in parts.parts)):
        raise ValueError("invalid generic log path")
    current = root
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid generic log root")
    for part in parts.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("generic log path contains a link")
    current.resolve(strict=True).relative_to(root.resolve(strict=True))
    return current


def _stream_verified(source: Path, size: int, sha256: str, *, destination: Path | None,
                     cancellation: CancellationSignal) -> None:
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
        raise ValueError("generic log is not the frozen ordinary file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("generic log changed before read")
        target = None if destination is None else destination.open("xb")
        try:
            digest, total = hashlib.sha256(), 0
            while True:
                if cancellation.is_cancelled():
                    raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
                        code=ErrorCode.BACKEND_CANCELLED, message="日志准备已取消。")
                chunk = stream.read(min(1024 * 1024, size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise ValueError("generic log grew during read")
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            after = os.fstat(stream.fileno())
            named = source.stat(follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
            if (total != size or digest.hexdigest() != sha256
                    or any(getattr(before, key) != getattr(after, key)
                           or getattr(after, key) != getattr(named, key) for key in fields)):
                raise ValueError("generic log content changed")
            if target is not None:
                target.flush()
                os.fsync(target.fileno())
        finally:
            if target is not None:
                target.close()


def freeze_generic_logs(workspace: PreparedWorkspace, *, controlled_root: Path,
                        parsed: dict[str, Any], cancellation: CancellationSignal) -> bytes:
    """Return the exact input receipt, without ever buffering a complete log file."""
    inputs = workspace.root / "inputs"
    before = inputs.stat(follow_symlinks=False)
    if inputs.is_symlink() or (before.st_dev, before.st_ino) != (workspace.inputs_device, workspace.inputs_inode):
        raise ValueError("generic input directory changed")
    inputs.chmod(0o755)
    try:
        target_root = inputs / "generic-logs"
        target_root.mkdir(mode=0o700)
        rows = []
        for ordinal, entry in enumerate(parsed["logs"], start=1):
            source = _ordinary_path(controlled_root, entry["relative_path"])
            relative = f"inputs/generic-logs/log-{ordinal:06d}.log"
            _stream_verified(source, entry["size"], entry["sha256"],
                destination=workspace.root / relative, cancellation=cancellation)
            rows.append({**entry, "log_path": relative})
        payload = canonical_json_bytes({**parsed, "logs": rows})
        if len(payload) > 2_000_000:
            raise ValueError("generic log manifest exceeds its byte budget")
        _atomic_write(inputs / "generic_logs.json", payload)
        return payload
    finally:
        _set_inputs_read_only(inputs)


def verify_generic_logs(workspace: PreparedWorkspace, *, receipt: bytes,
                        parsed: dict[str, Any], cancellation: CancellationSignal) -> None:
    """Reject any input drift before accepting a black-box Skill's report."""
    if cancellation.is_cancelled():
        raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.BACKEND_CANCELLED, message="日志校验已取消。")
    manifest = _ordinary_path(workspace.root, "inputs/generic_logs.json")
    if manifest.stat().st_size != len(receipt) or manifest.read_bytes() != receipt:
        raise ValueError("generic log receipt changed")
    for ordinal, entry in enumerate(parsed["logs"], start=1):
        path = _ordinary_path(workspace.root, f"inputs/generic-logs/log-{ordinal:06d}.log")
        _stream_verified(path, entry["size"], entry["sha256"],
            destination=None, cancellation=cancellation)
