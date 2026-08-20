#!/usr/bin/env python3
"""Validate a LAN generic Skill and emit content-free local A/B receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


ADAPTER_START = "<!-- problem-locator-generic-v2-adapter:start -->"
ADAPTER_END = "<!-- problem-locator-generic-v2-adapter:end -->"
V2_MARKERS = {
    b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n": "RESOLVED",
    b"<<<GENERIC_DIAGNOSIS_RESULT_V2:UNRESOLVED>>>\n": "UNRESOLVED",
}
MAX_REPORT_BYTES = 65_536
MAX_SKILL_FILE_BYTES = 8 * 1024 * 1024
MAX_IDENTITY_MANIFEST_BYTES = 4 * 1024
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SKILL_VERSION = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,126}[A-Za-z0-9])?\Z"
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_MANIFEST_KIND = "problem-locator-generic-lan-run-identity-v1"
IDENTITY_MANIFEST_FIELDS = {
    "schema_version",
    "manifest_kind",
    "service_account_sha256",
    "agent_executable_sha256",
    "agent_version_sha256",
    "settings_sha256",
    "model_identity_sha256",
    "tool_inventory_sha256",
}
IDENTITY_DIGEST_FIELDS = IDENTITY_MANIFEST_FIELDS - {
    "schema_version",
    "manifest_kind",
}
SEMANTIC_STATUSES = {
    "equivalent": "PASS",
    "different": "FAIL",
    "not-reviewed": "REVIEW_REQUIRED",
}

REQUIRED_ADAPTER_TOKENS = (
    "DIRECT_MODE",
    "FRAMEWORK_V2",
    "FRAMEWORK_V1",
    "AMBIGUOUS_FRAMEWORK_OUTPUT",
    "<<<RAW_PROBLEM_TEXT_UTF8_BYTES:N>>>",
    "<<<END_RAW_PROBLEM_TEXT>>>",
    "output/generic_diagnosis_result.md",
    "<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\\n",
    "<<<GENERIC_DIAGNOSIS_RESULT_V2:UNRESOLVED>>>\\n",
    "output/generic_diagnosis_result.txt",
    "<<<GENERIC_DIAGNOSIS_RESULT_V1>>>",
    "<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>",
)


class VerificationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _require_absolute(path: Path, code: str) -> Path:
    if not path.is_absolute():
        raise VerificationError(code)
    return path


def _read_regular_file(path: Path, *, limit: int, code: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise VerificationError(code) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or before.st_nlink != 1
        or before.st_size > limit
    ):
        raise VerificationError(code)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
        ):
            raise VerificationError(code)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_descriptor = os.fstat(descriptor)
        after_name = path.lstat()
    except VerificationError:
        raise
    except OSError as exc:
        raise VerificationError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(data) > limit
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (
            after_descriptor.st_dev,
            after_descriptor.st_ino,
            after_descriptor.st_size,
            after_descriptor.st_mtime_ns,
        )
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (
            after_name.st_dev,
            after_name.st_ino,
            after_name.st_size,
            after_name.st_mtime_ns,
        )
        or len(data) != after_descriptor.st_size
    ):
        raise VerificationError(code)
    return data


def _strict_text(data: bytes, code: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationError(code) from exc


def _skill_name(skill_markdown: str) -> str:
    if not skill_markdown.startswith("---\n"):
        raise VerificationError("SKILL_FRONTMATTER_INVALID")
    end = skill_markdown.find("\n---\n", 4)
    if end < 0:
        raise VerificationError("SKILL_FRONTMATTER_INVALID")
    fields: dict[str, list[str]] = {"name": [], "description": []}
    for line in skill_markdown[4:end].splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in fields:
            fields[key].append(value.strip())
    if len(fields["name"]) != 1 or len(fields["description"]) != 1:
        raise VerificationError("SKILL_FRONTMATTER_INVALID")
    name = fields["name"][0]
    if not SKILL_NAME.fullmatch(name):
        raise VerificationError("SKILL_NAME_INVALID")
    if not fields["description"][0]:
        raise VerificationError("SKILL_DESCRIPTION_MISSING")
    return name


def _tree_identity(skill_root: Path) -> str:
    records: list[bytes] = []
    file_count = 0
    try:
        entries = sorted(
            skill_root.rglob("*"),
            key=lambda item: item.relative_to(skill_root).as_posix(),
        )
    except OSError as exc:
        raise VerificationError("SKILL_TREE_UNREADABLE") from exc
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as exc:
            raise VerificationError("SKILL_TREE_UNREADABLE") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise VerificationError("SKILL_TREE_LINK_FORBIDDEN")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise VerificationError("SKILL_TREE_NODE_INVALID")
        relative = entry.relative_to(skill_root).as_posix()
        data = _read_regular_file(
            entry,
            limit=MAX_SKILL_FILE_BYTES,
            code="SKILL_TREE_FILE_INVALID",
        )
        records.append(
            relative.encode("utf-8")
            + b"\0"
            + str(len(data)).encode("ascii")
            + b"\0"
            + hashlib.sha256(data).hexdigest().encode("ascii")
            + b"\n"
        )
        file_count += 1
    try:
        final_entries = sorted(
            item.relative_to(skill_root).as_posix()
            for item in skill_root.rglob("*")
        )
    except OSError as exc:
        raise VerificationError("SKILL_TREE_UNREADABLE") from exc
    if final_entries != [item.relative_to(skill_root).as_posix() for item in entries]:
        raise VerificationError("SKILL_TREE_CHANGED")
    if file_count == 0:
        raise VerificationError("SKILL_TREE_EMPTY")
    return _sha256(b"".join(records))


def _skill_version(value: str) -> str:
    if not SKILL_VERSION.fullmatch(value):
        raise VerificationError("SKILL_VERSION_INVALID")
    return value


def validate_skill(skill_root: Path, skill_version: str) -> dict[str, Any]:
    declared_version = _skill_version(skill_version)
    root = _require_absolute(skill_root, "SKILL_ROOT_NOT_ABSOLUTE")
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise VerificationError("SKILL_ROOT_INVALID") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise VerificationError("SKILL_ROOT_INVALID")
    markdown = _strict_text(
        _read_regular_file(
            root / "SKILL.md",
            limit=MAX_SKILL_FILE_BYTES,
            code="SKILL_MARKDOWN_INVALID",
        ),
        "SKILL_MARKDOWN_UTF8_INVALID",
    )
    name = _skill_name(markdown)
    if root.name != name:
        raise VerificationError("SKILL_DIRECTORY_NAME_MISMATCH")
    if markdown.count(ADAPTER_START) != 1 or markdown.count(ADAPTER_END) != 1:
        raise VerificationError("ADAPTER_BLOCK_COUNT_INVALID")
    start = markdown.index(ADAPTER_START)
    end = markdown.find(ADAPTER_END, start + len(ADAPTER_START))
    if end < 0:
        raise VerificationError("ADAPTER_BLOCK_ORDER_INVALID")
    block = markdown[start : end + len(ADAPTER_END)]
    if any(token not in block for token in REQUIRED_ADAPTER_TOKENS):
        raise VerificationError("ADAPTER_BLOCK_INCOMPLETE")
    tree_sha256 = _tree_identity(root)
    return {
        "schema_version": 2,
        "receipt_kind": "problem-locator-generic-skill-validation-v2",
        "status": "PASS",
        "skill": {
            "tree_sha256": tree_sha256,
            "version": declared_version,
        },
        "content_included": False,
    }


def _read_markdown_report(path: Path, *, code: str) -> tuple[bytes, str]:
    data = _read_regular_file(
        _require_absolute(path, f"{code}_PATH_NOT_ABSOLUTE"),
        limit=MAX_REPORT_BYTES,
        code=code,
    )
    if not data:
        raise VerificationError(f"{code}_EMPTY")
    if data.startswith(b"\xef\xbb\xbf"):
        raise VerificationError(f"{code}_BOM_FORBIDDEN")
    text = _strict_text(data, f"{code}_UTF8_INVALID")
    if not text.strip():
        raise VerificationError(f"{code}_EMPTY")
    return data, text


def _read_v2_result(path: Path) -> tuple[str, bytes, str]:
    data = _read_regular_file(
        _require_absolute(path, "FRAMEWORK_RESULT_PATH_NOT_ABSOLUTE"),
        limit=MAX_REPORT_BYTES + max(len(item) for item in V2_MARKERS),
        code="FRAMEWORK_RESULT_INVALID",
    )
    marker = next((item for item in V2_MARKERS if data.startswith(item)), None)
    if marker is None:
        raise VerificationError("FRAMEWORK_RESULT_MARKER_INVALID")
    body = data[len(marker) :]
    if len(body) > MAX_REPORT_BYTES:
        raise VerificationError("FRAMEWORK_REPORT_OVERSIZE")
    if body.startswith(b"\xef\xbb\xbf"):
        raise VerificationError("FRAMEWORK_REPORT_BOM_FORBIDDEN")
    text = _strict_text(body, "FRAMEWORK_REPORT_UTF8_INVALID")
    if not text.strip():
        raise VerificationError("FRAMEWORK_REPORT_EMPTY")
    return V2_MARKERS[marker], body, text


def _read_identity_manifest(path: Path, *, code: str) -> bytes:
    data = _read_regular_file(
        _require_absolute(path, f"{code}_PATH_NOT_ABSOLUTE"),
        limit=MAX_IDENTITY_MANIFEST_BYTES,
        code=code,
    )
    if data.startswith(b"\xef\xbb\xbf"):
        raise VerificationError(f"{code}_BOM_FORBIDDEN")
    text = _strict_text(data, f"{code}_UTF8_INVALID")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise VerificationError(f"{code}_DUPLICATE_FIELD")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise VerificationError(f"{code}_JSON_INVALID")

    try:
        manifest = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except VerificationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{code}_JSON_INVALID") from exc
    if not isinstance(manifest, dict) or set(manifest) != IDENTITY_MANIFEST_FIELDS:
        raise VerificationError(f"{code}_FIELDS_INVALID")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise VerificationError(f"{code}_SCHEMA_INVALID")
    if manifest["manifest_kind"] != IDENTITY_MANIFEST_KIND:
        raise VerificationError(f"{code}_KIND_INVALID")
    if any(
        not isinstance(manifest[field], str)
        or not SHA256.fullmatch(manifest[field])
        for field in IDENTITY_DIGEST_FIELDS
    ):
        raise VerificationError(f"{code}_DIGEST_INVALID")
    if _canonical_bytes(manifest) != data:
        raise VerificationError(f"{code}_CANONICAL_JSON_REQUIRED")
    return data


def _outside_skill_root(receipt: Path, skill_root: Path) -> None:
    try:
        receipt.resolve(strict=False).relative_to(skill_root.resolve(strict=True))
    except ValueError:
        return
    except OSError as exc:
        raise VerificationError("RECEIPT_PATH_INVALID") from exc
    raise VerificationError("RECEIPT_INSIDE_SKILL_ROOT")


def _validate_receipt_target(receipt: Path) -> None:
    try:
        parent = receipt.parent.lstat()
    except OSError as exc:
        raise VerificationError("RECEIPT_PARENT_INVALID") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or _is_reparse(parent)
    ):
        raise VerificationError("RECEIPT_PARENT_INVALID")
    if receipt.exists() or receipt.is_symlink():
        raise VerificationError("RECEIPT_ALREADY_EXISTS")


def ab_receipt(
    *,
    skill_root: Path,
    skill_version: str,
    problem_input: Path,
    direct_report: Path,
    direct_status: str,
    direct_identity_manifest: Path,
    framework_result: Path,
    framework_identity_manifest: Path,
    semantic_verdict: str,
) -> dict[str, Any]:
    if direct_status not in {"RESOLVED", "UNRESOLVED"}:
        raise VerificationError("DIRECT_STATUS_INVALID")
    if semantic_verdict not in SEMANTIC_STATUSES:
        raise VerificationError("SEMANTIC_VERDICT_INVALID")
    validation = validate_skill(skill_root, skill_version)
    problem_bytes, _ = _read_markdown_report(
        problem_input,
        code="PROBLEM_INPUT_INVALID",
    )
    direct_bytes, direct_text = _read_markdown_report(
        direct_report,
        code="DIRECT_REPORT_INVALID",
    )
    result_status, framework_bytes, framework_text = _read_v2_result(framework_result)
    del direct_text, framework_text
    try:
        if direct_identity_manifest.resolve(
            strict=True
        ) == framework_identity_manifest.resolve(strict=True):
            raise VerificationError("RUN_IDENTITY_MANIFEST_PATHS_NOT_DISTINCT")
    except VerificationError:
        raise
    except OSError as exc:
        raise VerificationError("RUN_IDENTITY_MANIFEST_PATH_INVALID") from exc
    direct_identity = _read_identity_manifest(
        direct_identity_manifest,
        code="DIRECT_IDENTITY_MANIFEST_INVALID",
    )
    framework_identity = _read_identity_manifest(
        framework_identity_manifest,
        code="FRAMEWORK_IDENTITY_MANIFEST_INVALID",
    )
    if direct_identity != framework_identity:
        raise VerificationError("RUN_IDENTITY_MISMATCH")
    if semantic_verdict == "equivalent" and direct_status != result_status:
        raise VerificationError("SEMANTIC_VERDICT_STATUS_CONFLICT")
    direct_summary = {
        "utf8_size": len(direct_bytes),
        "sha256": _sha256(direct_bytes),
    }
    framework_summary = {
        "utf8_size": len(framework_bytes),
        "sha256": _sha256(framework_bytes),
    }
    direct_summary["result_status"] = direct_status
    framework_summary["result_status"] = result_status
    return {
        "schema_version": 2,
        "receipt_kind": "problem-locator-generic-lan-ab-v2",
        "status": SEMANTIC_STATUSES[semantic_verdict],
        "semantic_verdict": semantic_verdict,
        "skill": validation["skill"],
        "problem_input": {
            "utf8_size": len(problem_bytes),
            "sha256": _sha256(problem_bytes),
        },
        "direct_report": direct_summary,
        "framework_report": framework_summary,
        "run_identity": {
            "direct_manifest_sha256": _sha256(direct_identity),
            "framework_manifest_sha256": _sha256(framework_identity),
        },
        "content_included": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-skill")
    validate.add_argument("--skill-root", required=True, type=Path)
    validate.add_argument("--skill-version", required=True)
    ab = commands.add_parser("ab-receipt")
    ab.add_argument("--skill-root", required=True, type=Path)
    ab.add_argument("--skill-version", required=True)
    ab.add_argument("--problem-input", required=True, type=Path)
    ab.add_argument("--direct-report", required=True, type=Path)
    ab.add_argument(
        "--direct-status",
        choices=("RESOLVED", "UNRESOLVED"),
        required=True,
    )
    ab.add_argument("--direct-identity-manifest", required=True, type=Path)
    ab.add_argument("--framework-result", required=True, type=Path)
    ab.add_argument("--framework-identity-manifest", required=True, type=Path)
    ab.add_argument(
        "--semantic-verdict",
        choices=("equivalent", "different", "not-reviewed"),
        required=True,
    )
    ab.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-skill":
            sys.stdout.buffer.write(
                _canonical_bytes(validate_skill(args.skill_root, args.skill_version))
            )
            return 0
        _require_absolute(args.receipt, "RECEIPT_PATH_NOT_ABSOLUTE")
        receipt = ab_receipt(
            skill_root=args.skill_root,
            skill_version=args.skill_version,
            problem_input=args.problem_input,
            direct_report=args.direct_report,
            direct_status=args.direct_status,
            direct_identity_manifest=args.direct_identity_manifest,
            framework_result=args.framework_result,
            framework_identity_manifest=args.framework_identity_manifest,
            semantic_verdict=args.semantic_verdict,
        )
        _outside_skill_root(args.receipt, args.skill_root)
        _validate_receipt_target(args.receipt)
        payload = _canonical_bytes(receipt)
        try:
            with args.receipt.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise VerificationError("RECEIPT_WRITE_FAILED") from exc
        sys.stdout.buffer.write(
            _canonical_bytes(
                {
                    "schema_version": 1,
                    "status": receipt["status"],
                    "receipt_sha256": _sha256(payload),
                    "content_included": False,
                }
            )
        )
        if receipt["status"] == "PASS":
            return 0
        if receipt["status"] == "FAIL":
            return 1
        return 3
    except VerificationError as exc:
        sys.stderr.buffer.write(
            _canonical_bytes(
                {
                    "schema_version": 1,
                    "status": "ERROR",
                    "code": exc.code,
                    "content_included": False,
                }
            )
        )
        return 2
    except Exception:
        sys.stderr.buffer.write(
            _canonical_bytes(
                {
                    "schema_version": 1,
                    "status": "ERROR",
                    "code": "UNEXPECTED_FAILURE",
                    "content_included": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
