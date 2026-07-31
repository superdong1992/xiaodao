"""Startup-time fingerprinting for the pinned logparse installation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

from problem_locator.contracts import (
    AssetKind,
    ResolvedAsset,
    VersionedRef,
    canonical_json_bytes,
)


_READ_CHUNK_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 10.0


def _resolved_file(value: str | os.PathLike[str], *, executable: bool = False) -> Path:
    text = os.fspath(value)
    candidate = Path(text)
    if executable and candidate.parent == Path(".") and not candidate.is_absolute():
        discovered = shutil.which(text)
        if discovered is None:
            raise ValueError("configured logparse executable is unavailable")
        candidate = Path(discovered)
    try:
        resolved = candidate.expanduser().resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise ValueError("configured logparse file is unavailable") from exc
    if resolved.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("configured logparse file must be a regular file")
    if executable and not os.access(resolved, os.X_OK):
        raise ValueError("configured logparse executable is not executable")
    return resolved


def _lexical_executable(value: str | os.PathLike[str]) -> Path:
    """Validate an executable while preserving its absolute launcher path."""

    text = os.fspath(value)
    candidate = Path(text).expanduser()
    if candidate.parent == Path(".") and not candidate.is_absolute():
        discovered = shutil.which(text)
        if discovered is None:
            raise ValueError("configured logparse executable is unavailable")
        candidate = Path(discovered)
    absolute = Path(os.path.abspath(candidate))
    _resolved_file(absolute, executable=True)
    return absolute


def _resolved_repo(value: str | os.PathLike[str]) -> Path:
    try:
        resolved = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("configured logparse repository is unavailable") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise ValueError("configured logparse repository must be a directory")
    return resolved


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    before = path.stat(follow_symlinks=False)
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ValueError("logparse repository changed while it was fingerprinted")
    if size != after.st_size:
        raise ValueError("logparse repository file size changed while hashing")
    return size, digest.hexdigest()


def _git_paths(repo: Path) -> list[str]:
    environment = os.environ.copy()
    # Keep the required argv exact while requesting unquoted UTF-8 paths from
    # Git.  Special/control-character paths remain rejected below.
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.quotepath",
            "GIT_CONFIG_VALUE_0": "false",
            "LC_ALL": "C.UTF-8",
        }
    )
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repo),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("git cannot enumerate the configured logparse repository") from exc
    if completed.returncode != 0:
        raise ValueError("git cannot enumerate the configured logparse repository")
    try:
        decoded = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("logparse repository contains a non-UTF-8 path") from exc
    paths = decoded.splitlines()
    if not paths or any(not path for path in paths):
        raise ValueError("logparse repository file list is empty or ambiguous")
    if len(paths) != len(set(paths)):
        raise ValueError("logparse repository file list contains duplicates")
    return paths


def _safe_repo_file(repo: Path, path_text: str) -> tuple[str, Path]:
    if (
        path_text != path_text.strip()
        or path_text.startswith('"')
        or path_text.endswith('"')
        or "\\" in path_text
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path_text)
    ):
        raise ValueError("logparse repository contains a non-canonical path")
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("logparse repository contains an unsafe path")
    try:
        candidate = repo.joinpath(*relative.parts)
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
        if os.path.commonpath((os.fspath(repo), os.fspath(resolved))) != os.fspath(repo):
            raise ValueError("logparse repository path escapes its root")
    except (OSError, RuntimeError) as exc:
        raise ValueError("logparse repository contains an unavailable path") from exc
    if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("logparse repository entries must be ordinary files")
    return relative.as_posix(), resolved


def _python_version(python: Path) -> str:
    try:
        completed = subprocess.run(
            [os.fspath(python), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("configured logparse Python cannot be executed") from exc
    if completed.returncode != 0:
        raise ValueError("configured logparse Python version check failed")
    try:
        merged = completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("configured logparse Python version is not UTF-8") from exc
    if not merged or "\n" in merged or "\r" in merged:
        raise ValueError("configured logparse Python version must be one line")
    return merged


def fingerprint_logparse_asset(
    logparse_repo: str | os.PathLike[str],
    logparse_config_path: str | os.PathLike[str],
    logparse_python: str | os.PathLike[str],
) -> ResolvedAsset:
    """Return the S07 fixed ``LOGPARSE_TOOL`` asset for one installation."""

    repo = _resolved_repo(logparse_repo)
    config = _resolved_file(logparse_config_path)
    python = _resolved_file(logparse_python, executable=True)

    entries: list[dict[str, object]] = []
    for raw_path in _git_paths(repo):
        canonical_path, resolved_path = _safe_repo_file(repo, raw_path)
        size, digest = _sha256_file(resolved_path)
        entries.append({"path": canonical_path, "size": size, "sha256": digest})
    entries.sort(key=lambda entry: str(entry["path"]))
    repo_tree_sha256 = hashlib.sha256(
        canonical_json_bytes({"version": 1, "entries": entries})
    ).hexdigest()
    _config_size, config_sha256 = _sha256_file(config)
    content_hash = hashlib.sha256(
        canonical_json_bytes(
            {
                "repo_tree_sha256": repo_tree_sha256,
                "config_sha256": config_sha256,
                "python_resolved_path": os.fspath(python),
                "python_version": _python_version(python),
            }
        )
    ).hexdigest()
    reference = VersionedRef(
        id="logparse-tool/logparse",
        version=f"sha256-{content_hash[:16]}",
        content_hash=content_hash,
    )
    return ResolvedAsset(
        ref=reference,
        asset_kind=AssetKind.LOGPARSE_TOOL,
        root_path=os.fspath(repo),
    )


def resolve_logparse_configuration(
    logparse_repo: str | os.PathLike[str],
    logparse_config_path: str | os.PathLike[str],
    logparse_python: str | os.PathLike[str],
) -> tuple[Path, Path, Path]:
    """Validate service settings and retain the configured Python launcher path."""

    return (
        _resolved_repo(logparse_repo),
        _resolved_file(logparse_config_path),
        _lexical_executable(logparse_python),
    )


__all__ = ["fingerprint_logparse_asset", "resolve_logparse_configuration"]
