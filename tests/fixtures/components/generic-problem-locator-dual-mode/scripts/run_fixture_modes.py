#!/usr/bin/env python3
"""Execute the TEST_ONLY fixture's fixed DIRECT, V1, and V2 oracle branches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / "references" / "native-report.md"
V1_ORACLE = ROOT / "references" / "v1-result.txt"
EXPECTED_PROBLEM = (
    "订单支付成功后页面仍显示“处理中”。\n"
    "request-id: 订单-α-42\n"
    "已确认：刷新三次仍复现"
).encode("utf-8")
V2_RESOLVED = b"<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>\n"
V2_UNRESOLVED = b"<<<GENERIC_DIAGNOSIS_RESULT_V2:UNRESOLVED>>>\n"
UNRESOLVED_REPORT = (
    "# 通用定位报告\n\n"
    "状态：未定位。受控测试输入与固定 oracle 不匹配。\n"
).encode("utf-8")
UNRESOLVED_V1 = (
    "<<<GENERIC_DIAGNOSIS_RESULT_V1>>>\n"
    "STATUS: UNRESOLVED\n"
    "CONCLUSION:\n"
    "受控测试输入与固定 oracle 不匹配。\n"
    "ROOT_CAUSE_ANALYSIS:\n"
    "此 TEST_ONLY fixture 只对一个冻结输入提供确定性结果。\n"
    "<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>\n"
).encode("utf-8")


class FixtureError(ValueError):
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


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _read_regular(path: Path, code: str) -> bytes:
    if not path.is_absolute():
        raise FixtureError(f"{code}_PATH_NOT_ABSOLUTE")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FixtureError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or metadata.st_nlink != 1
    ):
        raise FixtureError(code)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FixtureError(code) from exc
    if len(data) != metadata.st_size:
        raise FixtureError(code)
    return data


def _write_new(path: Path, payload: bytes) -> None:
    if not path.is_absolute():
        raise FixtureError("OUTPUT_PATH_NOT_ABSOLUTE")
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise FixtureError("OUTPUT_PARENT_INVALID") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or _is_reparse(parent)
        or path.exists()
        or path.is_symlink()
    ):
        raise FixtureError("OUTPUT_TARGET_INVALID")
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise FixtureError("OUTPUT_WRITE_FAILED") from exc


def execute(mode: str, problem_input: Path, output: Path) -> dict[str, Any]:
    problem = _read_regular(problem_input, "PROBLEM_INPUT_INVALID")
    oracle = _read_regular(ORACLE.resolve(), "ORACLE_INVALID")
    v1_oracle = _read_regular(V1_ORACLE.resolve(), "V1_ORACLE_INVALID")
    matched = problem == EXPECTED_PROBLEM
    if mode == "DIRECT_MODE":
        payload = oracle if matched else UNRESOLVED_REPORT
    elif mode == "FRAMEWORK_V1":
        payload = v1_oracle if matched else UNRESOLVED_V1
    else:
        payload = (V2_RESOLVED + oracle) if matched else (V2_UNRESOLVED + UNRESOLVED_REPORT)
    _write_new(output, payload)
    return {
        "schema_version": 1,
        "status": "RESOLVED" if matched else "UNRESOLVED",
        "mode": mode,
        "output_utf8_size": len(payload),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "content_included": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("DIRECT_MODE", "FRAMEWORK_V1", "FRAMEWORK_V2"),
        required=True,
    )
    parser.add_argument("--problem-input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = execute(args.mode, args.problem_input, args.output)
    except FixtureError as exc:
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
    sys.stdout.buffer.write(_canonical_bytes(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
