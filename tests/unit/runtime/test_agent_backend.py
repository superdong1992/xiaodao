from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import shlex
import stat
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CancellationReason,
    ErrorCode,
    ExecutionLogSinks,
    FixtureManifest,
    JOB_STDOUT_STDERR_BYTES,
    ResourceLimits,
    default_resource_limits,
)
from problem_locator.runtime import agent_backend as backend_module
from problem_locator.runtime import process_tree as process_tree_module
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.failures import RuntimeExecutionError


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/runtime-backend"
FAKE_CLAUDE = FIXTURE_ROOT / "fake_claude.py"


class _Signal:
    def __init__(self, reason: CancellationReason | None = None) -> None:
        self._reason = reason
        self._event = threading.Event()
        if reason is not None:
            self._event.set()

    @property
    def reason(self) -> CancellationReason | None:
        return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self._event.wait(timeout_seconds)

    def cancel(self, reason: CancellationReason) -> None:
        if self._reason is None:
            self._reason = reason
            self._event.set()


class _Sink:
    def __init__(self, *, fail_write: bool = False, fail_close: bool = False) -> None:
        self.data = bytearray()
        self.fail_write = fail_write
        self.fail_close = fail_close
        self.closed = False
        self.close_calls = 0

    def write(self, chunk: bytes) -> None:
        if self.fail_write:
            raise OSError("injected sink write failure")
        if self.closed:
            raise ValueError("closed")
        self.data.extend(chunk)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("closed")

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.fail_close:
            raise OSError("injected sink close failure")


class _TrackingSink(_Sink):
    def __init__(self, write_observed: threading.Event | None = None) -> None:
        super().__init__()
        self.write_observed = write_observed
        self.write_after_close = False
        self.events: list[str] = []

    def write(self, chunk: bytes) -> None:
        if self.closed:
            self.write_after_close = True
        super().write(chunk)
        self.events.append("write")
        if self.write_observed is not None:
            self.write_observed.set()

    def close(self) -> None:
        self.events.append("close")
        super().close()


class _PollAfterEvent:
    def __init__(self, ready: threading.Event) -> None:
        self._ready = ready
        self.returncode = 0

    def poll(self) -> int | None:
        return 0 if self._ready.is_set() else None


class _HeldOpenManagedProcess:
    """Exited parent whose descendants still own stdout/stderr write ends."""

    def __init__(self, payload: bytes, ready: threading.Event) -> None:
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.write(stdout_write, payload)
        self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
        self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
        self.process = _PollAfterEvent(ready)
        self._retained = (stdin_read, stdout_write, stderr_write)

    def terminate_tree(self, grace_seconds: float) -> bool:
        del grace_seconds
        return False

    def close_after_exit(self) -> bool:
        return False

    def close_retained(self) -> None:
        for file_descriptor in self._retained:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


class _WinFunctionStub:
    def __init__(self) -> None:
        self.argtypes: list[object] | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        return 1


class _Kernel32Stub:
    def __init__(self) -> None:
        for name in (
            "AssignProcessToJobObject",
            "CloseHandle",
            "CreateJobObjectW",
            "CreateToolhelp32Snapshot",
            "OpenThread",
            "QueryInformationJobObject",
            "ResumeThread",
            "SetInformationJobObject",
            "TerminateJobObject",
            "Thread32First",
            "Thread32Next",
        ):
            setattr(self, name, _WinFunctionStub())


class _ShortWritePipe:
    def __init__(self, maximum_write: int) -> None:
        self.maximum_write = maximum_write
        self.data = bytearray()
        self.flushed = False
        self.closed = False

    def write(self, chunk: bytes) -> int:
        accepted = min(self.maximum_write, len(chunk))
        self.data.extend(chunk[:accepted])
        return accepted

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "inputs").mkdir(parents=True)
    (root / "runtime" / "tool-state").mkdir(parents=True)
    (root / "output" / "proposals").mkdir(parents=True)
    return root


def _sinks(
    *, stdout: _Sink | None = None, stderr: _Sink | None = None
) -> tuple[ExecutionLogSinks, _Sink, _Sink]:
    stdout = stdout or _Sink()
    stderr = stderr or _Sink()
    return (
        ExecutionLogSinks(
            stdout=stdout,
            stderr=stderr,
            combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
        ),
        stdout,
        stderr,
    )


def _limits(
    *, wall_time: float = 2.0, output: int = 1024 * 1024, workspace: int = 1024 * 1024
) -> BackendExecutionLimits:
    return BackendExecutionLimits(
        wall_time_seconds=wall_time,
        stdout_stderr_bytes=output,
        workspace_bytes=workspace,
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.5,
    )


def _backend(mode: str, **environment: str) -> AgentBackend:
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))}"
    return AgentBackend(
        command,
        parent_environment={"FAKE_CLAUDE_MODE": mode, **environment},
    )


def _execute(
    backend: AgentBackend,
    workspace: Path,
    *,
    prompt: str = "bounded prompt",
    cancellation: _Signal | None = None,
    limits: BackendExecutionLimits | None = None,
    sinks: ExecutionLogSinks | None = None,
    broker_environment: dict[str, str] | None = None,
):
    actual_sinks = sinks or _sinks()[0]
    return backend.execute(
        prompt=prompt,
        workspace_root=workspace,
        cancellation=cancellation or _Signal(),
        log_sinks=actual_sinks,
        resource_limits=default_resource_limits("DIAGNOSE"),
        broker_environment=broker_environment,
        test_limits=limits or _limits(),
    )


def _assert_failure(
    exc_info: pytest.ExceptionInfo[RuntimeExecutionError],
    code: ErrorCode,
) -> None:
    assert exc_info.value.failure.code is code


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL(  # type: ignore[attr-defined]
            "kernel32",
            use_last_error=True,
        )
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        raw_handle = kernel32.OpenProcess(0x00100000 | 0x001000, False, pid)
        if not raw_handle:
            return ctypes.get_last_error() == 5
        handle = wintypes.HANDLE(raw_handle)
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _agent_thread_identities() -> set[int]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.ident is not None
        and thread.name.startswith("problem-locator-agent-")
    }


def test_success_uses_stdin_fresh_process_and_atomic_final_name(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    prompt = "context with Unicode: 诊断"
    backend = _backend(
        "success",
        FAKE_EXPECTED_PROMPT=prompt,
        FAKE_OUTCOME_JSON='{"ok":true}',
    )

    result = _execute(backend, root, prompt=prompt)

    assert result.returncode == 0
    assert (root / "output/job_outcome.json").read_bytes() == b'{"ok":true}'
    assert not (root / "output/.job_outcome.json.part").exists()


def test_success_logs_bounded_agent_completion_metrics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    root = _workspace(tmp_path)
    result = _execute(
        _backend(
            "emit",
            FAKE_STDOUT_BYTES="7",
            FAKE_STDERR_BYTES="5",
            FAKE_OUTCOME_JSON='{"ok":true}',
        ),
        root,
    )

    completed = next(
        record
        for record in caplog.records
        if getattr(record, "dfx_event", "") == "runtime.agent_backend.completed"
    )
    assert completed.dfx_fields["returncode"] == 0
    assert completed.dfx_fields["stdout_stderr_bytes"] == 12
    assert completed.dfx_fields["workspace_bytes"] == result.workspace_bytes
    assert completed.dfx_fields["elapsed_seconds"] == result.elapsed_seconds
    assert "argv" not in completed.dfx_fields
    assert "environment" not in completed.dfx_fields


@pytest.mark.skipif(os.name == "nt", reason="POSIX workspace mode assertion")
def test_agent_process_starts_with_nonwritable_workspace_root_and_restores_it(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    original_mode = stat.S_IMODE(root.stat().st_mode)
    observed_mode: int | None = None

    def observing_factory(*args: object, **kwargs: object) -> object:
        nonlocal observed_mode
        observed_mode = stat.S_IMODE(root.stat().st_mode)
        return process_tree_module.spawn_managed_process(*args, **kwargs)

    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))}"
    backend = AgentBackend(
        command,
        parent_environment={
            "FAKE_CLAUDE_MODE": "success",
            "FAKE_OUTCOME_JSON": '{"ok":true}',
        },
        process_factory=observing_factory,  # type: ignore[arg-type]
    )

    _execute(backend, root)

    assert observed_mode is not None
    assert observed_mode & 0o222 == 0
    assert stat.S_IMODE(root.stat().st_mode) == original_mode
    assert (root / "output/job_outcome.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX workspace mode assertion")
def test_workspace_root_mode_is_restored_when_process_start_fails(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    original_mode = stat.S_IMODE(root.stat().st_mode)
    observed_mode: int | None = None

    def failing_factory(*args: object, **kwargs: object) -> object:
        nonlocal observed_mode
        del args, kwargs
        observed_mode = stat.S_IMODE(root.stat().st_mode)
        raise process_tree_module.ProcessTreeError("injected process start failure")

    backend = AgentBackend(
        "fake-agent",
        parent_environment={},
        process_factory=failing_factory,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(backend, root)

    _assert_failure(caught, ErrorCode.BACKEND_START_FAILED)
    assert observed_mode is not None and observed_mode & 0o222 == 0
    assert stat.S_IMODE(root.stat().st_mode) == original_mode


def test_nonzero_exit_is_typed_and_does_not_read_business_output(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(_backend("nonzero"), root)
    _assert_failure(caught, ErrorCode.BACKEND_EXIT_FAILED)
    assert not (root / "output/job_outcome.json").exists()


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        (CancellationReason.USER_CANCEL, False),
        (CancellationReason.SERVICE_SHUTDOWN, True),
    ],
)
def test_cancellation_terminates_execution(
    reason: CancellationReason,
    retryable: bool,
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    cancellation = _Signal()
    timer = threading.Timer(0.1, cancellation.cancel, args=(reason,))
    timer.start()
    try:
        with pytest.raises(RuntimeExecutionError) as caught:
            _execute(_backend("hang"), root, cancellation=cancellation)
    finally:
        timer.cancel()
    _assert_failure(caught, ErrorCode.BACKEND_CANCELLED)
    assert caught.value.failure.retryable is retryable


def test_cancellation_while_descendant_holds_exited_parent_pipes_wins(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    cancellation = _Signal()
    timer = threading.Timer(
        0.1,
        cancellation.cancel,
        args=(CancellationReason.USER_CANCEL,),
    )
    timer.start()
    try:
        with pytest.raises(RuntimeExecutionError) as caught:
            _execute(
                _backend("child-after-parent-exit"),
                root,
                cancellation=cancellation,
            )
    finally:
        timer.cancel()

    _assert_failure(caught, ErrorCode.BACKEND_CANCELLED)
    child_pid = int((root / "output/proposals/child/child.pid").read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_exists(child_pid):
        time.sleep(0.02)
    assert not _pid_exists(child_pid)


def test_initial_cancellation_prevents_spawn(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    called = False

    def forbidden_factory(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("must not spawn")

    backend = AgentBackend(
        "ignored",
        parent_environment={},
        process_factory=forbidden_factory,
    )
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            backend,
            root,
            cancellation=_Signal(CancellationReason.USER_CANCEL),
        )
    _assert_failure(caught, ErrorCode.BACKEND_CANCELLED)
    assert called is False


def test_prompt_writer_retries_short_writes() -> None:
    pipe = _ShortWritePipe(maximum_write=3)
    state = backend_module._InputState()

    backend_module._write_prompt(pipe, "short writes: 诊断", state)

    assert state.failure is None
    assert bytes(pipe.data) == "short writes: 诊断".encode("utf-8")
    assert pipe.flushed
    assert pipe.closed
    assert state.finished.is_set()


def test_held_open_pipes_stop_before_redactor_and_sink_close(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    write_observed = threading.Event()
    stdout = _TrackingSink(write_observed)
    stderr = _TrackingSink()
    sinks = ExecutionLogSinks(
        stdout=stdout,
        stderr=stderr,
        combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
    )
    secret = "token-value"
    payload = b"ready-marker:tok"
    managed = _HeldOpenManagedProcess(payload, write_observed)
    before = _agent_thread_identities()

    def process_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return managed

    backend = AgentBackend(
        "fake-agent",
        parent_environment={},
        process_factory=process_factory,  # type: ignore[arg-type]
    )
    broker_environment = {
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "ep",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN": secret,
    }
    try:
        with pytest.raises(RuntimeExecutionError) as caught:
            _execute(
                backend,
                root,
                sinks=sinks,
                broker_environment=broker_environment,
                limits=BackendExecutionLimits(
                    wall_time_seconds=0.5,
                    stdout_stderr_bytes=1024,
                    workspace_bytes=1024 * 1024,
                    poll_interval_seconds=0.005,
                    termination_grace_seconds=0.05,
                ),
            )
        observed_threads = _agent_thread_identities()
        observed_stdout = bytes(stdout.data)
        observed_events = tuple(stdout.events)
        observed_write_after_close = stdout.write_after_close
    finally:
        managed.close_retained()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not (
            _agent_thread_identities() <= before
        ):
            time.sleep(0.01)

    _assert_failure(caught, ErrorCode.BACKEND_EXIT_FAILED)
    assert observed_threads <= before
    assert observed_stdout == payload
    assert observed_events[-1] == "close"
    assert "write" not in observed_events[observed_events.index("close") + 1 :]
    assert observed_write_after_close is False
    assert stdout.close_calls == 1
    assert stderr.close_calls == 1


def test_timeout_cannot_be_blocked_by_agent_ignoring_stdin(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    prompt = "x" * 204_800
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            _backend("ignore-stdin"),
            root,
            prompt=prompt,
            limits=_limits(wall_time=0.15),
        )
    _assert_failure(caught, ErrorCode.BACKEND_TIMEOUT)
    assert time.monotonic() - started < 2.0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_timeout_terminates_complete_posix_child_tree(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            _backend("child-hang"),
            root,
            limits=_limits(wall_time=0.25),
        )
    _assert_failure(caught, ErrorCode.BACKEND_TIMEOUT)
    child_pid = int((root / "output/proposals/child/child.pid").read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_exists(child_pid):
        time.sleep(0.02)
    assert not _pid_exists(child_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object assertion")
def test_timeout_terminates_complete_windows_child_tree(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            _backend("child-hang"),
            root,
            limits=_limits(wall_time=0.25),
        )
    _assert_failure(caught, ErrorCode.BACKEND_TIMEOUT)
    child_pid = int((root / "output/proposals/child/child.pid").read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_exists(child_pid):
        time.sleep(0.02)
    assert not _pid_exists(child_pid)


def test_windows_kernel32_signatures_are_pointer_width_safe() -> None:
    kernel32 = _Kernel32Stub()

    configured = process_tree_module._configure_windows_kernel32(kernel32)

    assert configured is kernel32
    assert kernel32.CreateJobObjectW.restype is wintypes.HANDLE
    assert kernel32.SetInformationJobObject.argtypes[0] is wintypes.HANDLE
    assert kernel32.AssignProcessToJobObject.argtypes == [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    assert kernel32.QueryInformationJobObject.argtypes[0] is wintypes.HANDLE
    assert kernel32.TerminateJobObject.argtypes[0] is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CreateToolhelp32Snapshot.restype is wintypes.HANDLE
    assert kernel32.Thread32First.argtypes[0] is wintypes.HANDLE
    assert kernel32.Thread32Next.argtypes[0] is wintypes.HANDLE
    assert kernel32.OpenThread.restype is wintypes.HANDLE
    assert kernel32.ResumeThread.argtypes == [wintypes.HANDLE]


def test_windows_spawn_assigns_suspended_process_before_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    popen_arguments: dict[str, object] = {}

    class FakeProcess:
        pid = 321
        _handle = 654

    fake_process = FakeProcess()
    fake_job = object()

    def fake_popen(**kwargs: object) -> FakeProcess:
        events.append("spawn")
        popen_arguments.update(kwargs)
        return fake_process

    def fake_assign(cls: type[object], process: object) -> object:
        assert process is fake_process
        events.append("assign")
        return fake_job

    def fake_resume(process: object) -> None:
        assert process is fake_process
        events.append("resume")

    with monkeypatch.context() as scoped:
        scoped.setattr(process_tree_module.os, "name", "nt")
        scoped.setattr(
            process_tree_module.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0x00000200,
            raising=False,
        )
        scoped.setattr(process_tree_module.subprocess, "Popen", fake_popen)
        scoped.setattr(
            process_tree_module._WindowsJob,
            "create_and_assign",
            classmethod(fake_assign),
        )
        scoped.setattr(process_tree_module, "_resume_windows_process", fake_resume)
        managed = process_tree_module.spawn_managed_process(
            ["agent.exe"],
            cwd=tmp_path,
            environment={},
        )

    assert managed.process is fake_process
    assert events == ["spawn", "assign", "resume"]
    creationflags = popen_arguments["creationflags"]
    assert isinstance(creationflags, int)
    assert creationflags & process_tree_module._WINDOWS_CREATE_SUSPENDED
    assert creationflags & 0x00000200


def test_stdout_stderr_combined_limit_exact_boundary(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    sinks, stdout, stderr = _sinks()
    result = _execute(
        _backend("emit", FAKE_STDOUT_BYTES="31", FAKE_STDERR_BYTES="33"),
        root,
        limits=_limits(output=64),
        sinks=sinks,
    )
    assert result.stdout_stderr_bytes == 64
    assert len(stdout.data) + len(stderr.data) == 64


def test_stdout_stderr_combined_limit_overflow_terminates(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            _backend("emit", FAKE_STDOUT_BYTES="65"),
            root,
            limits=_limits(output=64),
        )
    _assert_failure(caught, ErrorCode.BACKEND_OUTPUT_LIMIT)


def test_workspace_limit_and_root_shape_bypass_are_rejected(tmp_path: Path) -> None:
    for mode in ("workspace-flood", "root-flood"):
        root = _workspace(tmp_path / mode)
        with pytest.raises(RuntimeExecutionError) as caught:
            _execute(
                _backend(mode, FAKE_WORKSPACE_BYTES="2048"),
                root,
                limits=_limits(workspace=1024),
            )
        _assert_failure(caught, ErrorCode.WORKSPACE_LIMIT)


def test_broker_secrets_are_redacted_before_sinks(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    sinks, stdout, _ = _sinks()
    broker_environment = {
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "endpoint-value",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "token-value",
    }
    _execute(
        _backend("emit-secrets"),
        root,
        sinks=sinks,
        broker_environment=broker_environment,
    )
    logged = bytes(stdout.data)
    assert b"endpoint-value" not in logged
    assert b"token-value" not in logged
    assert logged == b"prefix:" + b"*" * 14 + b":" + b"*" * 11 + b":suffix"


def test_sink_write_and_close_failures_are_normalized(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "write")
    sinks, _, _ = _sinks(stdout=_Sink(fail_write=True))
    with pytest.raises(RuntimeExecutionError) as write_caught:
        _execute(
            _backend("emit", FAKE_STDOUT_BYTES="1"),
            root,
            sinks=sinks,
        )
    _assert_failure(write_caught, ErrorCode.EXECUTION_RECORD_FAILED)

    root = _workspace(tmp_path / "close")
    sinks, _, _ = _sinks(stdout=_Sink(fail_close=True))
    with pytest.raises(RuntimeExecutionError) as close_caught:
        _execute(_backend("success"), root, sinks=sinks)
    _assert_failure(close_caught, ErrorCode.EXECUTION_RECORD_FAILED)


def test_redactor_tail_sink_failure_does_not_replace_timeout(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    sinks, _, _ = _sinks(stdout=_Sink(fail_write=True))
    broker_environment = {
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT": "endpoint-value",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN": "token-value",
    }

    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            _backend("emit-secret-prefix-hang"),
            root,
            sinks=sinks,
            broker_environment=broker_environment,
            limits=_limits(wall_time=0.15),
        )

    _assert_failure(caught, ErrorCode.BACKEND_TIMEOUT)
    assert any(
        detail.field == "execution_log" for detail in caught.value.failure.details
    )


def test_shared_sink_is_closed_once_on_early_failure(tmp_path: Path) -> None:
    shared = _Sink()
    sinks = ExecutionLogSinks(
        stdout=shared,
        stderr=shared,
        combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
    )

    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(
            AgentBackend("", parent_environment={}),
            _workspace(tmp_path),
            sinks=sinks,
        )

    _assert_failure(caught, ErrorCode.CONFIG_INVALID)
    assert shared.close_calls == 1


def test_invalid_command_and_missing_workspace_map_to_start_failure(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeExecutionError) as command_caught:
        _execute(AgentBackend("", parent_environment={}), _workspace(tmp_path / "one"))
    _assert_failure(command_caught, ErrorCode.CONFIG_INVALID)

    missing = tmp_path / "missing"
    with pytest.raises(RuntimeExecutionError) as workspace_caught:
        _execute(_backend("success"), missing)
    _assert_failure(workspace_caught, ErrorCode.BACKEND_START_FAILED)


def test_workspace_symlink_is_rejected_before_process_factory(
    tmp_path: Path,
) -> None:
    target = _workspace(tmp_path / "target")
    linked = tmp_path / "linked-workspace"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symbolic links are unavailable")
    calls = 0

    def forbidden_process_factory(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise AssertionError("unsafe Workspace must not spawn")

    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))}"
    backend = AgentBackend(
        command,
        parent_environment={"FAKE_CLAUDE_MODE": "success"},
        process_factory=forbidden_process_factory,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeExecutionError) as caught:
        _execute(backend, linked)

    _assert_failure(caught, ErrorCode.BACKEND_START_FAILED)
    assert calls == 0


@pytest.mark.skipif(os.name == "nt", reason="open directory rename semantics differ")
@pytest.mark.parametrize("swap_scope", ["root", "ancestor"])
def test_workspace_measurement_fails_safely_on_identity_swap(
    swap_scope: str,
    tmp_path: Path,
) -> None:
    container = tmp_path / "original-container"
    root = _workspace(container)
    outside_container = tmp_path / "outside-container"
    outside = _workspace(outside_container)
    (outside / "output/proposals/outside.bin").write_bytes(b"x" * 4096)
    identity = backend_module._capture_workspace_identity(root)
    assert identity is not None
    probe_calls = 0

    def swap_during_scan() -> None:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls != 2:
            return
        if swap_scope == "root":
            moved_root = container / "workspace-moved"
            root.rename(moved_root)
            root.symlink_to(outside, target_is_directory=True)
        else:
            moved_container = tmp_path / "original-container-moved"
            container.rename(moved_container)
            container.symlink_to(outside_container, target_is_directory=True)

    with pytest.raises(RuntimeExecutionError) as caught:
        backend_module._temporary_workspace_bytes(
            root,
            limit=1024,
            identity=identity,
            failure_probe=swap_during_scan,
            allow_transient_changes=True,
        )

    _assert_failure(caught, ErrorCode.WORKSPACE_LIMIT)
    assert probe_calls >= 2


def test_closed_managed_process_never_signals_a_reused_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 0

        def poll(self) -> int:
            return 0

    managed = process_tree_module.ManagedProcess(  # type: ignore[arg-type]
        Process(),
        process_group_id=777,
    )
    calls = 0

    def terminate_once(grace_seconds: float) -> bool:
        nonlocal calls
        del grace_seconds
        calls += 1
        return True

    with monkeypatch.context() as scoped:
        scoped.setattr(process_tree_module.os, "name", "posix")
        scoped.setattr(process_tree_module, "_posix_group_exists", lambda _: True)
        scoped.setattr(managed, "_terminate_posix", terminate_once)
        assert managed.close_after_exit() is False
        assert managed.terminate_tree(0.0) is False

    assert calls == 1


def test_production_limits_come_unchanged_from_frozen_resource_limits() -> None:
    frozen: ResourceLimits = default_resource_limits("DIAGNOSE")
    accelerated = BackendExecutionLimits.from_resource_limits(frozen)
    assert accelerated.wall_time_seconds == 1800
    assert accelerated.stdout_stderr_bytes == 67_108_864
    assert accelerated.workspace_bytes == 1_073_741_824


def test_backend_fixture_manifest_is_exact() -> None:
    raw = json.loads((FIXTURE_ROOT / "fixture-manifest.json").read_text())
    manifest = FixtureManifest.model_validate(raw)
    actual = sorted(
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path.name != "fixture-manifest.json"
    )
    assert [entry.path for entry in manifest.files] == actual
    for entry in manifest.files:
        data = (FIXTURE_ROOT / entry.path).read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
