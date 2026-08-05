"""Subprocess Agent Backend with bounded logs, time, and workspace output."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from problem_locator.contracts.enums import (
    CancellationReason,
    ErrorCode,
    ExecutionStage,
)
from problem_locator.contracts.models import (
    ApplicationErrorDetail,
    ExecutionFailure,
    ExecutionLogSinks,
    ResourceLimits,
)
from problem_locator.contracts.ports import AppendOnlyByteSink, CancellationSignal
from problem_locator.journey import record_stage_completed, record_stage_started

from .claude_command import ClaudeCommandError, prepare_claude_command
from .failures import RuntimeExecutionError, runtime_failure
from .process_tree import ManagedProcess, ProcessTreeError, spawn_managed_process
from .secret_redactor import StreamingSecretRedactor


_PIPE_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class BackendExecutionLimits:
    """Injectable accelerators whose production values come from the Job."""

    wall_time_seconds: float
    stdout_stderr_bytes: int
    workspace_bytes: int
    poll_interval_seconds: float = 0.05
    termination_grace_seconds: float = 5.0

    @classmethod
    def from_resource_limits(cls, limits: ResourceLimits) -> BackendExecutionLimits:
        return cls(
            wall_time_seconds=float(limits.wall_time_seconds),
            stdout_stderr_bytes=limits.stdout_stderr_bytes,
            workspace_bytes=limits.workspace_bytes,
        )

    def __post_init__(self) -> None:
        if self.wall_time_seconds <= 0:
            raise ValueError("wall_time_seconds must be positive")
        if self.stdout_stderr_bytes <= 0 or self.workspace_bytes <= 0:
            raise ValueError("byte limits must be positive")
        if self.poll_interval_seconds <= 0 or self.termination_grace_seconds < 0:
            raise ValueError("poll/grace values are invalid")


@dataclass(frozen=True, slots=True)
class BackendExecution:
    returncode: int
    stdout_stderr_bytes: int
    workspace_bytes: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class _WorkspaceIdentity:
    root: tuple[int, int]
    top_level: tuple[tuple[str, int, int], ...]

    def top_level_identity(self, name: str) -> tuple[int, int] | None:
        for candidate, device, inode in self.top_level:
            if candidate == name:
                return device, inode
        return None


@dataclass(frozen=True, slots=True)
class _WorkspaceRootWriteGuard:
    descriptor: int
    original_mode: int


class _OutputState:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.failure: BaseException | None = None
        self.limit_exceeded = False
        self.lock = threading.Lock()
        self.failed = threading.Event()

    def reserve(self, size: int) -> int:
        """Return how many bytes remain legal for this chunk."""

        with self.lock:
            remaining = max(0, self.limit - self.total)
            accepted = min(size, remaining)
            self.total += size
            if size > remaining:
                self.limit_exceeded = True
                self.failed.set()
            return accepted

    def record_failure(self, failure: BaseException) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = failure
            self.failed.set()


class _InputState:
    def __init__(self) -> None:
        self.failure: BaseException | None = None
        self.finished = threading.Event()


class _OwnedSink:
    """Idempotent owner around one public execution-log sink."""

    def __init__(self, sink: AppendOnlyByteSink) -> None:
        self._sink = sink
        self._closed = False
        self.finalization_failed = False
        self._lock = threading.RLock()

    def write(self, chunk: bytes) -> None:
        with self._lock:
            if self._closed:
                self.finalization_failed = True
                raise ValueError("execution log sink owner is closed")
            try:
                self._sink.write(chunk)
            except BaseException:
                self.finalization_failed = True
                raise

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                self.finalization_failed = True
                raise ValueError("execution log sink owner is closed")
            try:
                self._sink.flush()
            except BaseException:
                self.finalization_failed = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._sink.close()
            except BaseException:
                self.finalization_failed = True
                raise


class AgentBackend:
    """Run one fresh external Agent process for one Job."""

    def __init__(
        self,
        command: str,
        *,
        parent_environment: dict[str, str] | None = None,
        process_factory: Callable[..., ManagedProcess] = spawn_managed_process,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._command = command
        self._parent_environment = (
            None if parent_environment is None else dict(parent_environment)
        )
        self._process_factory = process_factory
        self._monotonic = monotonic

    def execute(
        self,
        *,
        prompt: str,
        workspace_root: Path,
        cancellation: CancellationSignal,
        log_sinks: ExecutionLogSinks,
        resource_limits: ResourceLimits,
        broker_environment: dict[str, str] | None = None,
        test_limits: BackendExecutionLimits | None = None,
    ) -> BackendExecution:
        stdout_sink = _OwnedSink(log_sinks.stdout)
        stderr_sink = (
            stdout_sink
            if log_sinks.stderr is log_sinks.stdout
            else _OwnedSink(log_sinks.stderr)
        )
        owned_sinks = tuple(
            dict.fromkeys((stdout_sink, stderr_sink))
        )
        owned_log_sinks = ExecutionLogSinks(
            stdout=stdout_sink,
            stderr=stderr_sink,
            combined_limit_bytes=log_sinks.combined_limit_bytes,
        )
        result: BackendExecution | None = None
        failure: ExecutionFailure | None = None
        try:
            result = self._execute_owned(
                prompt=prompt,
                workspace_root=workspace_root,
                cancellation=cancellation,
                log_sinks=owned_log_sinks,
                resource_limits=resource_limits,
                broker_environment=broker_environment,
                test_limits=test_limits,
            )
        except RuntimeExecutionError as exc:
            failure = exc.failure
        except Exception:
            failure = ExecutionFailure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="Agent Backend failed unexpectedly.",
                retryable=False,
                details=[],
            )
        finally:
            for sink in owned_sinks:
                try:
                    sink.close()
                except BaseException:
                    pass

        if any(sink.finalization_failed for sink in owned_sinks):
            if failure is None:
                failure = ExecutionFailure(
                    stage=ExecutionStage.EXECUTION_RECORD,
                    code=ErrorCode.EXECUTION_RECORD_FAILED,
                    message="Execution log could not be finalized.",
                    retryable=True,
                    details=[],
                )
            else:
                failure = _append_execution_log_detail(failure)
        if failure is not None:
            raise RuntimeExecutionError(failure) from None
        assert result is not None
        return result

    def _execute_owned(
        self,
        *,
        prompt: str,
        workspace_root: Path,
        cancellation: CancellationSignal,
        log_sinks: ExecutionLogSinks,
        resource_limits: ResourceLimits,
        broker_environment: dict[str, str] | None = None,
        test_limits: BackendExecutionLimits | None = None,
    ) -> BackendExecution:
        limits = test_limits or BackendExecutionLimits.from_resource_limits(
            resource_limits
        )
        backend_start_observed = record_stage_started(
            ExecutionStage.BACKEND_START,
            data={"workspace_root": workspace_root},
        )
        if cancellation.is_cancelled():
            raise RuntimeExecutionError(_cancelled_failure(cancellation.reason))
        try:
            invocation = prepare_claude_command(
                self._command,
                parent_environment=self._parent_environment,
                broker_environment=broker_environment,
            )
        except ClaudeCommandError as exc:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_START,
                code=exc.code,
                message="CLAUDE_COMMAND configuration is invalid.",
            ) from exc

        root = Path(workspace_root)
        workspace_identity = _capture_workspace_identity(root)
        if workspace_identity is None:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_START,
                code=ErrorCode.BACKEND_START_FAILED,
                message="Agent Workspace is unavailable.",
                retryable=True,
            )
        secrets = tuple(
            value.encode("utf-8")
            for value in (broker_environment or {}).values()
            if value
        )
        stdout_sink = StreamingSecretRedactor(
            secrets, log_sinks.stdout, close_sink=False
        )
        stderr_sink = StreamingSecretRedactor(
            secrets, log_sinks.stderr, close_sink=False
        )
        output_state = _OutputState(limits.stdout_stderr_bytes)
        input_state = _InputState()
        root_write_guard = _protect_workspace_root(root, workspace_identity)
        try:
            managed = self._process_factory(
                invocation.argv,
                cwd=root,
                environment=invocation.environment,
            )
        except ProcessTreeError as exc:
            _restore_workspace_root(root_write_guard)
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_START,
                code=ErrorCode.BACKEND_START_FAILED,
                message="Agent process execution group could not be created.",
                retryable=True,
            ) from exc
        except BaseException:
            _restore_workspace_root(root_write_guard)
            raise

        primary_failure: ExecutionFailure | None = None
        started = 0.0
        backend_execution_observed: float | None = None
        workspace_bytes = 0
        tree_released = False
        readers: tuple[threading.Thread, ...] = ()
        lifecycle_threads: tuple[threading.Thread, ...] = ()
        io_stop = threading.Event()
        pipes = (managed.stdin, managed.stdout, managed.stderr)
        try:
            try:
                stdin_fd, stdout_fd, stderr_fd = tuple(
                    _set_pipe_nonblocking(pipe) for pipe in pipes
                )
            except (AttributeError, OSError, TypeError, ValueError):
                primary_failure = ExecutionFailure(
                    stage=ExecutionStage.BACKEND_START,
                    code=ErrorCode.BACKEND_START_FAILED,
                    message="Agent process pipes could not be configured safely.",
                    retryable=True,
                    details=[],
                )
            else:
                readers = (
                    threading.Thread(
                        target=_drain_pipe,
                        args=(managed.stdout, stdout_sink, output_state),
                        kwargs={
                            "file_descriptor": stdout_fd,
                            "stop": io_stop,
                            "poll_interval_seconds": limits.poll_interval_seconds,
                        },
                        name="problem-locator-agent-stdout",
                    ),
                    threading.Thread(
                        target=_drain_pipe,
                        args=(managed.stderr, stderr_sink, output_state),
                        kwargs={
                            "file_descriptor": stderr_fd,
                            "stop": io_stop,
                            "poll_interval_seconds": limits.poll_interval_seconds,
                        },
                        name="problem-locator-agent-stderr",
                    ),
                )
                input_writer = threading.Thread(
                    target=_write_prompt,
                    args=(managed.stdin, prompt, input_state),
                    kwargs={
                        "file_descriptor": stdin_fd,
                        "stop": io_stop,
                        "poll_interval_seconds": limits.poll_interval_seconds,
                    },
                    name="problem-locator-agent-stdin",
                )
                lifecycle_threads = (input_writer, *readers)
                started = self._monotonic()
                for thread in lifecycle_threads:
                    thread.start()
                record_stage_completed(
                    ExecutionStage.BACKEND_START,
                    backend_start_observed,
                    data={
                        "argv": invocation.argv,
                        "process_id": getattr(managed.process, "pid", None),
                    },
                )
                backend_execution_observed = record_stage_started(
                    ExecutionStage.BACKEND_EXECUTE,
                    data={"process_id": getattr(managed.process, "pid", None)},
                )

            while primary_failure is None and managed.process.poll() is None:
                if cancellation.is_cancelled():
                    primary_failure = _cancelled_failure(cancellation.reason)
                    break
                if input_state.failure is not None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message=(
                            "Agent process closed stdin before receiving the prompt."
                        ),
                        retryable=False,
                        details=[],
                    )
                    break
                if output_state.failed.is_set():
                    primary_failure = _output_failure(output_state)
                    break
                elapsed = self._monotonic() - started
                if elapsed >= limits.wall_time_seconds:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_TIMEOUT,
                        message="Agent execution exceeded the fixed wall time.",
                        retryable=True,
                        details=[],
                    )
                    break
                try:
                    workspace_bytes = _temporary_workspace_bytes(
                        root,
                        limit=limits.workspace_bytes,
                        deadline=started + limits.wall_time_seconds,
                        cancellation=cancellation,
                        monotonic=self._monotonic,
                        identity=workspace_identity,
                        failure_probe=lambda: _background_io_failure(
                            input_state,
                            output_state,
                        ),
                        allow_transient_changes=True,
                    )
                except RuntimeExecutionError as exc:
                    primary_failure = exc.failure
                    break
                if workspace_bytes > limits.workspace_bytes:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.WORKSPACE_LIMIT,
                        message="Agent Workspace exceeded the fixed byte limit.",
                        retryable=False,
                        details=[],
                    )
                    break
                cancellation.wait(
                    min(
                        limits.poll_interval_seconds,
                        max(0.0, limits.wall_time_seconds - elapsed),
                    )
                )

            if primary_failure is None:
                # The executable has exited. Drain its final pipe bytes before
                # deciding whether output limits or sink failures won the race.
                primary_failure = _wait_for_lifecycle_threads(
                    lifecycle_threads,
                    input_state=input_state,
                    output_state=output_state,
                    cancellation=cancellation,
                    started=started,
                    limits=limits,
                    monotonic=self._monotonic,
                )
                if primary_failure is None and any(
                    thread.is_alive() for thread in lifecycle_threads
                ):
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent execution pipes remained open after exit.",
                        retryable=False,
                        details=[],
                    )
                elif primary_failure is None and managed.process.returncode != 0:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent process exited unsuccessfully.",
                        retryable=False,
                        details=[],
                    )

            if primary_failure is not None:
                primary_failure = _terminate_with_diagnostic(
                    managed, primary_failure, limits.termination_grace_seconds
                )
            try:
                clean = managed.close_after_exit()
            except ProcessTreeError:
                clean = False
            tree_released = clean
            if not clean:
                if primary_failure is None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent process tree did not exit cleanly.",
                        retryable=False,
                        details=[],
                    )
                primary_failure = _append_cleanup_detail(primary_failure)
        except RuntimeExecutionError as exc:
            primary_failure = primary_failure or exc.failure
        except Exception:
            # No implementation-specific exception text may cross the Runtime
            # seam.  The finally block below still owns whole-tree cleanup.
            primary_failure = primary_failure or ExecutionFailure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="Agent execution failed unexpectedly.",
                retryable=False,
                details=[],
            )
        finally:
            if not tree_released:
                if primary_failure is None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent process tree could not be released.",
                        retryable=False,
                        details=[],
                    )
                primary_failure = _terminate_with_diagnostic(
                    managed, primary_failure, limits.termination_grace_seconds
                )
                try:
                    if not managed.close_after_exit():
                        primary_failure = _append_cleanup_detail(primary_failure)
                except ProcessTreeError:
                    primary_failure = _append_cleanup_detail(primary_failure)

            # No process in the owned tree can now produce useful output.
            # Stop nonblocking pipe workers and confirm they have exited
            # before closing either redactor or either public sink.
            io_stop.set()
            _join_threads(lifecycle_threads, limits.termination_grace_seconds)
            if any(thread.is_alive() for thread in lifecycle_threads):
                _join_threads_until_stopped(lifecycle_threads)
            _close_pipes(pipes)

            sink_failure: BaseException | None = None
            for sink in (stdout_sink, stderr_sink):
                try:
                    sink.close()
                except BaseException as exc:  # sink failures must be normalized
                    sink_failure = sink_failure or exc
            if sink_failure is not None:
                if primary_failure is None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.EXECUTION_RECORD,
                        code=ErrorCode.EXECUTION_RECORD_FAILED,
                        message="Execution log could not be finalized.",
                        retryable=True,
                        details=[],
                    )
                else:
                    primary_failure = _append_execution_log_detail(primary_failure)

            try:
                _restore_workspace_root(root_write_guard)
            except RuntimeExecutionError as exc:
                primary_failure = primary_failure or exc.failure

        if primary_failure is not None:
            raise RuntimeExecutionError(primary_failure)
        workspace_bytes = _temporary_workspace_bytes(
            root,
            limit=limits.workspace_bytes,
            deadline=started + limits.wall_time_seconds,
            cancellation=cancellation,
            monotonic=self._monotonic,
            identity=workspace_identity,
        )
        if workspace_bytes > limits.workspace_bytes:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.WORKSPACE_LIMIT,
                message="Agent Workspace exceeded the fixed byte limit.",
            )
        result = BackendExecution(
            returncode=managed.process.returncode or 0,
            stdout_stderr_bytes=output_state.total,
            workspace_bytes=workspace_bytes,
            elapsed_seconds=max(0.0, self._monotonic() - started),
        )
        assert backend_execution_observed is not None
        record_stage_completed(
            ExecutionStage.BACKEND_EXECUTE,
            backend_execution_observed,
            data={
                "returncode": result.returncode,
                "stdout_stderr_bytes": result.stdout_stderr_bytes,
                "workspace_bytes": result.workspace_bytes,
                "elapsed_seconds": result.elapsed_seconds,
            },
        )
        return result


def _join_threads(threads: tuple[threading.Thread, ...], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        if thread.ident is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _join_threads_until_stopped(threads: tuple[threading.Thread, ...]) -> None:
    for thread in threads:
        if thread.ident is not None:
            while thread.is_alive():
                thread.join(timeout=0.05)


def _close_pipes(pipes: tuple[object, ...]) -> None:
    for pipe in pipes:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except BaseException:
            pass


def _set_pipe_nonblocking(pipe: object) -> int:
    file_descriptor = pipe.fileno()  # type: ignore[attr-defined]
    if isinstance(file_descriptor, bool) or not isinstance(file_descriptor, int):
        raise TypeError("Agent pipe file descriptor is invalid")
    os.set_blocking(file_descriptor, False)
    return file_descriptor


def _write_prompt(
    pipe: object,
    prompt: str,
    state: _InputState,
    *,
    file_descriptor: int | None = None,
    stop: threading.Event | None = None,
    poll_interval_seconds: float = 0.01,
) -> None:
    def stopped() -> bool:
        return stop is not None and stop.is_set()

    def wait_to_retry() -> None:
        if stop is None:
            time.sleep(poll_interval_seconds)
        else:
            stop.wait(poll_interval_seconds)

    try:
        payload = prompt.encode("utf-8")
        offset = 0
        while offset < len(payload):
            if stopped():
                break
            try:
                if file_descriptor is None:
                    written = pipe.write(payload[offset:])  # type: ignore[attr-defined]
                else:
                    written = os.write(file_descriptor, payload[offset:])
            except BlockingIOError:
                wait_to_retry()
                continue
            if not isinstance(written, int) or written <= 0:
                raise BrokenPipeError("Agent stdin did not accept prompt bytes")
            offset += written
        if not stopped():
            pipe.flush()  # type: ignore[attr-defined]
    except BaseException as exc:
        if not stopped():
            state.failure = exc
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except BaseException as exc:
            if state.failure is None and not stopped():
                state.failure = exc
        state.finished.set()


def _drain_pipe(
    pipe: object,
    sink: StreamingSecretRedactor,
    state: _OutputState,
    *,
    file_descriptor: int | None = None,
    stop: threading.Event | None = None,
    poll_interval_seconds: float = 0.01,
) -> None:
    def stopped() -> bool:
        return stop is not None and stop.is_set()

    def wait_to_retry() -> None:
        if stop is None:
            time.sleep(poll_interval_seconds)
        else:
            stop.wait(poll_interval_seconds)

    try:
        while not stopped():
            try:
                if file_descriptor is None:
                    chunk = pipe.read(_PIPE_CHUNK_BYTES)  # type: ignore[attr-defined]
                else:
                    chunk = os.read(file_descriptor, _PIPE_CHUNK_BYTES)
            except BlockingIOError:
                wait_to_retry()
                continue
            if not chunk:
                break
            accepted = state.reserve(len(chunk))
            if accepted:
                sink.write(chunk[:accepted])
            if accepted != len(chunk):
                break
    except BaseException as exc:
        if not stopped():
            state.record_failure(exc)
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except BaseException:
            pass


def _output_failure(state: _OutputState) -> ExecutionFailure:
    if state.limit_exceeded:
        return ExecutionFailure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.BACKEND_OUTPUT_LIMIT,
            message="Agent stdout/stderr exceeded the fixed byte limit.",
            retryable=False,
            details=[],
        )
    return ExecutionFailure(
        stage=ExecutionStage.EXECUTION_RECORD,
        code=ErrorCode.EXECUTION_RECORD_FAILED,
        message="Execution log could not be written.",
        retryable=True,
        details=[],
    )


def _background_io_failure(
    input_state: _InputState,
    output_state: _OutputState,
) -> ExecutionFailure | None:
    if input_state.failure is not None:
        return ExecutionFailure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.BACKEND_EXIT_FAILED,
            message="Agent process closed stdin before receiving the prompt.",
            retryable=False,
            details=[],
        )
    if output_state.failed.is_set():
        return _output_failure(output_state)
    return None


def _wait_for_lifecycle_threads(
    threads: tuple[threading.Thread, ...],
    *,
    input_state: _InputState,
    output_state: _OutputState,
    cancellation: CancellationSignal,
    started: float,
    limits: BackendExecutionLimits,
    monotonic: Callable[[], float],
) -> ExecutionFailure | None:
    cleanup_deadline = time.monotonic() + limits.termination_grace_seconds
    execution_deadline = started + limits.wall_time_seconds
    while any(thread.is_alive() for thread in threads):
        if cancellation.is_cancelled():
            return _cancelled_failure(cancellation.reason)
        io_failure = _background_io_failure(input_state, output_state)
        if io_failure is not None:
            return io_failure
        execution_remaining = execution_deadline - monotonic()
        if execution_remaining <= 0:
            return ExecutionFailure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_TIMEOUT,
                message="Agent execution exceeded the fixed wall time.",
                retryable=True,
                details=[],
            )
        cleanup_remaining = cleanup_deadline - time.monotonic()
        if cleanup_remaining <= 0:
            break
        _join_threads(
            threads,
            min(
                limits.poll_interval_seconds,
                execution_remaining,
                cleanup_remaining,
            ),
        )
    if cancellation.is_cancelled():
        return _cancelled_failure(cancellation.reason)
    io_failure = _background_io_failure(input_state, output_state)
    if io_failure is not None:
        return io_failure
    if monotonic() >= execution_deadline:
        return ExecutionFailure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.BACKEND_TIMEOUT,
            message="Agent execution exceeded the fixed wall time.",
            retryable=True,
            details=[],
        )
    return None


def _cancelled_failure(reason: CancellationReason | None) -> ExecutionFailure:
    return ExecutionFailure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_CANCELLED,
        message="Agent execution was cancelled.",
        retryable=reason is CancellationReason.SERVICE_SHUTDOWN,
        details=[],
    )


def _terminate_with_diagnostic(
    managed: ManagedProcess,
    failure: ExecutionFailure,
    grace_seconds: float,
) -> ExecutionFailure:
    try:
        clean = managed.terminate_tree(grace_seconds)
    except ProcessTreeError:
        clean = False
    return failure if clean else _append_cleanup_detail(failure)


def _append_cleanup_detail(failure: ExecutionFailure) -> ExecutionFailure:
    if any(
        detail.field == "process_tree"
        and detail.resource_type == "BACKEND"
        and detail.actual == "residual"
        for detail in failure.details
    ):
        return failure
    detail = ApplicationErrorDetail(
        field="process_tree",
        resource_type="BACKEND",
        resource_id=None,
        resource_ref=None,
        expected="terminated",
        actual="residual",
        limit=None,
        observed=None,
    )
    return ExecutionFailure(
        stage=failure.stage,
        code=failure.code,
        message=failure.message,
        retryable=failure.retryable,
        details=[*failure.details, detail],
    )


def _append_execution_log_detail(failure: ExecutionFailure) -> ExecutionFailure:
    if any(
        detail.field == "execution_log"
        and detail.resource_type == "EXECUTION_RECORD"
        and detail.actual == "finalization_failed"
        for detail in failure.details
    ):
        return failure
    detail = ApplicationErrorDetail(
        field="execution_log",
        resource_type="EXECUTION_RECORD",
        resource_id=None,
        resource_ref=None,
        expected="finalized",
        actual="finalization_failed",
        limit=None,
        observed=None,
    )
    return ExecutionFailure(
        stage=failure.stage,
        code=failure.code,
        message=failure.message,
        retryable=failure.retryable,
        details=[*failure.details, detail],
    )


_WORKSPACE_TOP_LEVEL = frozenset({"inputs", "runtime", "output"})


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _is_link_like(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(reparse_point and attributes & reparse_point)


def _has_unsafe_file_link_count(metadata: os.stat_result) -> bool:
    """Reject hard links while tolerating Windows' transient zero count."""

    return metadata.st_nlink != 1 and not (
        os.name == "nt" and metadata.st_nlink == 0
    )


def _supports_anchored_workspace_scan() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )


def _capture_workspace_identity(root: Path) -> _WorkspaceIdentity | None:
    """Capture immutable directory identities used by every later scan."""

    try:
        if _supports_anchored_workspace_scan():
            root_fd = os.open(root, _directory_open_flags())
            try:
                root_metadata = os.fstat(root_fd)
                top_level = _top_level_from_scandir(os.scandir(root_fd))
            finally:
                os.close(root_fd)
        else:
            root_metadata = os.stat(root, follow_symlinks=False)
            top_level = _top_level_from_scandir(os.scandir(root))
        if _is_link_like(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
            return None
        if top_level is None:
            return None
        identity = _WorkspaceIdentity(
            root=_metadata_identity(root_metadata),
            top_level=tuple(
                (name, *node_identity)
                for name, node_identity in sorted(top_level.items())
            ),
        )
        if not _workspace_path_matches_identity(root, identity):
            return None
        return identity
    except (OSError, TypeError, ValueError):
        return None


def _top_level_from_scandir(
    iterator: Iterator[os.DirEntry[str]],
) -> dict[str, tuple[int, int]] | None:
    top_level: dict[str, tuple[int, int]] = {}
    try:
        for entry in iterator:
            if entry.name not in _WORKSPACE_TOP_LEVEL:
                return None
            # Some Windows providers return zeroed device/inode values from
            # DirEntry.stat even though a direct stat has a stable identity.
            metadata = (
                os.stat(entry.path, follow_symlinks=False)
                if os.name == "nt"
                else entry.stat(follow_symlinks=False)
            )
            if _is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                return None
            top_level[entry.name] = _metadata_identity(metadata)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    if set(top_level) != _WORKSPACE_TOP_LEVEL:
        return None
    return top_level


def _protect_workspace_root(
    root: Path,
    identity: _WorkspaceIdentity,
) -> _WorkspaceRootWriteGuard | None:
    """Prevent ordinary Agent writes beside the three fixed workspace roots."""

    if os.name == "nt" or not hasattr(os, "fchmod"):
        return None
    descriptor = -1
    original_mode: int | None = None
    try:
        descriptor = os.open(root, _directory_open_flags())
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _metadata_identity(metadata) != identity.root
        ):
            raise OSError("workspace root identity changed")
        original_mode = stat.S_IMODE(metadata.st_mode)
        protected_mode = original_mode & ~0o222
        os.fchmod(descriptor, protected_mode)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o222:
            raise OSError("workspace root remained writable")
        return _WorkspaceRootWriteGuard(
            descriptor=descriptor,
            original_mode=original_mode,
        )
    except (OSError, TypeError, ValueError) as exc:
        if descriptor >= 0:
            if original_mode is not None:
                try:
                    os.fchmod(descriptor, original_mode)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise runtime_failure(
            stage=ExecutionStage.BACKEND_START,
            code=ErrorCode.BACKEND_START_FAILED,
            message="Agent Workspace protections could not be established.",
            retryable=True,
        ) from exc


def _restore_workspace_root(
    guard: _WorkspaceRootWriteGuard | None,
) -> None:
    if guard is None:
        return
    failure: OSError | None = None
    try:
        os.fchmod(guard.descriptor, guard.original_mode)
    except OSError as exc:
        failure = exc
    finally:
        try:
            os.close(guard.descriptor)
        except OSError as exc:
            failure = failure or exc
    if failure is not None:
        raise runtime_failure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.WORKSPACE_LIMIT,
            message="Agent Workspace protections could not be released safely.",
        ) from failure


def _workspace_path_matches_identity(
    root: Path,
    identity: _WorkspaceIdentity,
) -> bool:
    try:
        root_metadata = os.stat(root, follow_symlinks=False)
        if (
            _is_link_like(root_metadata)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or _metadata_identity(root_metadata) != identity.root
        ):
            return False
        top_level = _top_level_from_scandir(os.scandir(root))
        if top_level is None:
            return False
        return all(
            top_level.get(name) == (device, inode)
            for name, device, inode in identity.top_level
        )
    except (OSError, TypeError, ValueError):
        return False


def _workspace_measurement_failure(message: str) -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.WORKSPACE_LIMIT,
        message=message,
    )


def _validate_anchored_top_level(
    root_fd: int,
    identity: _WorkspaceIdentity,
    check_abort: Callable[[], None],
) -> None:
    check_abort()
    root_metadata = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or _metadata_identity(root_metadata) != identity.root
    ):
        raise OSError("workspace root identity changed")
    observed = _top_level_from_scandir(os.scandir(root_fd))
    if observed is None:
        raise OSError("workspace root shape changed")
    for name, device, inode in identity.top_level:
        if observed.get(name) != (device, inode):
            raise OSError("workspace top-level identity changed")


def _measure_anchored_directory(
    directory_fd: int,
    *,
    total: int,
    limit: int,
    check_abort: Callable[[], None],
    allow_transient_changes: bool,
) -> int:
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            check_abort()
            entry.name.encode("utf-8", errors="strict")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                if allow_transient_changes:
                    continue
                raise
            if _is_link_like(metadata):
                raise _workspace_measurement_failure(
                    "Workspace contains an invalid output node."
                )
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(
                        entry.name,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    if allow_transient_changes:
                        continue
                    raise
                try:
                    current = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or _metadata_identity(current) != _metadata_identity(metadata)
                    ):
                        if allow_transient_changes:
                            continue
                        raise OSError("workspace directory identity changed")
                    total = _measure_anchored_directory(
                        child_fd,
                        total=total,
                        limit=limit,
                        check_abort=check_abort,
                        allow_transient_changes=allow_transient_changes,
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if _has_unsafe_file_link_count(metadata):
                    raise _workspace_measurement_failure(
                        "Workspace contains a linked output file."
                    )
                file_flags = (
                    os.O_RDONLY
                    | int(getattr(os, "O_CLOEXEC", 0))
                    | int(getattr(os, "O_NOFOLLOW", 0))
                    | int(getattr(os, "O_NONBLOCK", 0))
                )
                try:
                    file_fd = os.open(entry.name, file_flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    if allow_transient_changes:
                        continue
                    raise
                try:
                    current = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or _metadata_identity(current) != _metadata_identity(metadata)
                    ):
                        if allow_transient_changes:
                            continue
                        raise OSError("workspace file identity changed")
                    if _has_unsafe_file_link_count(current):
                        raise _workspace_measurement_failure(
                            "Workspace contains a linked output file."
                        )
                    total += current.st_size
                finally:
                    os.close(file_fd)
            else:
                raise _workspace_measurement_failure(
                    "Workspace contains an invalid output node."
                )
            if total > limit:
                return total
    return total


def _measure_anchored_workspace(
    root_fd: int,
    *,
    identity: _WorkspaceIdentity,
    limit: int,
    check_abort: Callable[[], None],
    allow_transient_changes: bool,
) -> int:
    _validate_anchored_top_level(root_fd, identity, check_abort)
    total = 0
    for name in ("runtime", "output"):
        expected = identity.top_level_identity(name)
        if expected is None:
            raise OSError("workspace top-level identity is incomplete")
        child_fd = os.open(name, _directory_open_flags(), dir_fd=root_fd)
        try:
            metadata = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _metadata_identity(metadata) != expected
            ):
                raise OSError("workspace top-level identity changed")
            total = _measure_anchored_directory(
                child_fd,
                total=total,
                limit=limit,
                check_abort=check_abort,
                allow_transient_changes=allow_transient_changes,
            )
        finally:
            os.close(child_fd)
        if total > limit:
            break
    _validate_anchored_top_level(root_fd, identity, check_abort)
    return total


def _measure_portable_directory(
    directory: Path,
    *,
    root: Path,
    identity: _WorkspaceIdentity,
    total: int,
    limit: int,
    check_abort: Callable[[], None],
    allow_transient_changes: bool,
) -> int:
    if not _workspace_path_matches_identity(root, identity):
        raise OSError("workspace root identity changed")
    with os.scandir(directory) as iterator:
        for entry in iterator:
            check_abort()
            if not _workspace_path_matches_identity(root, identity):
                raise OSError("workspace root identity changed")
            entry.name.encode("utf-8", errors="strict")
            try:
                entry_metadata = entry.stat(follow_symlinks=False)
                metadata = (
                    os.stat(entry.path, follow_symlinks=False)
                    if os.name == "nt"
                    else entry_metadata
                )
            except FileNotFoundError:
                if allow_transient_changes:
                    continue
                raise
            entry_identity = _metadata_identity(entry_metadata)
            if (
                os.name != "nt" or entry_identity != (0, 0)
            ) and entry_identity != _metadata_identity(metadata):
                if allow_transient_changes:
                    continue
                raise OSError("workspace node identity changed")
            if _is_link_like(metadata):
                raise _workspace_measurement_failure(
                    "Workspace contains an invalid output node."
                )
            if stat.S_ISDIR(metadata.st_mode):
                total = _measure_portable_directory(
                    Path(entry.path),
                    root=root,
                    identity=identity,
                    total=total,
                    limit=limit,
                    check_abort=check_abort,
                    allow_transient_changes=allow_transient_changes,
                )
            elif stat.S_ISREG(metadata.st_mode):
                if _has_unsafe_file_link_count(metadata):
                    raise _workspace_measurement_failure(
                        "Workspace contains a linked output file."
                    )
                total += metadata.st_size
            else:
                raise _workspace_measurement_failure(
                    "Workspace contains an invalid output node."
                )
            if total > limit:
                return total
    return total


def _measure_portable_workspace(
    root: Path,
    *,
    identity: _WorkspaceIdentity,
    limit: int,
    check_abort: Callable[[], None],
    allow_transient_changes: bool,
) -> int:
    total = 0
    for name in ("runtime", "output"):
        total = _measure_portable_directory(
            root / name,
            root=root,
            identity=identity,
            total=total,
            limit=limit,
            check_abort=check_abort,
            allow_transient_changes=allow_transient_changes,
        )
        if total > limit:
            break
    return total


def _temporary_workspace_bytes(
    root: Path,
    *,
    limit: int,
    deadline: float | None = None,
    cancellation: CancellationSignal | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    identity: _WorkspaceIdentity | None = None,
    failure_probe: Callable[[], ExecutionFailure | None] | None = None,
    allow_transient_changes: bool = False,
) -> int:
    def check_abort() -> None:
        if failure_probe is not None:
            failure = failure_probe()
            if failure is not None:
                raise RuntimeExecutionError(failure)
        if cancellation is not None and cancellation.is_cancelled():
            raise RuntimeExecutionError(_cancelled_failure(cancellation.reason))
        if deadline is not None and monotonic() >= deadline:
            raise RuntimeExecutionError(
                ExecutionFailure(
                    stage=ExecutionStage.BACKEND_EXECUTE,
                    code=ErrorCode.BACKEND_TIMEOUT,
                    message="Agent execution exceeded the fixed wall time.",
                    retryable=True,
                    details=[],
                )
            )

    check_abort()
    expected_identity = identity or _capture_workspace_identity(root)
    if expected_identity is None or not _workspace_path_matches_identity(
        root, expected_identity
    ):
        raise _workspace_measurement_failure(
            "Workspace output roots could not be measured safely."
        )
    try:
        if _supports_anchored_workspace_scan():
            root_fd = os.open(root, _directory_open_flags())
            try:
                total = _measure_anchored_workspace(
                    root_fd,
                    identity=expected_identity,
                    limit=limit,
                    check_abort=check_abort,
                    allow_transient_changes=allow_transient_changes,
                )
            finally:
                os.close(root_fd)
        else:
            total = _measure_portable_workspace(
                root,
                identity=expected_identity,
                limit=limit,
                check_abort=check_abort,
                allow_transient_changes=allow_transient_changes,
            )
    except RuntimeExecutionError:
        raise
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as exc:
        raise _workspace_measurement_failure(
            "Workspace output could not be measured safely."
        ) from exc
    if not _workspace_path_matches_identity(root, expected_identity):
        raise _workspace_measurement_failure(
            "Workspace output roots could not be measured safely."
        )
    return total


__all__ = [
    "AgentBackend",
    "BackendExecution",
    "BackendExecutionLimits",
]
