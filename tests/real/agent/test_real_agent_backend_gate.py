from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from problem_locator.contracts import (
    AgentJobOutcome,
    CancellationReason,
    ExecutionLogSinks,
    JOB_STDOUT_STDERR_BYTES,
    JobType,
    canonical_json_bytes,
    default_resource_limits,
)
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.claude_command import prepare_claude_command
from problem_locator.runtime.failures import RuntimeExecutionError


ROOT = Path(__file__).resolve().parents[3]
AGENT_OUTCOME = (
    ROOT / "tests/fixtures/contracts/positive/agent-job-outcome-diagnosis.json"
)


class _Signal:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: CancellationReason | None = None

    @property
    def reason(self) -> CancellationReason | None:
        return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self._event.wait(timeout_seconds)


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        assert not self.closed
        self.data.extend(chunk)

    def flush(self) -> None:
        assert not self.closed

    def close(self) -> None:
        self.closed = True


def test_real_claude_code_writes_exact_agent_outcome_through_backend(
    tmp_path: Path,
) -> None:
    if os.environ.get("S08_REAL_AGENT_GATE") != "1":
        pytest.skip("requires the explicitly configured real Agent release gate")
    command = os.environ.get("S08_REAL_AGENT_COMMAND")
    assert command, "S08_REAL_AGENT_COMMAND is required for the real Agent gate"

    invocation = prepare_claude_command(command)
    executable = Path(invocation.argv[0])
    assert executable.name in {"claude", "claude.exe"}
    version = subprocess.run(
        [os.fspath(executable), "--version"],
        cwd=tmp_path,
        env=invocation.environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+ \(Claude Code\)", version)

    expected = canonical_json_bytes(
        AgentJobOutcome.model_validate_json(AGENT_OUTCOME.read_bytes())
    )
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    runtime = workspace / "runtime"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    (runtime / "tool-state").mkdir(parents=True)
    (output / "proposals").mkdir(parents=True)
    input_marker = b"immutable real-Agent smoke input\n"
    runtime_marker = b"immutable real-Agent smoke runtime\n"
    (inputs / "marker.txt").write_bytes(input_marker)
    (runtime / "marker.txt").write_bytes(runtime_marker)

    stdout = _Sink()
    stderr = _Sink()
    prompt = (
        "This is a release smoke in an isolated temporary workspace. "
        "Do not read or modify inputs/ or runtime/. Create exactly one file, "
        "output/job_outcome.json, whose UTF-8 bytes are exactly the JSON line "
        "between BEGIN and END, including one final newline. Do not create "
        "any other file or directory. Exit successfully after writing it.\n"
        f"BEGIN\n{expected.decode('utf-8')}END\n"
    )
    try:
        execution = AgentBackend(command).execute(
            prompt=prompt,
            workspace_root=workspace,
            cancellation=_Signal(),
            log_sinks=ExecutionLogSinks(
                stdout=stdout,
                stderr=stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
            test_limits=BackendExecutionLimits(
                wall_time_seconds=180.0,
                stdout_stderr_bytes=4 * 1024 * 1024,
                workspace_bytes=8 * 1024 * 1024,
                poll_interval_seconds=0.02,
                termination_grace_seconds=5.0,
            ),
        )
    except RuntimeExecutionError as exc:
        pytest.fail(
            f"real Agent Backend failed with {exc.failure.code.value}; "
            f"stdout={bytes(stdout.data).decode('utf-8', 'replace')!r}; "
            f"stderr={bytes(stderr.data).decode('utf-8', 'replace')!r}"
        )

    assert execution.returncode == 0
    assert stdout.closed is True and stderr.closed is True
    assert (output / "job_outcome.json").read_bytes() == expected
    assert AgentJobOutcome.model_validate_json(
        (output / "job_outcome.json").read_bytes()
    ) == AgentJobOutcome.model_validate_json(expected)
    assert (inputs / "marker.txt").read_bytes() == input_marker
    assert (runtime / "marker.txt").read_bytes() == runtime_marker
    assert sorted(path.name for path in workspace.iterdir()) == [
        "inputs",
        "output",
        "runtime",
    ]
    assert list((output / "proposals").iterdir()) == []
