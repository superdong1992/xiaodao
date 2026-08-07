"""Shared boundary for JSON authored by an Agent inside one Workspace.

Agent-facing tools accept unambiguous JSON drafts, validate the surface-specific
schema, and replace those drafts with the exact V1 Canonical JSON bytes before
another component consumes or hashes them.  This keeps formatting correctness
out of model behaviour while retaining strict rejection of ambiguous JSON.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from problem_locator.contracts import (
    InvalidJsonBytesError,
    bytes_sha256,
    canonical_json_bytes,
)


class AgentJsonSurface(StrEnum):
    """The complete set of Canonical JSON files authored by an Agent."""

    JOB_OUTCOME = "job_outcome"
    LOGPARSE_REQUEST = "logparse_request"
    RESULT_ARCHIVE_REQUEST = "result_archive_request"
    USER_RESULT = "user_result"


AGENT_JSON_SURFACE_OWNERS: Mapping[AgentJsonSurface, str] = MappingProxyType(
    {
        AgentJsonSurface.JOB_OUTCOME: "problem-locator-finalize-outcome",
        AgentJsonSurface.LOGPARSE_REQUEST: "problem-locator-logparse",
        AgentJsonSurface.RESULT_ARCHIVE_REQUEST: "problem-locator-pack-result",
        AgentJsonSurface.USER_RESULT: "problem-locator-finalize-outcome",
    }
)


@dataclass(frozen=True, slots=True)
class AgentJsonDocument:
    """One unambiguous JSON value and its recursively canonical encoding."""

    value: Any
    canonical_bytes: bytes

    @property
    def size(self) -> int:
        return len(self.canonical_bytes)

    @property
    def sha256(self) -> str:
        return bytes_sha256(self.canonical_bytes)


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is forbidden")
        result[key] = value
    return result


def parse_agent_json_bytes(data: bytes) -> AgentJsonDocument:
    """Parse valid, unambiguous UTF-8 JSON without requiring canonical spelling."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise InvalidJsonBytesError("a UTF-8 BOM is forbidden")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidJsonBytesError(
            "invalid UTF-8 JSON bytes: "
            f"decode failed at byte {exc.start}: {exc.reason}"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise InvalidJsonBytesError(
            "invalid UTF-8 JSON bytes: "
            f"JSON syntax error at line {exc.lineno} column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    except ValueError as exc:
        raise InvalidJsonBytesError(str(exc)) from exc
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise InvalidJsonBytesError("JSON value cannot be canonically encoded") from exc
    return AgentJsonDocument(value=value, canonical_bytes=canonical)


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    stable = (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    return stable if os.name == "nt" else stable + (metadata.st_ctime_ns,)


def read_agent_json_file(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, AgentJsonDocument]:
    """Read one bounded ordinary file and reject path or content races."""

    target = Path(path)
    named = target.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or named.st_size > max_bytes
    ):
        raise ValueError("Agent JSON file is not a bounded ordinary file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("Agent JSON file changed while it was opened")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, (max_bytes + 1) - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError("Agent JSON file is too large")
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final_named = target.stat(follow_symlinks=False)
    if (
        _metadata_fingerprint(final) != _metadata_fingerprint(opened)
        or _metadata_fingerprint(final_named) != _metadata_fingerprint(named)
        or len(data) != final.st_size
    ):
        raise ValueError("Agent JSON file changed while it was read")
    raw = bytes(data)
    return raw, parse_agent_json_bytes(raw)


def _sync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace_agent_json(path: Path, data: bytes) -> None:
    """Atomically publish already validated bytes at one fixed file path."""

    target = Path(path)
    parent = target.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    try:
        target_metadata = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        target_metadata = None
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("Agent JSON parent is not a directory")
    if target_metadata is not None and (
        not stat.S_ISREG(target_metadata.st_mode) or target_metadata.st_nlink != 1
    ):
        raise ValueError("Agent JSON target is not an ordinary file")
    temporary: Path | None = None
    try:
        for _attempt in range(16):
            candidate = parent / f".{target.name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                continue
            temporary = candidate
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            _sync_directory(parent)
            if target.read_bytes() != data:
                raise OSError("Agent JSON replacement bytes changed")
            return
        raise OSError("cannot reserve an Agent JSON temporary file")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def normalize_agent_json_file(
    path: Path,
    *,
    surface: AgentJsonSurface,
    max_bytes: int,
    validate: Callable[[Any], object],
) -> AgentJsonDocument:
    """Validate and canonically replace one registered Agent JSON surface."""

    if surface not in AGENT_JSON_SURFACE_OWNERS:
        raise ValueError("Agent JSON surface has no normalization owner")
    raw, document = read_agent_json_file(path, max_bytes=max_bytes)
    validate(document.value)
    if raw != document.canonical_bytes:
        atomic_replace_agent_json(path, document.canonical_bytes)
    return document


__all__ = [
    "AGENT_JSON_SURFACE_OWNERS",
    "AgentJsonDocument",
    "AgentJsonSurface",
    "atomic_replace_agent_json",
    "normalize_agent_json_file",
    "parse_agent_json_bytes",
    "read_agent_json_file",
]
