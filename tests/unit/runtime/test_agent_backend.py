from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import threading
import time
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
        self.closed = True
        if self.fail_close:
            raise OSError("injected sink close failure")


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


def _assert_failure(exc_info: pytest.ExceptionInfo[RuntimeExecutionError], code: ErrorCode) -> None:
    assert exc_info.value.failure.code is code


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


def test_nonzero_exit_is_typed_and_does_not_read_business_output(tmp_path: Path) -> None:
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
def test_cancellation_terminates_execution(reason: CancellationReason, retryable: bool, tmp_path: Path) -> None:
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


def test_invalid_command_and_missing_workspace_map_to_start_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeExecutionError) as command_caught:
        _execute(AgentBackend("", parent_environment={}), _workspace(tmp_path / "one"))
    _assert_failure(command_caught, ErrorCode.CONFIG_INVALID)

    missing = tmp_path / "missing"
    with pytest.raises(RuntimeExecutionError) as workspace_caught:
        _execute(_backend("success"), missing)
    _assert_failure(workspace_caught, ErrorCode.BACKEND_START_FAILED)


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
