from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
RELEASE_REQUIRED = "PROBLEM_LOCATOR_RELEASE_GATES_REQUIRED"


@pytest.mark.skipif(
    platform.system() not in {"Windows", "Darwin"},
    reason="the default native Client gate follows a Windows or macOS host",
)
def test_native_claude_host_sends_flat_inputs_without_hooks(
    tmp_path: Path,
) -> None:
    node = shutil.which("node.exe") or shutil.which("node")
    claude = shutil.which("claude.exe") or shutil.which("claude")
    if not node or not claude:
        if os.environ.get(RELEASE_REQUIRED) == "1":
            pytest.fail("the native Claude Code Host and Node.js are required")
        pytest.skip("the native Claude Code Host is unavailable")

    output = tmp_path / "host-capability"
    completed = subprocess.run(
        [
            node,
            os.fspath(ROOT / "tools/test-flow/adapters/host-capability.mjs"),
            "--repo-root",
            os.fspath(ROOT),
            "--output-root",
            os.fspath(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(
        (output / "host-capability-result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "PASS"
    assert result["client"] == ("windows" if os.name == "nt" else "macos")
    assert result["flat_schema"] is True
    assert result["flat_call"] is True
    assert result["client_dfx_absent"] is True
