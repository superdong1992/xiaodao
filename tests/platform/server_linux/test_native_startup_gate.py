from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from problem_locator.contracts import (
    ReadinessReport,
    StateExport,
    ValidationReport,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_SKILL_FIXTURE = ROOT / "tests/fixtures/components/runtime-catalog/skill-dir"
OFFICIAL_KEYS = {
    "BIND_HOST",
    "CLAUDE_COMMAND",
    "DATA_ROOT",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
    "LOGPARSE_REPO",
    "PORT",
    "PUBLIC_BASE_URL",
    "SKILL_DIR",
}


def _required_absolute_path(name: str) -> Path:
    raw = os.environ.get(name)
    assert raw, f"{name} is required for the native startup gate"
    path = Path(raw)
    assert path.is_absolute(), f"{name} must be absolute"
    return path


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    assert set(values) == OFFICIAL_KEYS
    path.write_text(
        "".join(
            f"{key}={_dotenv_value(values[key])}\n" for key in sorted(values)
        ),
        encoding="utf-8",
    )


def _wait_for_json(
    process: subprocess.Popen[str],
    url: str,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[int, dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(
                f"service exited before startup completed (exit={exit_code}): "
                f"{stderr[-2000:]}"
            )
        try:
            with urlopen(url, timeout=0.5) as response:  # noqa: S310 - loopback gate
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (UnicodeDecodeError, ValueError):
                body = {}
            if url.endswith("/ready") and exc.code == 503:
                last_error = exc
                time.sleep(0.05)
                continue
            return exc.code, body
        except (OSError, URLError, ValueError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"loopback endpoint did not become ready: {type(last_error).__name__}")


def _stop_service(process: subprocess.Popen[str], expected_system: str) -> None:
    if process.poll() is not None:
        assert process.returncode == 0
        return
    if expected_system == "Windows":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        # Exercise Uvicorn's normal interactive shutdown path.  This is the
        # portable POSIX equivalent of the Windows CTRL_BREAK below.
        process.send_signal(signal.SIGINT)
    try:
        exit_code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("service did not complete bounded shutdown") from None
    assert exit_code == 0


def _start_service(env_file: Path, child_env: dict[str, str], expected_system: str):
    creationflags = 0
    if expected_system == "Windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    launcher = Path("/evidence/test_service_launcher.py")
    if not launcher.is_file():
        launcher = ROOT / "tools/test-flow/runtime-support/test_service_launcher.py"
    assert launcher.is_file()
    return subprocess.Popen(
        [
            sys.executable,
            "-I",
            os.fspath(launcher),
            "serve",
            "--env-file",
            os.fspath(env_file),
        ],
        cwd=ROOT,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )


def _assert_service_ready(process: subprocess.Popen[str], port: int) -> None:
    live_status, live = _wait_for_json(
        process,
        f"http://127.0.0.1:{port}/live",
    )
    assert live_status == 200
    assert live == {"ok": True, "data": {"status": "live"}, "error": None}

    ready_status, ready = _wait_for_json(
        process,
        f"http://127.0.0.1:{port}/ready",
    )
    assert ready_status == 200
    assert ready["ok"] is True and ready["error"] is None
    report = ReadinessReport.model_validate(ready["data"])
    assert report.ready is True
    assert [check.name for check in report.checks] == [
        "CONFIG",
        "INSTANCE_LOCK",
        "STATE",
        "DATA_DIRECTORIES",
        "RECOVERY",
    ]


def _run_native_startup_gate(expected_system: str, tmp_path: Path) -> None:
    assert platform.system() == expected_system
    assert os.environ.get("S08_NATIVE_STARTUP_GATE") == expected_system.lower()

    skill_dir = Path(os.environ.get("SKILL_DIR", PRODUCTION_SKILL_FIXTURE))
    assert skill_dir.is_absolute()
    logparse_repo = _required_absolute_path("LOGPARSE_REPO")
    logparse_config = _required_absolute_path("LOGPARSE_CONFIG_PATH")
    logparse_python = _required_absolute_path("LOGPARSE_PYTHON")

    data_root = tmp_path / "data"
    export_path = tmp_path / "state-export.json"
    port = _free_loopback_port()
    values = {
        "DATA_ROOT": os.fspath(data_root),
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "BIND_HOST": "127.0.0.1",
        "PORT": str(port),
        "CLAUDE_COMMAND": os.environ.get("CLAUDE_COMMAND", "claude"),
        "SKILL_DIR": os.fspath(skill_dir),
        "LOGPARSE_REPO": os.fspath(logparse_repo),
        "LOGPARSE_CONFIG_PATH": os.fspath(logparse_config),
        "LOGPARSE_PYTHON": os.fspath(logparse_python),
    }
    env_file = tmp_path / "native-startup.env"
    _write_env_file(env_file, values)
    child_env = os.environ.copy()
    for key in OFFICIAL_KEYS:
        child_env.pop(key, None)

    first = _start_service(env_file, child_env, expected_system)
    try:
        _assert_service_ready(first, port)
    finally:
        _stop_service(first, expected_system)

    validated = subprocess.run(
        [
            sys.executable,
            "-m",
            "problem_locator",
            "validate-state",
            "--data-root",
            os.fspath(data_root),
        ],
        cwd=ROOT,
        env=child_env,
        check=True,
        capture_output=True,
    )
    report = ValidationReport.model_validate_json(validated.stdout)
    assert validated.stdout == canonical_json_bytes(report)
    assert validated.stderr == b""
    assert report.valid is True and report.errors == []

    exported = subprocess.run(
        [
            sys.executable,
            "-m",
            "problem_locator",
            "export-state",
            "--data-root",
            os.fspath(data_root),
            "--output",
            os.fspath(export_path),
        ],
        cwd=ROOT,
        env=child_env,
        check=True,
        capture_output=True,
    )
    assert exported.stdout == b"" and exported.stderr == b""
    state_export = StateExport.model_validate_json(export_path.read_bytes())
    assert export_path.read_bytes() == canonical_json_bytes(state_export)
    assert state_export.object_counts.cases == 0

    second = _start_service(env_file, child_env, expected_system)
    try:
        _assert_service_ready(second, port)
    finally:
        _stop_service(second, expected_system)


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="requires an explicitly configured native Linux runner",
)
def test_native_linux_startup_gate(tmp_path: Path) -> None:
    _run_native_startup_gate("Linux", tmp_path)
