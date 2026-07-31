"""Shell-free, cancellation-aware execution of the pinned logparse CLI."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from problem_locator.contracts import CancellationReason, CancellationSignal


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    cancelled: bool
    cancellation_reason: CancellationReason | None
    start_failed: bool
    output_limited: bool


_MAX_CAPTURE_BYTES = 2_000_000
_READ_BYTES = 64 * 1024


def _drain_bounded(
    stream: BinaryIO,
    chunks: list[bytes],
    exceeded: threading.Event,
) -> None:
    retained = 0
    try:
        while True:
            chunk = stream.read(_READ_BYTES)
            if not chunk:
                return
            remaining = _MAX_CAPTURE_BYTES - retained
            if remaining > 0:
                kept = chunk[:remaining]
                chunks.append(kept)
                retained += len(kept)
            if len(chunk) > remaining:
                exceeded.set()
    except OSError:
        exceeded.set()


def sanitized_logparse_environment() -> dict[str, str]:
    reserved = {
        "logparse_repo",
        "logparse_config_path",
        "logparse_python",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.casefold() not in reserved
        and not key.casefold().startswith("problem_locator_logparse_")
    }


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort synchronous termination of one broker-owned process tree."""

    if process.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - exercised by S08 Windows gate
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


class SubprocessExecutor:
    """Execute argv arrays while exposing child ownership to one session."""

    def __init__(
        self,
        *,
        register: Callable[[subprocess.Popen[bytes]], None],
        unregister: Callable[[subprocess.Popen[bytes]], None],
        session_stopping: threading.Event,
    ) -> None:
        self._register = register
        self._unregister = unregister
        self._session_stopping = session_stopping

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        cancellation: CancellationSignal,
    ) -> ProcessResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("process argv must contain non-empty strings")
        if self._session_stopping.is_set() or cancellation.is_cancelled():
            return ProcessResult(
                returncode=None,
                stdout=b"",
                stderr=b"",
                cancelled=True,
                cancellation_reason=cancellation.reason,
                start_failed=False,
                output_limited=False,
            )
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":  # pragma: no cover - exercised by S08 Windows gate
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=os.fspath(cwd),
                env=sanitized_logparse_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=start_new_session,
                creationflags=creationflags,
            )
        except OSError:
            return ProcessResult(
                returncode=None,
                stdout=b"",
                stderr=b"",
                cancelled=False,
                cancellation_reason=None,
                start_failed=True,
                output_limited=False,
            )

        self._register(process)
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            terminate_process_tree(process)
            self._unregister(process)
            return ProcessResult(
                returncode=process.returncode,
                stdout=b"",
                stderr=b"",
                cancelled=False,
                cancellation_reason=None,
                start_failed=True,
                output_limited=False,
            )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_limited = threading.Event()
        stderr_limited = threading.Event()
        stdout_reader = threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout_chunks, stdout_limited),
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr_chunks, stderr_limited),
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()
        was_cancelled = False
        try:
            while True:
                try:
                    process.wait(timeout=0.05)
                    was_cancelled = (
                        self._session_stopping.is_set() or cancellation.is_cancelled()
                    )
                    break
                except subprocess.TimeoutExpired:
                    if self._session_stopping.is_set() or cancellation.is_cancelled():
                        was_cancelled = True
                        terminate_process_tree(process)
                        break
            stdout_reader.join(timeout=2.0)
            stderr_reader.join(timeout=2.0)
            if stdout_reader.is_alive() or stderr_reader.is_alive():
                stdout_limited.set()
                stderr_limited.set()
                process.stdout.close()
                process.stderr.close()
                stdout_reader.join(timeout=1.0)
                stderr_reader.join(timeout=1.0)
            return ProcessResult(
                returncode=process.returncode,
                stdout=b"".join(stdout_chunks),
                stderr=b"".join(stderr_chunks),
                cancelled=was_cancelled,
                cancellation_reason=cancellation.reason if was_cancelled else None,
                start_failed=False,
                output_limited=stdout_limited.is_set() or stderr_limited.is_set(),
            )
        finally:
            self._unregister(process)


__all__ = [
    "ProcessResult",
    "SubprocessExecutor",
    "sanitized_logparse_environment",
    "terminate_process_tree",
]
