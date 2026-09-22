"""Validation for logparse-owned parse manifests and target-log results."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import TreeManifest, canonical_json_bytes

from .requests import Anchor
from .tree import build_tree_manifest


_MAX_MACHINE_RESULT_BYTES = 2_000_000
_TARGET_FIELDS = {
    "label",
    "module",
    "module_key",
    "module_name",
    "slot",
    "process_name",
    "pid",
    "match_status",
    "board_cycle",
    "cpu_id",
    "cpu_cycle",
    "caveats",
    "log_path",
}


@dataclass(frozen=True, slots=True)
class ControlledRun:
    root: Path
    task_id: str
    parse_manifest_relative_path: str
    tree_manifest: TreeManifest
    size: int
    sha256: str


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("logparse machine JSON contains a duplicate field")
        result[key] = value
    return result


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    if not payload or len(payload) > _MAX_MACHINE_RESULT_BYTES:
        raise ValueError(f"{label} is empty or exceeds the machine-result limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not one UTF-8 JSON value") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be one JSON object")
    return value


def _plain_root(value: Path) -> Path:
    supplied = Path(value)
    try:
        metadata = supplied.lstat()
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("controlled logparse root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("controlled logparse root must be a plain directory")
    return resolved


def _task_directory(root: Path, *, check_abort: Callable[[], None] | None = None) -> tuple[str, Path]:
    try:
        entries = []
        with os.scandir(root) as scanned:
            for entry in scanned:
                if check_abort is not None:
                    check_abort()
                entries.append(entry)
                if len(entries) > 1:
                    break
    except OSError as exc:
        raise ValueError("controlled logparse root cannot be enumerated") from exc
    if len(entries) != 1:
        raise ValueError("logparse parse output must contain exactly one task directory")
    entry = entries[0]
    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
        raise ValueError("logparse parse output task must be a plain directory")
    task_id = entry.name
    if (
        task_id in {"", ".", ".."}
        or task_id != task_id.strip()
        or len(task_id.encode("utf-8")) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in task_id)
    ):
        raise ValueError("logparse task id is not a safe path segment")
    return task_id, Path(entry.path)


def _validate_parse_manifest(task_id: str, task_root: Path, product: str) -> str:
    manifest_path = task_root / "parse_manifest.json"
    try:
        metadata = manifest_path.lstat()
    except OSError as exc:
        raise ValueError("logparse parse manifest is missing") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_MACHINE_RESULT_BYTES
    ):
        raise ValueError("logparse parse manifest must be one bounded plain file")
    value = _json_object(manifest_path.read_bytes(), label="logparse parse manifest")
    expected_types: dict[str, type] = {
        "stages": list,
        "artifacts": dict,
        "counters": dict,
        "diagnostics": list,
        "workspace": dict,
    }
    if (
        value.get("schema_version") != 1
        or value.get("artifact_contract_version") != 1
        or value.get("task_id") != task_id
        or value.get("product") != product
        or value.get("status") != "success"
        or not isinstance(value.get("created_at"), str)
        or not value.get("created_at")
        or any(not isinstance(value.get(field), expected) for field, expected in expected_types.items())
        or value["workspace"].get("retained") is not False
    ):
        raise ValueError("logparse parse manifest has an invalid success contract")
    return f"{task_id}/parse_manifest.json"


def inspect_controlled_run(
    root: Path, *, product: str, check_abort: Callable[[], None] | None = None,
) -> ControlledRun:
    """Validate and hash a complete parse output root."""

    if check_abort is not None:
        check_abort()
    controlled_root = _plain_root(root)
    task_id, task_root = _task_directory(controlled_root, check_abort=check_abort)
    manifest_relative_path = _validate_parse_manifest(task_id, task_root, product)
    tree_manifest, size, digest = build_tree_manifest(controlled_root, check_abort=check_abort)
    return ControlledRun(
        root=controlled_root,
        task_id=task_id,
        parse_manifest_relative_path=manifest_relative_path,
        tree_manifest=tree_manifest,
        size=size,
        sha256=digest,
    )


def inspect_existing_run(
    root: Path,
    *,
    product: str,
    expected_parse_manifest_relative_path: str,
    expected_size: int,
    expected_sha256: str,
) -> ControlledRun:
    """Revalidate a materialized immutable ``LOGPARSE_RUN`` in full."""

    run = inspect_controlled_run(root, product=product)
    if (
        run.parse_manifest_relative_path != expected_parse_manifest_relative_path
        or run.size != expected_size
        or run.sha256 != expected_sha256
    ):
        raise ValueError("materialized LOGPARSE_RUN does not match its frozen metadata")
    return run


def _validate_text_log(
    root: Path, relative_path: str, size: int, sha256: str, *,
    check_abort: Callable[[], None] | None = None,
) -> None:
    if check_abort is not None:
        check_abort()
    path = root.joinpath(*PurePosixPath(relative_path).parts)
    if _plain_target_path(root, os.fspath(path)) != relative_path:
        raise ValueError("parsed log path differs from the verified tree")
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    digest = hashlib.sha256()
    observed_size = 0
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or any(getattr(before, field) != getattr(opened, field) for field in fields)
        ):
            raise ValueError("parsed log changed before it was read")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while True:
                if check_abort is not None:
                    check_abort()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if b"\x00" in chunk:
                    raise ValueError("parsed log contains binary content")
                decoder.decode(chunk)
                observed_size += len(chunk)
                digest.update(chunk)
        decoder.decode(b"", final=True)
        after = os.fstat(descriptor)
    except UnicodeDecodeError as exc:
        raise ValueError("parsed log is not strict UTF-8 text") from exc
    finally:
        os.close(descriptor)
    named = path.stat(follow_symlinks=False)
    if (
        observed_size != size
        or digest.hexdigest() != sha256
        or any(getattr(opened, field) != getattr(after, field) for field in fields)
        or any(getattr(opened, field) != getattr(named, field) for field in fields)
    ):
        raise ValueError("parsed log differs from the verified tree")


def generic_parse_result(
    run: ControlledRun,
    *,
    source_attachment_id: str,
    source_attachment_sha256: str,
    check_abort: Callable[[], None] | None = None,
) -> bytes:
    """List every verified textual mechanism log without selecting a target."""

    logs: list[dict[str, Any]] = []
    listed_bytes = 0
    if check_abort is not None:
        check_abort()
    for entry in run.tree_manifest.entries:
        if check_abort is not None:
            check_abort()
        parts = PurePosixPath(entry.path).parts
        if (
            len(parts) < 3
            or parts[:2] != (run.task_id, "mech_modules")
            or PurePosixPath(entry.path).suffix != ".log"
        ):
            continue
        _validate_text_log(run.root, entry.path, entry.size, entry.sha256, check_abort=check_abort)
        item = {"relative_path": entry.path, "size": entry.size, "sha256": entry.sha256}
        listed_bytes += len(canonical_json_bytes(item))
        if listed_bytes > _MAX_MACHINE_RESULT_BYTES:
            raise ValueError("parsed log inventory exceeds the machine-result limit")
        logs.append(item)
    result = canonical_json_bytes({
        "schema_version": 1,
        "api_version": 1,
        "task_id": run.task_id,
        "parse_manifest_relative_path": run.parse_manifest_relative_path,
        "source_attachment_id": source_attachment_id,
        "source_attachment_sha256": source_attachment_sha256,
        "tree_manifest_sha256": run.sha256,
        "logs": logs,
    })
    if len(result) > _MAX_MACHINE_RESULT_BYTES:
        raise ValueError("parsed log inventory exceeds the machine-result limit")
    return result


def validate_generic_parse_result(
    payload: bytes,
    *,
    controlled_root: Path,
    product: str,
    source_attachment_id: str,
    source_attachment_sha256: str,
    check_abort: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Revalidate a parse-only response against its source and current output tree."""

    if check_abort is not None:
        check_abort()
    value = _json_object(payload, label="logparse parse-only result")
    run = inspect_controlled_run(controlled_root, product=product, check_abort=check_abort)
    expected = generic_parse_result(
        run,
        source_attachment_id=source_attachment_id,
        source_attachment_sha256=source_attachment_sha256,
        check_abort=check_abort,
    )
    if canonical_json_bytes(value) != expected:
        raise ValueError("parse-only result differs from its verified output tree")
    return value


def _normalized_slot(value: str) -> str:
    return value[5:] if value.casefold().startswith("slot_") else value


def _plain_target_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError("target log path is missing or invalid")
    supplied = Path(value)
    if not supplied.is_absolute():
        raise ValueError("external logparse target path must be absolute")
    lexical = Path(os.path.abspath(supplied))
    if os.path.commonpath((os.fspath(root), os.fspath(lexical))) != os.fspath(root):
        raise ValueError("target log path escapes the controlled output root")
    lexical_relative = lexical.relative_to(root)
    current = root
    try:
        for part in lexical_relative.parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("target log path contains a symbolic link")
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("target log path is unavailable") from exc
    if os.path.commonpath((os.fspath(root), os.fspath(resolved))) != os.fspath(root):
        raise ValueError("target log path escapes the controlled output root")
    relative = PurePosixPath(resolved.relative_to(root).as_posix())
    try:
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("target log path is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("target log must be a single-link plain file")
    return relative.as_posix()


def normalize_target_result(
    payload: bytes,
    *,
    anchor: Anchor,
    controlled_root: Path,
) -> dict[str, Any]:
    """Validate one real CLI response and rewrite its path relative to the run."""

    root = _plain_root(controlled_root)
    value = _json_object(payload, label="logparse target result")
    if set(value) != {"schema_version", "api_version", "target_logs"}:
        raise ValueError("logparse target result has an unknown top-level shape")
    targets = value.get("target_logs")
    if value.get("schema_version") != 1 or value.get("api_version") != 1:
        raise ValueError("logparse target result version is unsupported")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise ValueError("each target command must return exactly one target object")
    target = dict(targets[0])
    if not set(target) <= _TARGET_FIELDS:
        raise ValueError("logparse target object contains unknown fields")
    required = {
        "label",
        "module_key",
        "module_name",
        "slot",
        "process_name",
        "match_status",
        "caveats",
    }
    if not required <= set(target):
        raise ValueError("logparse target object is missing required fields")
    scalar_fields = ("label", "module_key", "module_name", "slot", "process_name", "match_status")
    if any(not isinstance(target.get(field), str) for field in scalar_fields):
        raise ValueError("logparse target object contains a non-string scalar")
    if "module" in target and not isinstance(target["module"], str):
        raise ValueError("logparse target module must be a string when present")
    if (
        target["label"] != anchor.label
        or (
            "module" in target
            and target["module"].casefold() != anchor.module.casefold()
        )
        or target["process_name"].casefold() != anchor.process_name.casefold()
        or _normalized_slot(target["slot"]) != _normalized_slot(anchor.slot)
        or anchor.module.casefold()
        not in {target["module_key"].casefold(), target["module_name"].casefold()}
        or (anchor.pid is not None and target.get("pid") != anchor.pid)
    ):
        raise ValueError("logparse target result does not match the requested anchor")
    if "pid" in target and not isinstance(target["pid"], str):
        raise ValueError("logparse target pid must be a string when present")
    if "cpu_id" in target and not isinstance(target["cpu_id"], str):
        raise ValueError("logparse target cpu_id must be a string when present")
    for field in ("board_cycle", "cpu_cycle"):
        if field in target and target[field] is not None and not isinstance(target[field], str):
            raise ValueError(f"logparse target {field} must be a string or null")
    caveats = target["caveats"]
    if not isinstance(caveats, list) or any(not isinstance(item, str) for item in caveats):
        raise ValueError("logparse target caveats must be a string array")
    status = target["match_status"]
    if status in {"exact", "nearest"}:
        target["log_path"] = _plain_target_path(root, target.get("log_path"))
    elif status in {"missing", "ambiguous"}:
        if "log_path" in target:
            raise ValueError("missing or ambiguous targets cannot name a log path")
    else:
        raise ValueError("logparse target match_status is unsupported")
    return target


def aggregate_target_results(
    targets: list[dict[str, Any]],
    *,
    logparse_run_artifact_draft: dict[str, Any] | None = None,
) -> bytes:
    """Return one canonical broker result while retaining anchor declaration order."""

    result: dict[str, Any] = {
        "schema_version": 1,
        "api_version": 1,
        "target_logs": targets,
    }
    if logparse_run_artifact_draft is not None:
        result["logparse_run_artifact_draft"] = logparse_run_artifact_draft
    return canonical_json_bytes(result)


__all__ = [
    "ControlledRun",
    "aggregate_target_results",
    "generic_parse_result",
    "inspect_controlled_run",
    "inspect_existing_run",
    "normalize_target_result",
    "validate_generic_parse_result",
]
