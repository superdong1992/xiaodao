#!/usr/bin/env python3
"""Run the pinned Linux Client browser and emit a closed, redacted process receipt."""

from __future__ import annotations

import argparse
import base64
import ctypes
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional


SUMMARY_PREFIX = "TEST_FLOW_BROWSER_EXECUTION_V1="
ARGUMENT_PROFILE = "chrome-headless-shell-for-testing-local-v1"
SAFE_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
BROWSER_TIMEOUT_SECONDS = 45.0
PROCESS_GROUP_GRACE_SECONDS = 1.0
PROCESS_GROUP_ABSENCE_SECONDS = 1.0
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def digest(value: bytes) -> dict[str, Any]:
    return {
        "byte_count": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "truncated": False,
    }


def signal_name(number: int) -> Optional[str]:
    try:
        return signal.Signals(number).name
    except ValueError:
        return None


def writable_home() -> dict[str, Any]:
    home = os.environ.get("HOME")
    present = isinstance(home, str) and bool(home)
    realpath = str(Path(home).resolve(strict=True)) if present else None
    writable = False
    if present:
        descriptor, probe = tempfile.mkstemp(prefix=".test-flow-home-", dir=home)
        os.close(descriptor)
        os.unlink(probe)
        writable = True
    return {"path": home, "realpath": realpath, "present": present, "writable": writable}


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def emit_summary(value: dict[str, Any]) -> None:
    encoded = base64.b64encode(canonical_bytes(value)).decode("ascii")
    sys.stderr.write(f"{SUMMARY_PREFIX}{encoded}\n")
    sys.stderr.flush()


def process_group_present(process_group: int) -> bool:
    """Return whether this runner's dedicated POSIX process group still exists."""
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def child_subreaper_state() -> Optional[bool]:
    if sys.platform != "linux":
        return None
    state = ctypes.c_int(0)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(state), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return bool(state.value)


def set_child_subreaper(enabled: bool) -> None:
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def reap_process_group_children(process_group: int) -> None:
    while True:
        try:
            child, _status = os.waitpid(-process_group, os.WNOHANG)
        except ChildProcessError:
            return
        if child == 0:
            return


def wait_for_process_group_absence(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        reap_process_group_children(process_group)
        if not process_group_present(process_group):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def signal_process_group(process_group: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return False
    return True


def run_browser_process(
    command: list[str],
    *,
    timeout_seconds: float = BROWSER_TIMEOUT_SECONDS,
    termination_grace_seconds: float = PROCESS_GROUP_GRACE_SECONDS,
    absence_grace_seconds: float = PROCESS_GROUP_ABSENCE_SECONDS,
) -> dict[str, Any]:
    """Run one browser in a private session and seal its whole process group."""
    previous_subreaper = child_subreaper_state()
    if previous_subreaper is False:
        set_child_subreaper(True)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        if previous_subreaper is False:
            set_child_subreaper(False)
        raise
    process_group = process.pid
    timed_out = False
    termination_reason = "NONE"
    term_sent = False
    kill_sent = False

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        termination_reason = "TIMEOUT"
        term_sent = signal_process_group(process_group, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=termination_grace_seconds)
        except subprocess.TimeoutExpired:
            kill_sent = signal_process_group(process_group, signal.SIGKILL)
            # SIGKILL is terminal for every member of this dedicated process group.
            # An unbounded final communicate is intentional: it reaps the direct
            # child and drains both pipes rather than sealing partial evidence.
            stdout, stderr = process.communicate()

    parent_reaped = process.poll() is not None
    # Chrome children that exited with the browser may be reparented to this
    # temporary subreaper. Reap those zombies before deciding that a live
    # residual process tree needs termination.
    reap_process_group_children(process_group)
    if process_group_present(process_group):
        if termination_reason == "NONE":
            termination_reason = "RESIDUAL_AFTER_EXIT"
        if not term_sent:
            term_sent = signal_process_group(process_group, signal.SIGTERM)
        if not wait_for_process_group_absence(process_group, termination_grace_seconds):
            kill_sent = signal_process_group(process_group, signal.SIGKILL) or kill_sent

    group_absent = wait_for_process_group_absence(process_group, absence_grace_seconds)
    if previous_subreaper is False and group_absent:
        set_child_subreaper(False)
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "process_tree": {
            "strategy": "posix-process-group-v1",
            "session_started": True,
            "termination_reason": termination_reason,
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "parent_reaped": parent_reaped,
            "group_absent": group_absent,
        },
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--label", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    directory = Path(arguments.directory).resolve(strict=True)
    chrome = Path(arguments.chrome).resolve(strict=True)
    if not directory.is_dir() or not chrome.is_file():
        raise ValueError("BROWSER_RUNNER_INPUT_INVALID")
    if not 1 <= arguments.port <= 65535 or not SAFE_LABEL.fullmatch(arguments.label):
        raise ValueError("BROWSER_RUNNER_ARGUMENT_INVALID")

    stdout = b""
    stderr = b""
    browser_started = False
    browser_exit_code: Optional[int] = None
    browser_signal_number: Optional[int] = None
    browser_signal_name: Optional[str] = None
    timed_out = False
    wrapper_status = "PASS"
    failure_code: Optional[str] = None
    server_stopped = False
    profile_removed = False
    server: Optional[http.server.ThreadingHTTPServer] = None
    server_thread: Optional[threading.Thread] = None
    profile: Optional[str] = None
    process_tree = {
        "strategy": "posix-process-group-v1",
        "session_started": False,
        "termination_reason": "NONE",
        "term_sent": False,
        "kill_sent": False,
        "parent_reaped": False,
        "group_absent": True,
    }
    home: dict[str, Any]

    try:
        home = writable_home()
        handler = functools.partial(QuietHandler, directory=str(directory))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", arguments.port), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        profile = tempfile.mkdtemp(prefix=f"test-flow-chrome-profile-{arguments.label}-", dir="/tmp")
        command = [
            str(chrome),
            "--headless=new",
            "--no-sandbox",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-gpu",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--no-proxy-server",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=30000",
            "--dump-dom",
            f"http://127.0.0.1:{arguments.port}/",
        ]
        browser_started = True
        completed = run_browser_process(command)
        stdout = completed["stdout"]
        stderr = completed["stderr"]
        timed_out = completed["timed_out"]
        process_tree = completed["process_tree"]
        if not timed_out:
            if completed["returncode"] < 0:
                browser_signal_number = -completed["returncode"]
                browser_signal_name = signal_name(browser_signal_number)
            else:
                browser_exit_code = completed["returncode"]
        if not (process_tree["parent_reaped"] and process_tree["group_absent"]):
            wrapper_status = "ERROR"
            failure_code = "PROCESS_TREE_CLEANUP_FAILED"
    except Exception as error:  # The receipt intentionally carries no exception text.
        home = {"path": os.environ.get("HOME"), "realpath": None, "present": bool(os.environ.get("HOME")), "writable": False}
        wrapper_status = "ERROR"
        failure_code = type(error).__name__.upper()
    finally:
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
            server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=5)
            server_stopped = not (server_thread and server_thread.is_alive())
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
            profile_removed = not Path(profile).exists()

    summary = {
        "schema_version": 1,
        "wrapper_status": wrapper_status,
        "failure_code": failure_code,
        "label": arguments.label,
        "argument_profile": ARGUMENT_PROFILE,
        "home": home,
        "browser_started": browser_started,
        "browser_exit_code": browser_exit_code,
        "browser_signal_number": browser_signal_number,
        "browser_signal_name": browser_signal_name,
        "timed_out": timed_out,
        "stdout": digest(stdout),
        "stderr": digest(stderr),
        "cleanup": {
            "http_server_stopped": server_stopped,
            "profile_removed": profile_removed,
            "process_tree": process_tree,
        },
    }
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    emit_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
