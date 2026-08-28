from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import ModuleType

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the Linux Client browser runner requires POSIX process groups",
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RUNNER = (
    REPOSITORY_ROOT
    / "tools"
    / "test-flow"
    / "runtime-support"
    / "linux_client_browser_runner.py"
)


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_linux_client_browser_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)


def _process_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_for_process_absence(pid: int, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not _process_absent(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _unused_ipv4_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _summary(stderr: str) -> dict[str, object]:
    prefix = "TEST_FLOW_BROWSER_EXECUTION_V1="
    lines = [line for line in stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1
    return json.loads(base64.b64decode(lines[0][len(prefix) :]))


def test_browser_timeout_kills_and_reaps_the_ready_process_group_once(tmp_path: Path) -> None:
    runner = _load_runner()
    fake_browser = tmp_path / "fake-browser-hang"
    ready = tmp_path / "grandchild-ready.json"
    launches = tmp_path / "launches.log"
    _write_executable(
        fake_browser,
        """import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ready = Path(sys.argv[1])
launches = Path(sys.argv[2])
with launches.open("a", encoding="utf-8") as stream:
    stream.write(f"{os.getpid()}\\n")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child_source = '''import json, os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "x", encoding="utf-8") as stream:
    json.dump({"pid": os.getpid(), "process_group": os.getpgrp()}, stream)
while True:
    time.sleep(1)
'''
subprocess.Popen([sys.executable, "-c", child_source, str(ready)])
deadline = time.monotonic() + 5
while not ready.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("grandchild readiness timeout")
    time.sleep(0.01)
while True:
    time.sleep(1)
""",
    )

    result = runner.run_browser_process(
        [str(fake_browser), str(ready), str(launches)],
        timeout_seconds=0.5,
        termination_grace_seconds=0.1,
        absence_grace_seconds=2.0,
    )

    launch_ids = launches.read_text(encoding="utf-8").splitlines()
    assert len(launch_ids) == 1
    grandchild = json.loads(ready.read_text(encoding="utf-8"))
    assert result["timed_out"] is True
    assert result["returncode"] == -signal.SIGKILL
    assert result["process_tree"] == {
        "strategy": "posix-process-group-v1",
        "session_started": True,
        "termination_reason": "TIMEOUT",
        "term_sent": True,
        "kill_sent": True,
        "parent_reaped": True,
        "group_absent": True,
    }
    assert grandchild["process_group"] == int(launch_ids[0])
    assert _wait_for_process_absence(grandchild["pid"])


def test_normal_exit_reaps_exited_descendants_without_false_residual(tmp_path: Path) -> None:
    runner = _load_runner()
    fake_browser = tmp_path / "fake-browser-exited-child"
    _write_executable(
        fake_browser,
        """import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", "pass"])
time.sleep(0.1)
""",
    )

    result = runner.run_browser_process(
        [str(fake_browser)],
        timeout_seconds=2.0,
        termination_grace_seconds=0.1,
        absence_grace_seconds=2.0,
    )

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["process_tree"] == {
        "strategy": "posix-process-group-v1",
        "session_started": True,
        "termination_reason": "NONE",
        "term_sent": False,
        "kill_sent": False,
        "parent_reaped": True,
        "group_absent": True,
    }


def test_runner_serves_ipv4_page_to_fake_browser_and_emits_closed_receipt(tmp_path: Path) -> None:
    page_root = tmp_path / "page"
    page_root.mkdir()
    page = '<!doctype html><html data-result="QQ=="><body>probe</body></html>\n'
    (page_root / "index.html").write_text(page, encoding="utf-8")
    fake_browser = tmp_path / "fake-browser-get"
    _write_executable(
        fake_browser,
        """import sys
import urllib.request

url = sys.argv[-1]
with urllib.request.urlopen(url, timeout=5) as response:
    sys.stdout.buffer.write(response.read())
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--chrome",
            str(fake_browser),
            "--directory",
            str(page_root),
            "--port",
            str(_unused_ipv4_port()),
            "--label",
            "capability",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == page
    summary = _summary(completed.stderr)
    assert summary["wrapper_status"] == "PASS"
    assert summary["argument_profile"] == "chrome-headless-shell-for-testing-local-v1"
    assert summary["browser_started"] is True
    assert summary["browser_exit_code"] == 0
    assert summary["timed_out"] is False
    assert summary["cleanup"] == {
        "http_server_stopped": True,
        "profile_removed": True,
        "process_tree": {
            "strategy": "posix-process-group-v1",
            "session_started": True,
            "termination_reason": "NONE",
            "term_sent": False,
            "kill_sent": False,
            "parent_reaped": True,
            "group_absent": True,
        },
    }
