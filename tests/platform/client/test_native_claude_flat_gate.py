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
RELEASE_CLAUDE_ENTRY = "PROBLEM_LOCATOR_RELEASE_CLAUDE_ENTRY"


@pytest.mark.skipif(
    platform.system() not in {"Windows", "Darwin"},
    reason="the default native Client gate follows a Windows or macOS host",
)
def test_native_claude_host_sends_flat_inputs_without_hooks(
    tmp_path: Path,
) -> None:
    node = shutil.which("node.exe") or shutil.which("node")
    claude_value = os.environ.get(RELEASE_CLAUDE_ENTRY)
    claude = Path(claude_value) if claude_value else None
    if not node or claude is None or not claude.is_absolute() or not claude.is_file():
        if os.environ.get(RELEASE_REQUIRED) == "1":
            pytest.fail("the explicit official npm Claude 2.1.89 cli.js and Node.js are required")
        pytest.skip("the explicit official npm Claude 2.1.89 cli.js is unavailable")

    output = tmp_path / "host-capability"
    completed = subprocess.run(
        [
            node,
            os.fspath(ROOT / "tools/test-flow/adapters/host-capability.mjs"),
            "--repo-root",
            os.fspath(ROOT),
            "--output-root",
            os.fspath(output),
            "--claude-entry",
            os.fspath(claude),
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
    assert result["claude_version"] == "2.1.89 (Claude Code)"
