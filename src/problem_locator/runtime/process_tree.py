"""Cross-platform child-process-tree lifecycle control.

POSIX children run in a fresh session/process group.  Windows children run in
a fresh process group and are assigned immediately to a kill-on-close Job
Object.  No code path intentionally degrades to parent-only termination.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

from .limits import PROCESS_TERMINATION_GRACE_SECONDS


_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_THREAD_SNAPSHOT = 0x00000004
_WINDOWS_JOB_BASIC_ACCOUNTING_INFORMATION_CLASS = 1


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


def _configure_windows_kernel32(kernel32: object) -> object:
    """Attach pointer-width-safe signatures to every Win32 call we use."""

    kernel32.CreateJobObjectW.argtypes = [  # type: ignore[attr-defined]
        ctypes.c_void_p,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE  # type: ignore[attr-defined]
    kernel32.SetInformationJobObject.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = (  # type: ignore[attr-defined]
        wintypes.BOOL
    )
    kernel32.AssignProcessToJobObject.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = (  # type: ignore[attr-defined]
        wintypes.BOOL
    )
    kernel32.QueryInformationJobObject.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    kernel32.QueryInformationJobObject.restype = (  # type: ignore[attr-defined]
        wintypes.BOOL
    )
    kernel32.TerminateJobObject.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        wintypes.UINT,
    ]
    kernel32.TerminateJobObject.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]  # type: ignore[attr-defined]
    kernel32.CloseHandle.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.argtypes = [  # type: ignore[attr-defined]
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = (  # type: ignore[attr-defined]
        wintypes.HANDLE
    )
    thread_entry_pointer = ctypes.POINTER(_THREADENTRY32)
    kernel32.Thread32First.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        thread_entry_pointer,
    ]
    kernel32.Thread32First.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.Thread32Next.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        thread_entry_pointer,
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.OpenThread.argtypes = [  # type: ignore[attr-defined]
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenThread.restype = wintypes.HANDLE  # type: ignore[attr-defined]
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]  # type: ignore[attr-defined]
    kernel32.ResumeThread.restype = wintypes.DWORD  # type: ignore[attr-defined]
    return kernel32


def _windows_kernel32() -> object:
    if sys.platform != "win32":
        raise ProcessTreeError("Windows process APIs are unavailable")
    return _configure_windows_kernel32(
        ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    )


class ProcessTreeError(RuntimeError):
    """A safely terminable execution group could not be created or reaped."""


class ManagedProcess:
    """A subprocess plus the platform primitive owning its descendants."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        process_group_id: int | None = None,
        windows_job: _WindowsJob | None = None,
    ) -> None:
        self.process = process
        self._process_group_id = process_group_id
        self._windows_job = windows_job
        self._closed = False
        self._cleanup_result: bool | None = None

    @property
    def stdin(self) -> BinaryIO:
        assert self.process.stdin is not None
        return self.process.stdin

    @property
    def stdout(self) -> BinaryIO:
        assert self.process.stdout is not None
        return self.process.stdout

    @property
    def stderr(self) -> BinaryIO:
        assert self.process.stderr is not None
        return self.process.stderr

    def terminate_tree(
        self, grace_seconds: float = PROCESS_TERMINATION_GRACE_SECONDS
    ) -> bool:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        if self._closed:
            return self._cleanup_result is True
        if os.name == "nt":
            try:
                clean = self._terminate_windows(grace_seconds)
            except ProcessTreeError:
                self._closed = True
                self._cleanup_result = False
                raise
        else:
            clean = self._terminate_posix(grace_seconds)
        if clean or os.name == "nt":
            self._closed = True
            self._cleanup_result = clean
        return clean

    def _terminate_posix(self, grace_seconds: float) -> bool:
        group_id = self._process_group_id
        if group_id is None:
            raise ProcessTreeError("POSIX process group was not established")
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            self._reap_parent(grace_seconds)
            return not _posix_group_exists(group_id)
        except OSError as exc:
            raise ProcessTreeError("failed to signal the POSIX process group") from exc

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            self.process.poll()
            if not _posix_group_exists(group_id):
                self._reap_parent(max(0.0, deadline - time.monotonic()))
                return True
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ProcessTreeError("failed to kill the POSIX process group") from exc
        self._reap_parent(grace_seconds)
        kill_deadline = time.monotonic() + grace_seconds
        while time.monotonic() < kill_deadline and _posix_group_exists(group_id):
            time.sleep(0.01)
        return not _posix_group_exists(group_id)

    def _terminate_windows(self, grace_seconds: float) -> bool:
        job = self._windows_job
        if job is None:
            raise ProcessTreeError("Windows Job Object was not established")
        deadline = time.monotonic() + grace_seconds
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                # TerminateJobObject below remains the authoritative hard stop.
                pass
        try:
            while time.monotonic() < deadline:
                self.process.poll()
                if job.active_processes() == 0:
                    break
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            if job.active_processes() != 0:
                job.terminate_all()
            empty = job.wait_empty(grace_seconds)
        except ProcessTreeError:
            # Kill-on-close remains the final containment guarantee even if a
            # Job query or explicit termination call fails.
            try:
                job.close()
            finally:
                self._reap_parent(grace_seconds)
            raise
        job.close()
        self._reap_parent(grace_seconds)
        return empty and self.process.poll() is not None

    def _reap_parent(self, timeout: float) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return

    def close_after_exit(self) -> bool:
        """Release ownership after normal exit, killing any lingering child."""

        if self._closed:
            return self._cleanup_result is True
        if os.name == "nt":
            job = self._windows_job
            if job is None:
                raise ProcessTreeError("Windows Job Object was not established")
            try:
                active = job.active_processes()
                if active == 0:
                    clean = self.process.poll() is not None
                else:
                    job.terminate_all()
                    job.wait_empty(PROCESS_TERMINATION_GRACE_SECONDS)
                    clean = False
                job.close()
            except ProcessTreeError:
                try:
                    job.close()
                finally:
                    self._reap_parent(PROCESS_TERMINATION_GRACE_SECONDS)
                    self._closed = True
                    self._cleanup_result = False
                raise
            self._reap_parent(PROCESS_TERMINATION_GRACE_SECONDS)
            self._closed = True
            self._cleanup_result = clean
            return clean
        group_id = self._process_group_id
        if group_id is None:
            self._closed = True
            self._cleanup_result = False
            return False
        # A reaped process group no longer exists.  If descendants retained
        # the group after the executable exited, clean them up and report that
        # normal completion was not clean.
        self.process.poll()
        if not _posix_group_exists(group_id):
            self._closed = True
            self._cleanup_result = True
            return True
        self.terminate_tree()
        self._closed = True
        # Descendants surviving the parent required forced cleanup, so normal
        # completion is unclean even when that cleanup succeeded.
        self._cleanup_result = False
        return False


def _posix_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spawn_managed_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> ManagedProcess:
    """Spawn without a shell and establish whole-tree ownership immediately."""

    if not argv:
        raise ProcessTreeError("argv must contain an executable")
    common: dict[str, object] = {
        "args": list(argv),
        "cwd": str(cwd),
        "env": dict(environment),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "bufsize": 0,
        "close_fds": True,
    }
    if os.name == "nt":
        common["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
        )
        try:
            process = subprocess.Popen(**common)  # type: ignore[arg-type]
        except OSError as exc:
            raise ProcessTreeError("failed to start the Agent process") from exc
        job: _WindowsJob | None = None
        try:
            job = _WindowsJob.create_and_assign(process)
            _resume_windows_process(process)
        except Exception as exc:
            if job is not None:
                try:
                    job.close()
                except ProcessTreeError:
                    pass
            _stop_parent_after_group_failure(process)
            raise ProcessTreeError(
                "failed to establish the Agent Windows execution group"
            ) from exc
        return ManagedProcess(process, windows_job=job)

    common["start_new_session"] = True
    try:
        process = subprocess.Popen(**common)  # type: ignore[arg-type]
    except OSError as exc:
        raise ProcessTreeError("failed to start the Agent process") from exc
    # ``start_new_session=True`` performs ``setsid()`` in the child before
    # exec and reports any pre-exec failure through Popen's error pipe.  The
    # resulting process-group id is therefore the child pid.  A very short
    # command may already have exited by the time Popen returns; treating
    # ``getpgid(pid) -> ESRCH`` as a start failure would reject valid runs.
    group_id = process.pid
    try:
        observed_group_id = os.getpgid(process.pid)
        if observed_group_id != group_id:
            raise ProcessTreeError("child is not leader of its new process group")
    except ProcessLookupError:
        # The process exited after a successfully established new session.
        pass
    except Exception as exc:
        _stop_posix_group_after_verification_failure(process, group_id)
        if isinstance(exc, ProcessTreeError):
            raise
        raise ProcessTreeError("failed to verify the POSIX process group") from exc
    return ManagedProcess(process, process_group_id=group_id)


def _stop_parent_after_group_failure(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _stop_posix_group_after_verification_failure(
    process: subprocess.Popen[bytes], group_id: int
) -> None:
    """Best-effort whole-group stop after a post-spawn verification failure."""

    try:
        os.killpg(group_id, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        # If the group was never established, the parent-only fallback below
        # is still needed; if it already vanished the calls are harmless.
        pass
    _stop_parent_after_group_failure(process)


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _WindowsJob:
    """Small ctypes owner for a kill-on-close Windows Job Object."""

    def __init__(self, handle: wintypes.HANDLE) -> None:
        self._handle = handle
        self._closed = False

    @classmethod
    def create_and_assign(cls, process: subprocess.Popen[bytes]) -> _WindowsJob:
        if sys.platform != "win32":
            raise ProcessTreeError("Windows Job Objects are unavailable")
        kernel32 = _windows_kernel32()
        raw_handle = kernel32.CreateJobObjectW(None, None)  # type: ignore[attr-defined]
        if not raw_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = wintypes.HANDLE(raw_handle)
        job = cls(handle)
        try:
            limits = _EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = wintypes.HANDLE(
                int(process._handle)  # type: ignore[attr-defined]
            )
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            job.close()
            raise
        return job

    def active_processes(self) -> int:
        if self._closed:
            return 0
        if sys.platform != "win32":
            raise ProcessTreeError("Windows Job Objects are unavailable")
        kernel32 = _windows_kernel32()
        information = _BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(
            self._handle,
            _WINDOWS_JOB_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            cause = ctypes.WinError(ctypes.get_last_error())
            raise ProcessTreeError("failed to query the Windows Job Object") from cause
        return int(information.ActiveProcesses)

    def terminate_all(self) -> None:
        if self._closed:
            return
        if sys.platform != "win32":
            raise ProcessTreeError("Windows Job Objects are unavailable")
        kernel32 = _windows_kernel32()
        if not kernel32.TerminateJobObject(self._handle, 1):
            cause = ctypes.WinError(ctypes.get_last_error())
            raise ProcessTreeError(
                "failed to terminate the Windows Job Object"
            ) from cause

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.active_processes() == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        if self._closed:
            return
        if sys.platform == "win32":
            kernel32 = _windows_kernel32()
            if not kernel32.CloseHandle(self._handle):
                cause = ctypes.WinError(ctypes.get_last_error())
                raise ProcessTreeError(
                    "failed to close the Windows Job Object"
                ) from cause
        self._closed = True


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the sole primary thread after Job assignment succeeds."""

    if sys.platform != "win32":
        raise ProcessTreeError("Windows thread control is unavailable")
    kernel32 = _windows_kernel32()
    raw_snapshot = kernel32.CreateToolhelp32Snapshot(  # type: ignore[attr-defined]
        _WINDOWS_THREAD_SNAPSHOT,
        0,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if not raw_snapshot or raw_snapshot == invalid_handle:
        cause = ctypes.WinError(ctypes.get_last_error())
        raise ProcessTreeError(
            "failed to enumerate the suspended Agent thread"
        ) from cause
    snapshot = wintypes.HANDLE(raw_snapshot)
    thread_ids: list[int] = []
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if int(entry.th32OwnerProcessID) == process.pid:
                thread_ids.append(int(entry.th32ThreadID))
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise ProcessTreeError("suspended Agent must have exactly one primary thread")

    raw_thread_handle = kernel32.OpenThread(  # type: ignore[attr-defined]
        _WINDOWS_THREAD_SUSPEND_RESUME,
        False,
        thread_ids[0],
    )
    if not raw_thread_handle:
        cause = ctypes.WinError(ctypes.get_last_error())
        raise ProcessTreeError("failed to open the suspended Agent thread") from cause
    thread_handle = wintypes.HANDLE(raw_thread_handle)
    try:
        previous_count = int(kernel32.ResumeThread(thread_handle))
        if previous_count != 1:
            raise ProcessTreeError("failed to resume the suspended Agent thread")
    finally:
        kernel32.CloseHandle(thread_handle)


__all__ = ["ManagedProcess", "ProcessTreeError", "spawn_managed_process"]
