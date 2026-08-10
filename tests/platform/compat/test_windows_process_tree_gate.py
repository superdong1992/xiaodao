from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
WINDOWS_PROCESS = ROOT / "tools/test-flow/adapters/windows-process.ps1"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    library.OpenProcess.restype = ctypes.c_void_p
    library.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    library.GetExitCodeProcess.restype = ctypes.c_int
    library.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    library.TerminateProcess.restype = ctypes.c_int
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int
    return library


def _process_active(pid: int) -> bool:
    library = _kernel32()
    handle = library.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        if not library.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        return exit_code.value == _STILL_ACTIVE
    finally:
        library.CloseHandle(handle)


def _terminate_exact_process(pid: int) -> None:
    library = _kernel32()
    handle = library.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE | 0x0001,
        False,
        pid,
    )
    if not handle:
        return
    try:
        exit_code = ctypes.c_uint32()
        if library.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == _STILL_ACTIVE:
            library.TerminateProcess(handle, 1)
    finally:
        library.CloseHandle(handle)


def _wait_absent(pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_active(pid):
            return True
        time.sleep(0.02)
    return not _process_active(pid)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows process gate")
def test_windows_job_object_cancellation_kills_descendants_and_seals_status(
    tmp_path: Path,
) -> None:
    working_directory = tmp_path / "working directory"
    working_directory.mkdir()
    child_pid_path = working_directory / "child pid.txt"
    cancel_path = tmp_path / "cancel"
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    status_path = tmp_path / "status.json"
    spec_path = tmp_path / "spec.json"
    parent_script = """
import os
from pathlib import Path
import subprocess
import sys
import time

time.sleep(0.5)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
print("TEST_FLOW_PROGRESS windows.child.started", flush=True)
time.sleep(120)
""".strip()
    spec = {
        "schema_version": 1,
        "executable": sys.executable,
        "arguments": ["-c", parent_script, os.fspath(child_pid_path)],
        "working_directory": os.fspath(working_directory),
        "environment": {"PYTHONIOENCODING": "utf-8"},
        "stdout_path": os.fspath(stdout_path),
        "stderr_path": os.fspath(stderr_path),
        "raw_log_limit_bytes": 1024 * 1024,
        "cancel_path": os.fspath(cancel_path),
    }
    spec_path.write_text(
        json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    wrapper = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(WINDOWS_PROCESS),
            "-SpecPath",
            os.fspath(spec_path),
            "-StatusPath",
            os.fspath(status_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 15.0
        while not child_pid_path.is_file():
            if wrapper.poll() is not None:
                wrapper_stdout, wrapper_stderr = wrapper.communicate()
                raise AssertionError(
                    f"Windows wrapper exited before child startup: {wrapper.returncode}\n"
                    f"stdout={wrapper_stdout}\nstderr={wrapper_stderr}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("Windows wrapper did not start the descendant")
            time.sleep(0.02)
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        cancel_path.write_text("terminate\n", encoding="ascii")
        wrapper_stdout, wrapper_stderr = wrapper.communicate(timeout=15.0)

        assert wrapper.returncode != 0, (wrapper_stdout, wrapper_stderr)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["schema_version"] == 1
        assert status["status"] == "EXITED"
        assert status["job_assigned"] is True
        assert status["controller_termination"] is True
        assert status["raw_log_limit_exceeded"] is False
        assert status["exit_code"] != 0
        assert "TEST_FLOW_PROGRESS windows.child.started" in stdout_path.read_text(
            encoding="utf-8"
        )
        assert stderr_path.read_bytes() == b""
        assert _wait_absent(int(status["process_id"]))
        assert _wait_absent(child_pid)
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            try:
                wrapper.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                wrapper.kill()
                wrapper.wait(timeout=5.0)
        if child_pid is not None and _process_active(child_pid):
            _terminate_exact_process(child_pid)
            _wait_absent(child_pid)
