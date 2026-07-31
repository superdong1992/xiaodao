"""Deterministic subprocess fixture for S04 Agent Backend tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _integer(name: str, default: int = 0) -> int:
    return int(os.environ.get(name, str(default)))


def _write_repeated(stream: object, byte: bytes, size: int) -> None:
    remaining = size
    while remaining:
        chunk = byte * min(8192, remaining)
        stream.write(chunk)  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
        remaining -= len(chunk)


def _sleep_forever() -> None:
    while True:
        time.sleep(60)


def main() -> int:
    if sys.argv[1:] == ["--child"]:
        _sleep_forever()

    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
    if mode == "ignore-stdin":
        _sleep_forever()

    prompt = sys.stdin.buffer.read()
    expected = os.environ.get("FAKE_EXPECTED_PROMPT")
    if expected is not None and prompt != expected.encode("utf-8"):
        return 23

    output = Path("output")
    proposals = output / "proposals"
    proposals.mkdir(parents=True, exist_ok=True)

    if mode == "nonzero":
        return 17
    if mode == "hang":
        _sleep_forever()
    if mode == "child-hang":
        child = subprocess.Popen([sys.executable, __file__, "--child"])
        marker = proposals / "child" / "child.pid"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(child.pid), encoding="ascii")
        _sleep_forever()
    if mode == "emit":
        _write_repeated(sys.stdout.buffer, b"o", _integer("FAKE_STDOUT_BYTES"))
        _write_repeated(sys.stderr.buffer, b"e", _integer("FAKE_STDERR_BYTES"))
    if mode == "emit-secrets":
        endpoint = os.environ["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT"].encode("utf-8")
        token = os.environ["PROBLEM_LOCATOR_LOGPARSE_TOKEN"].encode("utf-8")
        sys.stdout.buffer.write(b"prefix:" + endpoint[:3])
        sys.stdout.buffer.flush()
        time.sleep(0.02)
        sys.stdout.buffer.write(endpoint[3:] + b":" + token + b":suffix")
        sys.stdout.buffer.flush()
    if mode == "workspace-flood":
        target = proposals / "flood" / "payload.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"w" * _integer("FAKE_WORKSPACE_BYTES"))
        _sleep_forever()
    if mode == "root-flood":
        Path("bypass.bin").write_bytes(b"r" * _integer("FAKE_WORKSPACE_BYTES"))
        _sleep_forever()
    if mode == "part-only":
        (output / "job_outcome.json.part").write_bytes(b"partial")
        return 0

    payload = os.environ.get("FAKE_OUTCOME_JSON", "{}").encode("utf-8")
    temporary = output / ".job_outcome.json.part"
    temporary.write_bytes(payload)
    os.replace(temporary, output / "job_outcome.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
