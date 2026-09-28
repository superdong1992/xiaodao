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
from problem_locator.runtime.final_response import parse_route_response


ROOT = Path(__file__).resolve().parents[3]
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


_SCENARIOS = (
    "explicit-match",
    "generic-symptom",
    "out-of-scope",
    "ambiguous-candidates",
    "missing-diagnostic-materials",
)


def _route_scenario(scenario: str) -> tuple[Job, str, RouteKind, list[str]]:
    value = json.loads(ROUTE_JOB.read_bytes())
    value["goal"] = "审核冻结问题是否明确符合唯一专用 Skill 的适用范围。"
    statement = (
        "支付服务 payment 通过网络向库存服务 inventory 发出的 RPC 请求超时；"
        "问题发生在两个独立进程之间，不涉及进程内调用。"
    )
    scope = "payment 到 inventory 的跨进程网络 RPC"
    expected_kind = RouteKind.MATCHED
    expected_candidates = ["match"]
    if scenario == "generic-symptom":
        statement = "系统很慢，有时卡住，尚不清楚涉及哪个组件或调用方式。"
        scope = "业务系统响应速度"
        expected_kind = RouteKind.NO_CAPABILITY
        expected_candidates = ["uncertain"]
    elif scenario == "out-of-scope":
        statement = "图片编辑器进程内的本地滤镜函数执行很慢，已确认没有 RPC 或网络调用。"
        scope = "图片编辑器的进程内滤镜函数"
        expected_kind = RouteKind.NO_CAPABILITY
        expected_candidates = ["ruled_out"]
    elif scenario == "ambiguous-candidates":
        expected_kind = RouteKind.NO_CAPABILITY
        expected_candidates = ["match", "match"]
    elif scenario == "missing-diagnostic-materials":
        statement += "还没有提供日志、问题时间和进程名。"
    else:
        assert scenario == "explicit-match"
    value["context_snapshot"]["problem_spec"] = {
        "revision": 1,
        "statement": statement,
        "scope": scope,
        "actual_behavior": statement,
        "expected_behavior": "相关操作正常完成。",
        "goals": ["定位当前问题的原因。"],
        "non_goals": [],
        "constraints": [],
        "completion_criteria": ["给出有依据的定位结论。"],
    }
    value["available_skill_refs"][0]["id"] = "diagnosis-skill/payment-rpc-timeout"
    if scenario == "ambiguous-candidates":
        value["available_skill_refs"].append({
            "id": "diagnosis-skill/payment-rpc-timeout-alternative",
            "version": "1.0.0",
            "content_hash": "f" * 64,
        })
    job = Job.model_validate(value)
    skills = []
    for ref in job.available_skill_refs:
        skills.append({
            "capability": "payment 到 inventory 的跨进程 RPC 超时定位",
            "logparse_product": None,
            "ref": ref.model_dump(mode="json"),
            "required_artifacts": ["log_archive"] if scenario == "missing-diagnostic-materials" else [],
            "required_user_inputs": ["problem_time", "client_process", "server_process"]
                if scenario == "missing-diagnostic-materials" else [],
            "requires_logparse": False,
            "summary": "诊断 payment 到 inventory 的网络 RPC 超时。",
            "routing": {
                "applicability": [{
                    "id": "payment-inventory-rpc-timeout",
                    "description": "问题明确涉及 payment 服务向 inventory 服务发出的跨进程网络 RPC 请求超时。",
                }],
                "exclusions": [{
                    "id": "local-only-operation",
                    "description": "问题仅发生在同一进程内的本地操作中，不涉及网络 RPC。",
                }],
            },
        })
    return job, canonical_json_bytes({"schema_version": 3, "skills": skills}).decode("utf-8"), expected_kind, expected_candidates


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_real_route_agent_synthesizes_valid_outcome_from_production_contract(
    tmp_path: Path,
    scenario: str,
) -> None:
    if os.environ.get("S08_REAL_ROUTE_AGENT_GATE") != "1":
        pytest.skip("requires the explicitly configured real ROUTE Agent gate")
    command = os.environ.get("S08_REAL_ROUTE_AGENT_COMMAND")
    assert command, "S08_REAL_ROUTE_AGENT_COMMAND is required for the real ROUTE gate"

    job, skill_index, expected_kind, expected_candidates = _route_scenario(scenario)
    assert job.job_type is JobType.ROUTE
    skill_ref = job.available_skill_refs[0]
    manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
        resolved_logparse_plan=None,
        review_subject=None,
    )
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
            backend_phase="ROUTE",
            file_access="none",
            test_limits=BackendExecutionLimits(
                wall_time_seconds=float(
                    os.environ["TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS"]
                ),
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
    assert list((runtime / "tool-state").iterdir()) == []
    validated = parse_route_response(execution.final_result, job, skill_index=skill_index)
    assert validated.draft.result_type is (
        OutcomeResultType.COMPLETED if expected_kind is RouteKind.MATCHED
        else OutcomeResultType.NO_CAPABILITY
    )
    assert isinstance(validated.draft.payload, RouteDecision)
    assert validated.draft.payload.kind is expected_kind
    assert validated.draft.payload.skill_ref == (skill_ref if expected_kind is RouteKind.MATCHED else None)
    assert validated.route_admission is not None
    assert [item["status"] for item in validated.route_admission["candidate_results"]] == expected_candidates
    assert validated.draft.consumed_evidence_refs == []
    assert validated.draft.proposed_evidence_drafts == []
    assert validated.draft.proposed_artifact_drafts == []
    assert (inputs / "manifest.json").read_bytes() == manifest_bytes
    assert (runtime / "context.txt").read_text(encoding="utf-8") == context.body
    assert sorted(path.name for path in workspace.iterdir()) == [
        "inputs",
        "output",
        "runtime",
    ]
    assert list((runtime / "tool-state").iterdir()) == []
    assert list((output / "proposals").iterdir()) == []
    assert list(output.iterdir()) == [output / "proposals"]
    assert stdout.closed is True and stderr.closed is True
