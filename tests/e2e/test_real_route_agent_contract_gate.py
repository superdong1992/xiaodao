from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CancellationReason,
    ExecutionLogSinks,
    JOB_STDOUT_STDERR_BYTES,
    Job,
    JobType,
    OutcomeResultType,
    RouteDecision,
    RouteKind,
    WorkspaceInputManifest,
    canonical_json_bytes,
    default_resource_limits,
)
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.context_builder import ContextBuilder, ContextMaterials
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import read_agent_output


ROOT = Path(__file__).resolve().parents[2]
ROUTE_JOB = ROOT / "tests/fixtures/contracts/positive/job-route.json"
ASSET_ROOT = ROOT / "src/problem_locator/runtime/assets"


class _Signal:
    reason: CancellationReason | None = None

    def __init__(self) -> None:
        self._event = threading.Event()

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


def test_real_route_agent_synthesizes_valid_outcome_from_production_contract(
    tmp_path: Path,
) -> None:
    if os.environ.get("S08_REAL_ROUTE_AGENT_GATE") != "1":
        pytest.skip("requires the explicitly configured real ROUTE Agent gate")
    command = os.environ.get("S08_REAL_ROUTE_AGENT_COMMAND")
    assert command, "S08_REAL_ROUTE_AGENT_COMMAND is required for the real ROUTE gate"

    job = Job.model_validate_json(ROUTE_JOB.read_bytes())
    assert job.job_type is JobType.ROUTE
    assert len(job.available_skill_refs) == 1
    skill_ref = job.available_skill_refs[0]
    manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
    )
    skill_index = canonical_json_bytes(
        {
            "schema_version": 1,
            "skills": [
                {
                    "capability": "rpc-timeout",
                    "logparse_product": None,
                    "ref": skill_ref.model_dump(mode="json"),
                    "requires_logparse": False,
                    "summary": "Diagnose a payment-to-inventory RPC timeout.",
                }
            ],
        }
    ).decode("utf-8")
    materials = ContextMaterials(
        profile=(ASSET_ROOT / "profiles/router/profile.md").read_text(encoding="utf-8"),
        tool_bundle=(
            ASSET_ROOT / "tool-bundles/router/tool-bundle.json"
        ).read_text(encoding="utf-8"),
        output_contract=(
            ASSET_ROOT / "output-contracts/route/output-contract.md"
        ).read_text(encoding="utf-8"),
        manifest=manifest,
        skill_index=skill_index,
    )
    context = ContextBuilder().build(job, materials)

    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    runtime = workspace / "runtime"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    (runtime / "tool-state").mkdir(parents=True)
    (output / "proposals").mkdir(parents=True)
    manifest_bytes = canonical_json_bytes(manifest)
    (inputs / "manifest.json").write_bytes(manifest_bytes)
    (runtime / "context.txt").write_text(context.body, encoding="utf-8", newline="\n")

    stdout = _Sink()
    stderr = _Sink()
    try:
        execution = AgentBackend(command).execute(
            prompt=context.body,
            workspace_root=workspace,
            cancellation=_Signal(),
            log_sinks=ExecutionLogSinks(
                stdout=stdout,
                stderr=stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=default_resource_limits(JobType.ROUTE),
            test_limits=BackendExecutionLimits(
                wall_time_seconds=240.0,
                stdout_stderr_bytes=4 * 1024 * 1024,
                workspace_bytes=8 * 1024 * 1024,
                poll_interval_seconds=0.02,
                termination_grace_seconds=5.0,
            ),
        )
    except RuntimeExecutionError as exc:
        pytest.fail(
            "real ROUTE Agent Backend failed with "
            f"{exc.failure.code.value}; stdout_bytes={len(stdout.data)}; "
            f"stderr_bytes={len(stderr.data)}"
        )

    assert execution.returncode == 0
    validated = read_agent_output(workspace, job, manifest)
    assert validated.canonical_bytes == (output / "job_outcome.json").read_bytes()
    assert validated.outcome.result_type is OutcomeResultType.COMPLETED
    assert isinstance(validated.outcome.payload, RouteDecision)
    assert validated.outcome.payload.kind is RouteKind.MATCHED
    assert validated.outcome.payload.skill_ref == skill_ref
    assert validated.outcome.consumed_evidence_refs == []
    assert validated.outcome.proposed_evidence_drafts == []
    assert validated.outcome.proposed_artifact_drafts == []
    assert (inputs / "manifest.json").read_bytes() == manifest_bytes
    assert (runtime / "context.txt").read_text(encoding="utf-8") == context.body
    assert sorted(path.name for path in workspace.iterdir()) == [
        "inputs",
        "output",
        "runtime",
    ]
    assert json.loads(validated.canonical_bytes)["payload"]["skill_ref"] == json.loads(
        canonical_json_bytes(skill_ref)
    )
    assert stdout.closed is True and stderr.closed is True
