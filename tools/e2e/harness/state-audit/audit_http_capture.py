#!/usr/bin/env python3
"""Offline audit of Windows curl artifact-download evidence.

Inputs are raw curl header dumps, small write-out JSON metadata files, the two
downloaded USER_RESULT bodies, the internal 404 body, authoritative journey
summaries, and one canonical StateExport.  The script performs no network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from problem_locator.contracts.enums import ArtifactKind
from problem_locator.contracts.models import ArtifactView, StateExport, UserResultPayload
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


SCHEMA_VERSION = 1
SERVICE_BASE_URL = "http://127.0.0.1:18000"
_STATUS_LINE = re.compile(r"^HTTP/(?:1\.[01]|2) ([0-9]{3})(?: .*)?$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class AuditFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AuditFailure(code)


def require_one(values: Sequence[Any], code: str) -> Any:
    require(len(values) == 1, code)
    return values[0]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def read_ordinary_file(path: Path, code: str) -> bytes:
    try:
        require(not path.is_symlink(), code)
        require(path.is_file(), code)
        return path.read_bytes()
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure(code) from exc


def strict_json_bytes(raw: bytes, code: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise AuditFailure(code) from exc


def load_strict_json(path: Path, code: str) -> Any:
    return strict_json_bytes(read_ordinary_file(path, code), code)


def load_canonical_export(path: Path) -> StateExport:
    raw = read_ordinary_file(path, "STATE_EXPORT_INVALID")
    try:
        exported = parse_canonical_json_bytes(raw, model_type=StateExport)
        require(canonical_json_bytes(exported) == raw, "STATE_EXPORT_INVALID")
        return exported
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("STATE_EXPORT_INVALID") from exc


def write_exclusive_canonical(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    descriptor: int | None = None
    try:
        require(path.parent.is_dir(), "OUTPUT_PARENT_INVALID")
        require(not path.parent.is_symlink(), "OUTPUT_PARENT_INVALID")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise AuditFailure("OUTPUT_EXISTS") from exc
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure("OUTPUT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class HeaderCapture:
    def __init__(self, status: int, headers: Mapping[str, tuple[str, ...]]) -> None:
        self.status = status
        self.headers = dict(headers)

    def exactly_one(self, name: str, code: str) -> str:
        values = self.headers.get(name.lower(), ())
        require(len(values) == 1, code)
        return values[0]


def parse_header_capture(path: Path, code: str) -> HeaderCapture:
    raw = read_ordinary_file(path, code)
    try:
        text = raw.decode("iso-8859-1")
    except Exception as exc:
        raise AuditFailure(code) from exc
    require("\r" not in text.replace("\r\n", ""), code)
    normalized = text.replace("\r\n", "\n")
    blocks = [block for block in normalized.split("\n\n") if block]
    require(len(blocks) == 1, code)
    lines = blocks[0].split("\n")
    require(bool(lines), code)
    matched = _STATUS_LINE.fullmatch(lines[0])
    require(matched is not None, code)
    status = int(matched.group(1))
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        require(bool(line), code)
        require(not line[0].isspace(), code)
        require(":" in line, code)
        name, value = line.split(":", 1)
        require(_HEADER_NAME.fullmatch(name) is not None, code)
        require("\x00" not in value, code)
        headers.setdefault(name.lower(), []).append(value.strip(" \t"))
    return HeaderCapture(
        status,
        {name: tuple(values) for name, values in headers.items()},
    )


def load_artifact_view(
    path: Path, property_name: str, code: str
) -> tuple[str, ArtifactView]:
    payload = load_strict_json(path, code)
    require(isinstance(payload, dict), code)
    case_id = payload.get("case_id")
    require(isinstance(case_id, str) and bool(case_id), code)
    try:
        artifact = ArtifactView.model_validate(payload.get(property_name))
    except Exception as exc:
        raise AuditFailure(code) from exc
    return case_id, artifact


class CurlMeta:
    def __init__(
        self,
        *,
        http_code: int,
        url_effective: str,
        num_redirects: int,
        size_download: int,
    ) -> None:
        self.http_code = http_code
        self.url_effective = url_effective
        self.num_redirects = num_redirects
        self.size_download = size_download


def load_curl_meta(path: Path, code: str) -> CurlMeta:
    payload = load_strict_json(path, code)
    require(isinstance(payload, dict), code)
    http_code = payload.get("http_code")
    url_effective = payload.get("url_effective")
    num_redirects = payload.get("num_redirects")
    size_download = payload.get("size_download")
    require(type(http_code) is int, code)
    require(isinstance(url_effective, str) and bool(url_effective), code)
    require(type(num_redirects) is int and num_redirects >= 0, code)
    if type(size_download) is float:
        require(size_download.is_integer(), code)
        size_download = int(size_download)
    require(type(size_download) is int and size_download >= 0, code)
    return CurlMeta(
        http_code=http_code,
        url_effective=url_effective,
        num_redirects=num_redirects,
        size_download=size_download,
    )


def audit_public_capture(
    *,
    label: str,
    case_id: str,
    view: ArtifactView,
    headers: HeaderCapture,
    meta: CurlMeta,
    body: bytes,
    expected_content_type: str,
    validate_user_result: bool,
) -> None:
    prefix = label.upper()
    expected_url = (
        f"{SERVICE_BASE_URL}/api/v1/artifacts/{view.artifact_id}/content"
        f"?case_id={case_id}"
    )
    require(view.download_url == expected_url, f"{prefix}_VIEW_URL")
    require(headers.status == 200, f"{prefix}_HTTP_STATUS")
    require(meta.http_code == 200, f"{prefix}_META_STATUS")
    require(meta.url_effective == expected_url, f"{prefix}_EFFECTIVE_URL")
    require(meta.num_redirects == 0, f"{prefix}_REDIRECT")
    require(meta.size_download == len(body), f"{prefix}_META_SIZE")
    require(
        headers.exactly_one("content-type", f"{prefix}_CONTENT_TYPE_COUNT")
        == expected_content_type,
        f"{prefix}_CONTENT_TYPE",
    )
    require(
        headers.exactly_one("content-length", f"{prefix}_CONTENT_LENGTH_COUNT")
        == str(view.size),
        f"{prefix}_CONTENT_LENGTH",
    )
    require(
        headers.exactly_one("x-content-sha256", f"{prefix}_SHA_HEADER_COUNT")
        == view.sha256,
        f"{prefix}_SHA_HEADER",
    )
    require(len(body) == view.size, f"{prefix}_BODY_SIZE")
    require(hashlib.sha256(body).hexdigest() == view.sha256, f"{prefix}_BODY_SHA256")
    if validate_user_result:
        try:
            parse_canonical_json_bytes(body, model_type=UserResultPayload)
        except Exception as exc:
            raise AuditFailure(f"{prefix}_USER_RESULT_INVALID") from exc


def audit_result_archive(body: bytes, result: UserResultPayload, code: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(body), mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            require(bool(names) and names[0] == "result.txt", code)
            require(len(names) == len(set(names)), code)
            require(names[1:] == [f"target-log-{index:03d}.log" for index in range(1, len(names))], code)
            for info in infos:
                require("/" not in info.filename and "\\" not in info.filename, code)
                require(info.date_time == (1980, 1, 1, 0, 0, 0), code)
                require(info.compress_type == zipfile.ZIP_DEFLATED, code)
            require(
                archive.read("result.txt")
                == (result.candidate_statement + "\n").encode("utf-8"),
                code,
            )
    except AuditFailure:
        raise
    except Exception as exc:
        raise AuditFailure(code) from exc


def audit_internal_capture(
    *,
    expected_url: str,
    headers: HeaderCapture,
    meta: CurlMeta,
    body: bytes,
) -> None:
    require(headers.status == 404, "INTERNAL_HTTP_STATUS")
    require(meta.http_code == 404, "INTERNAL_META_STATUS")
    require(meta.url_effective == expected_url, "INTERNAL_EFFECTIVE_URL")
    require(meta.num_redirects == 0, "INTERNAL_REDIRECT")
    require(meta.size_download == len(body), "INTERNAL_META_SIZE")
    require(
        headers.exactly_one("content-type", "INTERNAL_CONTENT_TYPE_COUNT")
        == "application/json",
        "INTERNAL_CONTENT_TYPE",
    )
    require(
        headers.exactly_one("content-length", "INTERNAL_CONTENT_LENGTH_COUNT")
        == str(len(body)),
        "INTERNAL_CONTENT_LENGTH",
    )
    require(
        "x-content-sha256" not in headers.headers,
        "INTERNAL_SHA_HEADER_PRESENT",
    )
    payload = strict_json_bytes(body, "INTERNAL_BODY_INVALID")
    require(
        payload
        == {
            "ok": False,
            "data": None,
            "error": {
                "code": "ARTIFACT_NOT_FOUND",
                "message": "The downloadable Artifact does not exist.",
                "details": [],
                "retryable": False,
            },
        },
        "INTERNAL_ERROR_ENVELOPE",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-headers", type=Path, required=True)
    parser.add_argument("--after-headers", type=Path, required=True)
    parser.add_argument("--internal-headers", type=Path, required=True)
    parser.add_argument("--before-meta", type=Path, required=True)
    parser.add_argument("--after-meta", type=Path, required=True)
    parser.add_argument("--internal-meta", type=Path, required=True)
    parser.add_argument("--before-result", type=Path, required=True)
    parser.add_argument("--after-result", type=Path, required=True)
    parser.add_argument("--before-archive-headers", type=Path, required=True)
    parser.add_argument("--after-archive-headers", type=Path, required=True)
    parser.add_argument("--before-archive-meta", type=Path, required=True)
    parser.add_argument("--after-archive-meta", type=Path, required=True)
    parser.add_argument("--before-archive", type=Path, required=True)
    parser.add_argument("--after-archive", type=Path, required=True)
    parser.add_argument("--internal-body", type=Path, required=True)
    parser.add_argument("--journey-summary", type=Path, required=True)
    parser.add_argument("--restart-summary", type=Path, required=True)
    parser.add_argument("--state-export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def perform_audit(arguments: argparse.Namespace) -> Mapping[str, Any]:
    before_case_id, before_view = load_artifact_view(
        arguments.journey_summary,
        "public_artifact",
        "JOURNEY_SUMMARY_INVALID",
    )
    after_case_id, after_view = load_artifact_view(
        arguments.restart_summary,
        "public_artifact",
        "RESTART_SUMMARY_INVALID",
    )
    require(before_case_id == after_case_id, "RESTART_CASE_ID")
    require(before_view == after_view, "RESTART_ARTIFACT_VIEW")
    before_archive_case_id, before_archive_view = load_artifact_view(
        arguments.journey_summary,
        "public_result_archive",
        "JOURNEY_ARCHIVE_SUMMARY_INVALID",
    )
    after_archive_case_id, after_archive_view = load_artifact_view(
        arguments.restart_summary,
        "public_result_archive",
        "RESTART_ARCHIVE_SUMMARY_INVALID",
    )
    require(before_archive_case_id == before_case_id, "ARCHIVE_CASE_ID")
    require(after_archive_case_id == after_case_id, "RESTART_ARCHIVE_CASE_ID")
    require(before_archive_view == after_archive_view, "RESTART_ARCHIVE_VIEW")

    exported = load_canonical_export(arguments.state_export)
    aggregate = exported.state.cases.get(before_case_id)
    require(aggregate is not None, "STATE_CASE_MISSING")
    user_result = require_one(
        [
            artifact
            for artifact in aggregate.artifacts.values()
            if artifact.kind is ArtifactKind.USER_RESULT
        ],
        "STATE_USER_RESULT_COUNT",
    )
    logparse_run = require_one(
        [
            artifact
            for artifact in aggregate.artifacts.values()
            if artifact.kind is ArtifactKind.LOGPARSE_RUN
        ],
        "STATE_LOGPARSE_COUNT",
    )
    result_archive = require_one(
        [
            artifact
            for artifact in aggregate.artifacts.values()
            if artifact.kind is ArtifactKind.USER_RESULT_ARCHIVE
        ],
        "STATE_USER_RESULT_ARCHIVE_COUNT",
    )
    require(before_view.artifact_id == user_result.artifact_id, "VIEW_USER_RESULT_ID")
    require(before_view.artifact_id != logparse_run.artifact_id, "VIEW_INTERNAL_ID")
    require(before_view.name == user_result.name, "VIEW_NAME")
    require(before_view.content_type == user_result.content_type, "VIEW_CONTENT_TYPE")
    require(before_view.size == user_result.size, "VIEW_SIZE")
    require(before_view.sha256 == user_result.sha256, "VIEW_SHA256")
    require(before_view.created_at == user_result.created_at, "VIEW_CREATED_AT")
    require(before_archive_view.artifact_id == result_archive.artifact_id, "ARCHIVE_VIEW_ID")
    require(before_archive_view.name == result_archive.name, "ARCHIVE_VIEW_NAME")
    require(before_archive_view.content_type == result_archive.content_type, "ARCHIVE_VIEW_CONTENT_TYPE")
    require(before_archive_view.size == result_archive.size, "ARCHIVE_VIEW_SIZE")
    require(before_archive_view.sha256 == result_archive.sha256, "ARCHIVE_VIEW_SHA256")
    require(before_archive_view.created_at == result_archive.created_at, "ARCHIVE_VIEW_CREATED_AT")

    before_headers = parse_header_capture(arguments.before_headers, "BEFORE_HEADERS_INVALID")
    after_headers = parse_header_capture(arguments.after_headers, "AFTER_HEADERS_INVALID")
    internal_headers = parse_header_capture(
        arguments.internal_headers,
        "INTERNAL_HEADERS_INVALID",
    )
    before_archive_headers = parse_header_capture(arguments.before_archive_headers, "BEFORE_ARCHIVE_HEADERS_INVALID")
    after_archive_headers = parse_header_capture(arguments.after_archive_headers, "AFTER_ARCHIVE_HEADERS_INVALID")
    before_meta = load_curl_meta(arguments.before_meta, "BEFORE_META_INVALID")
    after_meta = load_curl_meta(arguments.after_meta, "AFTER_META_INVALID")
    internal_meta = load_curl_meta(arguments.internal_meta, "INTERNAL_META_INVALID")
    before_archive_meta = load_curl_meta(arguments.before_archive_meta, "BEFORE_ARCHIVE_META_INVALID")
    after_archive_meta = load_curl_meta(arguments.after_archive_meta, "AFTER_ARCHIVE_META_INVALID")
    before_body = read_ordinary_file(arguments.before_result, "BEFORE_RESULT_INVALID")
    after_body = read_ordinary_file(arguments.after_result, "AFTER_RESULT_INVALID")
    internal_body = read_ordinary_file(arguments.internal_body, "INTERNAL_BODY_INVALID")
    before_archive_body = read_ordinary_file(arguments.before_archive, "BEFORE_ARCHIVE_INVALID")
    after_archive_body = read_ordinary_file(arguments.after_archive, "AFTER_ARCHIVE_INVALID")

    audit_public_capture(
        label="before",
        case_id=before_case_id,
        view=before_view,
        headers=before_headers,
        meta=before_meta,
        body=before_body,
        expected_content_type="application/json",
        validate_user_result=True,
    )
    audit_public_capture(
        label="after",
        case_id=after_case_id,
        view=after_view,
        headers=after_headers,
        meta=after_meta,
        body=after_body,
        expected_content_type="application/json",
        validate_user_result=True,
    )
    result_payload = parse_canonical_json_bytes(before_body, model_type=UserResultPayload)
    audit_public_capture(
        label="before_archive",
        case_id=before_case_id,
        view=before_archive_view,
        headers=before_archive_headers,
        meta=before_archive_meta,
        body=before_archive_body,
        expected_content_type="application/zip",
        validate_user_result=False,
    )
    audit_public_capture(
        label="after_archive",
        case_id=after_case_id,
        view=after_archive_view,
        headers=after_archive_headers,
        meta=after_archive_meta,
        body=after_archive_body,
        expected_content_type="application/zip",
        validate_user_result=False,
    )
    audit_result_archive(before_archive_body, result_payload, "BEFORE_ARCHIVE_CONTENT")
    audit_result_archive(after_archive_body, result_payload, "AFTER_ARCHIVE_CONTENT")
    require(before_archive_body == after_archive_body, "RESTART_ARCHIVE_BYTES")
    require(before_body == after_body, "RESTART_RESULT_BYTES")
    require(
        before_headers.headers.get("content-type")
        == after_headers.headers.get("content-type")
        and before_headers.headers.get("content-length")
        == after_headers.headers.get("content-length")
        and before_headers.headers.get("x-content-sha256")
        == after_headers.headers.get("x-content-sha256"),
        "RESTART_RESULT_HEADERS",
    )

    internal_url = (
        f"{SERVICE_BASE_URL}/api/v1/artifacts/{logparse_run.artifact_id}/content"
        f"?case_id={before_case_id}"
    )
    audit_internal_capture(
        expected_url=internal_url,
        headers=internal_headers,
        meta=internal_meta,
        body=internal_body,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "case_id": before_case_id,
        "public_artifact": {
            "artifact_id": user_result.artifact_id,
            "size": user_result.size,
            "sha256": user_result.sha256,
            "before_status": 200,
            "after_status": 200,
            "bytes_equal_after_restart": True,
        },
        "public_result_archive": {
            "artifact_id": result_archive.artifact_id,
            "size": result_archive.size,
            "sha256": result_archive.sha256,
            "before_status": 200,
            "after_status": 200,
            "bytes_equal_after_restart": True,
        },
        "internal_logparse": {
            "artifact_id": logparse_run.artifact_id,
            "status": 404,
            "error_code": "ARTIFACT_NOT_FOUND",
        },
        "redirects_followed": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        report = perform_audit(arguments)
    except AuditFailure as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "failure_code": exc.code,
        }
        exit_code = 1
    except Exception:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "failure_code": "AUDIT_INTERNAL",
        }
        exit_code = 1
    else:
        exit_code = 0

    try:
        write_exclusive_canonical(arguments.output, report)
    except AuditFailure as exc:
        sys.stderr.write(f"http-audit={exc.code}\n")
        return 2
    sys.stdout.write(
        "http-audit=passed\n" if exit_code == 0 else "http-audit=failed\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
