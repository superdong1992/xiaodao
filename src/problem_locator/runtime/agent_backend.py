"""Subprocess Agent Backend with bounded logs, time, and workspace output."""

from __future__ import annotations

import os
import stat
import threading
import time
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

    def write(self, chunk: bytes) -> None:
        self._sink.write(chunk)

    def flush(self) -> None:
        try:
            self._sink.flush()
        except BaseException:
            self.finalization_failed = True
            raise

    def close(self) -> None:
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
        if not root.is_dir():
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
        try:
            managed = self._process_factory(
                invocation.argv,
                cwd=root,
                environment=invocation.environment,
            )
        except ProcessTreeError as exc:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_START,
                code=ErrorCode.BACKEND_START_FAILED,
                message="Agent process execution group could not be created.",
                retryable=True,
            ) from exc

        primary_failure: ExecutionFailure | None = None
        started = 0.0
        workspace_bytes = 0
        tree_released = False
        readers: tuple[threading.Thread, ...] = ()
        lifecycle_threads: tuple[threading.Thread, ...] = ()
        try:
            readers = (
                threading.Thread(
                    target=_drain_pipe,
                    args=(managed.stdout, stdout_sink, output_state),
                    name="problem-locator-agent-stdout",
                    daemon=True,
                ),
                threading.Thread(
                    target=_drain_pipe,
                    args=(managed.stderr, stderr_sink, output_state),
                    name="problem-locator-agent-stderr",
                    daemon=True,
                ),
            )
            input_writer = threading.Thread(
                target=_write_prompt,
                args=(managed.stdin, prompt, input_state),
                name="problem-locator-agent-stdin",
                daemon=True,
            )
            lifecycle_threads = (input_writer, *readers)
            started = self._monotonic()
            for thread in lifecycle_threads:
                thread.start()

            while managed.process.poll() is None and primary_failure is None:
                if cancellation.is_cancelled():
                    primary_failure = _cancelled_failure(cancellation.reason)
                    break
                if input_state.failure is not None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent process closed stdin before receiving the prompt.",
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
                _join_threads(lifecycle_threads, limits.termination_grace_seconds)
                if output_state.failed.is_set():
                    primary_failure = _output_failure(output_state)
                elif input_state.failure is not None:
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent process did not consume the submitted prompt.",
                        retryable=False,
                        details=[],
                    )
                elif any(thread.is_alive() for thread in lifecycle_threads):
                    primary_failure = ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent execution pipes remained open after exit.",
                        retryable=False,
                        details=[],
                    )
                elif managed.process.returncode != 0:
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

            _join_threads(lifecycle_threads, limits.termination_grace_seconds)
            if any(thread.is_alive() for thread in lifecycle_threads):
                primary_failure = _append_cleanup_detail(
                    primary_failure
                    or ExecutionFailure(
                        stage=ExecutionStage.BACKEND_EXECUTE,
                        code=ErrorCode.BACKEND_EXIT_FAILED,
                        message="Agent execution pipes could not be reclaimed.",
                        retryable=False,
                        details=[],
                    )
                )

            sink_failure: BaseException | None = None
            for sink in (stdout_sink, stderr_sink):
                try:
                    sink.close()
                except BaseException as exc:  # sink failures must be normalized
                    sink_failure = sink_failure or exc
            if sink_failure is not None and primary_failure is None:
                primary_failure = ExecutionFailure(
                    stage=ExecutionStage.EXECUTION_RECORD,
                    code=ErrorCode.EXECUTION_RECORD_FAILED,
                    message="Execution log could not be finalized.",
                    retryable=True,
                    details=[],
                )

        if primary_failure is not None:
            raise RuntimeExecutionError(primary_failure)
        workspace_bytes = _temporary_workspace_bytes(
            root,
            limit=limits.workspace_bytes,
        )
        if workspace_bytes > limits.workspace_bytes:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.WORKSPACE_LIMIT,
                message="Agent Workspace exceeded the fixed byte limit.",
            )
        return BackendExecution(
            returncode=managed.process.returncode or 0,
            stdout_stderr_bytes=output_state.total,
            workspace_bytes=workspace_bytes,
            elapsed_seconds=max(0.0, self._monotonic() - started),
        )


def _join_threads(threads: tuple[threading.Thread, ...], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        if thread.ident is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _write_prompt(pipe: object, prompt: str, state: _InputState) -> None:
    try:
        pipe.write(prompt.encode("utf-8"))  # type: ignore[attr-defined]
        pipe.flush()  # type: ignore[attr-defined]
    except BaseException as exc:
        state.failure = exc
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        except BaseException as exc:
            if state.failure is None:
                state.failure = exc
        state.finished.set()


def _drain_pipe(
    pipe: object,
    sink: StreamingSecretRedactor,
    state: _OutputState,
) -> None:
    try:
        while True:
            chunk = pipe.read(_PIPE_CHUNK_BYTES)  # type: ignore[attr-defined]
            if not chunk:
                break
            accepted = state.reserve(len(chunk))
            if accepted:
                sink.write(chunk[:accepted])
            if accepted != len(chunk):
                break
    except BaseException as exc:
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


def _temporary_workspace_bytes(
    root: Path,
    *,
    limit: int,
    deadline: float | None = None,
    cancellation: CancellationSignal | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    try:
        root_metadata = root.stat(follow_symlinks=False)
        if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
            raise OSError("workspace root is not a directory")
        top_level = {candidate.name: candidate for candidate in root.iterdir()}
        if set(top_level) != {"inputs", "runtime", "output"}:
            raise OSError("workspace root shape changed")
        for name, candidate in top_level.items():
            metadata = candidate.stat(follow_symlinks=False)
            if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise OSError(f"workspace {name} root changed")
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.BACKEND_EXECUTE,
            code=ErrorCode.WORKSPACE_LIMIT,
            message="Workspace output roots could not be measured safely.",
        ) from exc

    total = 0
    for relative in ("runtime", "output"):
        subtree = root / relative
        try:
            for candidate in subtree.rglob("*"):
                if cancellation is not None and cancellation.is_cancelled():
                    raise RuntimeExecutionError(
                        _cancelled_failure(cancellation.reason)
                    )
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
                candidate.relative_to(subtree).as_posix().encode(
                    "utf-8", errors="strict"
                )
                metadata = candidate.stat(follow_symlinks=False)
                if candidate.is_symlink() or (
                    not stat.S_ISDIR(metadata.st_mode)
                    and not stat.S_ISREG(metadata.st_mode)
                ):
                    raise RuntimeExecutionError(
                        ExecutionFailure(
                            stage=ExecutionStage.BACKEND_EXECUTE,
                            code=ErrorCode.WORKSPACE_LIMIT,
                            message="Workspace contains an invalid output node.",
                            retryable=False,
                            details=[],
                        )
                    )
                if stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        raise RuntimeExecutionError(
                            ExecutionFailure(
                                stage=ExecutionStage.BACKEND_EXECUTE,
                                code=ErrorCode.WORKSPACE_LIMIT,
                                message="Workspace contains a linked output file.",
                                retryable=False,
                                details=[],
                            )
                        )
                    total += metadata.st_size
                    if total > limit:
                        return total
        except RuntimeExecutionError:
            raise
        except (OSError, UnicodeEncodeError, ValueError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.WORKSPACE_LIMIT,
                message="Workspace output could not be measured safely.",
            ) from exc
    return total


__all__ = [
    "AgentBackend",
    "BackendExecution",
    "BackendExecutionLimits",
]
