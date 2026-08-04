"""Controlled deterministic builder for the user-facing diagnosis archive."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

from problem_locator.contracts import canonical_json_bytes, parse_canonical_json_bytes
from problem_locator.integrations.logparse.paths import (
    resolve_workspace_path,
    validate_relative_path,
)


_MAX_REQUEST_BYTES = 1_048_576
_MAX_RESULT_TEXT_BYTES = 1_048_576
_MAX_TARGET_LOGS = 100
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _exact_request(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "result_text",
        "target_log_paths",
    }:
        raise ValueError("archive request fields are invalid")
    if value["schema_version"] != 1:
        raise ValueError("archive request schema_version must equal 1")
    result_text = value["result_text"]
    if not isinstance(result_text, str) or not result_text.strip():
        raise ValueError("result_text must be a non-empty string")
    if len(result_text.encode("utf-8")) > _MAX_RESULT_TEXT_BYTES:
        raise ValueError("result_text is too large")
    paths = value["target_log_paths"]
    if (
        not isinstance(paths, list)
        or len(paths) > _MAX_TARGET_LOGS
        or any(not isinstance(path, str) for path in paths)
        or len(paths) != len(set(paths))
    ):
        raise ValueError("target_log_paths are invalid")
    for raw_path in paths:
        path = validate_relative_path(raw_path)
        parts = path.parts
        from_existing_run = (
            len(parts) >= 5
            and parts[:2] == ("inputs", "artifacts")
            and parts[3] == "tree"
        )
        from_new_run = (
            len(parts) >= 5
            and parts[:2] == ("output", "proposals")
            and parts[3] == "tree"
        )
        if not (from_existing_run or from_new_run):
            raise ValueError("target logs must come from a LOGPARSE_RUN tree")
    return result_text, tuple(paths)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _ordinary_file(path: Path) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path.is_symlink()
    ):
        raise ValueError("target log is not an ordinary file")
    return path.read_bytes()


def validate_result_archive_bytes(
    archive_bytes: bytes,
    *,
    target_logs: tuple[bytes, ...],
) -> str:
    """Validate deterministic entries and return the user-facing result text."""

    expected_names = ["result.txt"] + [
        f"target-log-{index:03d}.log"
        for index in range(1, len(target_logs) + 1)
    ]
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if names != expected_names or len(names) != len(set(names)):
                raise ValueError("result archive entries are invalid")
            for info in infos:
                if (
                    info.is_dir()
                    or "/" in info.filename
                    or "\\" in info.filename
                    or info.date_time != _ZIP_TIMESTAMP
                    or info.compress_type != zipfile.ZIP_DEFLATED
                    or info.flag_bits & 0x1
                ):
                    raise ValueError("result archive metadata is invalid")
            result_info = infos[0]
            if result_info.file_size > _MAX_RESULT_TEXT_BYTES:
                raise ValueError("result.txt is too large")
            result_bytes = archive.read(result_info)
            try:
                result_text = result_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("result.txt must be UTF-8") from exc
            if not result_text.strip():
                raise ValueError("result.txt must be non-empty")
            for info, expected in zip(infos[1:], target_logs, strict=True):
                if info.file_size != len(expected) or archive.read(info) != expected:
                    raise ValueError("result archive target log bytes do not match")
    except zipfile.BadZipFile as exc:
        raise ValueError("result archive is not a valid ZIP") from exc
    return result_text


def build_result_archive(
    workspace_root: Path,
    request_path: str,
    result_path: str,
) -> Path:
    request_relative = validate_relative_path(request_path)
    result_relative = validate_relative_path(result_path)
    if (
        len(request_relative.parts) != 4
        or request_relative.parts[:2] != ("output", "proposals")
        or request_relative.parts[3] != "request.json"
        or len(result_relative.parts) != 4
        or result_relative.parts[:2] != ("output", "proposals")
        or result_relative.parts[3] != "result.zip"
        or request_relative.parts[2] != result_relative.parts[2]
    ):
        raise ValueError("archive I/O must use one output/proposals/<key> directory")
    request_file = resolve_workspace_path(workspace_root, request_path, must_exist=True)
    request_metadata = request_file.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(request_metadata.st_mode)
        or request_metadata.st_nlink != 1
        or request_metadata.st_size > _MAX_REQUEST_BYTES
    ):
        raise ValueError("archive request file is invalid")
    raw_request = request_file.read_bytes()
    value = parse_canonical_json_bytes(raw_request)
    result_text, target_log_paths = _exact_request(value)
    if canonical_json_bytes(value) != raw_request:
        raise ValueError("archive request must be canonical JSON")

    target = resolve_workspace_path(workspace_root, result_path, must_exist=False)
    parent = target.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise ValueError("archive result parent is invalid")
    temporary = target.with_name("result.zip.tmp")
    if target.exists() or temporary.exists():
        raise ValueError("archive output already exists")

    try:
        with temporary.open("xb") as stream:
            with zipfile.ZipFile(
                stream,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                strict_timestamps=True,
            ) as archive:
                archive.writestr(_zip_info("result.txt"), result_text.encode("utf-8"))
                for index, relative_path in enumerate(target_log_paths, start=1):
                    source = resolve_workspace_path(
                        workspace_root,
                        relative_path,
                        must_exist=True,
                    )
                    archive.writestr(
                        _zip_info(f"target-log-{index:03d}.log"),
                        _ordinary_file(source),
                    )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="problem-locator-pack-result")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        target = build_result_archive(Path.cwd(), args.request, args.result)
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"problem-locator-pack-result: {exc}", file=sys.stderr)
        return 2
    print(target.relative_to(Path.cwd()).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_result_archive", "main", "validate_result_archive_bytes"]
