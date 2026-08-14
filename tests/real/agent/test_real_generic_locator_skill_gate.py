from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from problem_locator.contracts import (
    DiagnosisMode,
    GenericDiagnosisOutcome,
    GenericResultStatus,
    Job,
    JobStatus,
    OutcomeResultType,
    StateFile,
)
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
    InMemoryStateRepository,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CATALOG_FIXTURES = ROOT / "tests/fixtures/components/runtime-catalog"
CASE_ID = "00000000-0000-0000-0000-000000000001"
RAW_PROBLEM_TEXT = (
    "订单支付成功后页面仍显示“处理中”。\n"
    "request-id: 订单-α-42\n"
    "已确认：刷新三次仍复现"
)


def _load(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))


def _generic_state(tmp_path: Path) -> tuple[StateFile, Job, VersionedAssetCatalog]:
    skill_dir = tmp_path / "specialized-skills"
    shutil.copytree(
        CATALOG_FIXTURES / "skill-dir/manual-triage",
        skill_dir / "manual-triage",
    )
    catalog = VersionedAssetCatalog(
        skill_dir=skill_dir,
        generic_skill_name="generic-problem-locator-smoke",
    )
    payload = _load("job-route.json")
    payload.update(catalog.generic_diagnose_bindings().model_dump(mode="json"))
    payload.update(
        job_type="DIAGNOSE",
        diagnosis_mode=DiagnosisMode.GENERIC,
        generic_skill_name="generic-problem-locator-smoke",
        generic_problem_text=RAW_PROBLEM_TEXT,
        status=JobStatus.RUNNING,
        goal="Run the configured generic problem locator smoke Skill.",
        base_state_revision=1,
        context_snapshot=None,
        evidence_refs=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_refs=[],
        available_skill_refs=[],
        skill_ref=None,
        logparse_tool_ref=None,
        logparse_product=None,
        review_target=None,
        started_at="2026-08-11T00:00:01.000Z",
        finished_at=None,
        runtime_epoch="00000000-0000-4000-8000-000000000499",
    )
    job = Job.model_validate(payload)

    state_payload = _load("state.json")
    aggregate = state_payload["cases"][CASE_ID]
    aggregate["case"].update(
        status="RUNNING",
        raw_problem_text=RAW_PROBLEM_TEXT,
        active_job_id=job.job_id,
        selected_skill_ref=None,
        final_result=None,
        unresolved_result=None,
        generic_result=None,
        failure=None,
    )
    aggregate["jobs"] = {job.job_id: job.model_dump(mode="json")}
    aggregate["outcomes"] = {}
    aggregate["outcome_processing_records"] = {}
    aggregate["execution_failure_records"] = {}
    aggregate["evidence"] = {}
    aggregate["artifacts"] = {}
    return StateFile.model_validate(state_payload), job, catalog


def test_real_preinstalled_generic_skill_receives_exact_input_and_writes_result(
    tmp_path: Path,
) -> None:
    if os.environ.get("S08_REAL_GENERIC_LOCATOR_GATE") != "1":
        pytest.skip("requires the explicitly selected real generic-locator gate")
    command = os.environ.get("S08_REAL_GENERIC_LOCATOR_AGENT_COMMAND")
    assert command, "S08_REAL_GENERIC_LOCATOR_AGENT_COMMAND is required"

    state, job, catalog = _generic_state(tmp_path)
    records = InMemoryExecutionRecordStore()
    runtime = DiagnosisRuntime(
        state_repository=InMemoryStateRepository(state),
        resource_store=InMemoryResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=None,
        execution_records=records,
        clock=FakeClock("2026-08-11T00:00:05.000Z"),
        id_generator=DeterministicIdGenerator(),
        workspace_manager=WorkspaceManager(tmp_path / "data"),
        backend=AgentBackend(command),
        backend_test_limits=BackendExecutionLimits(
            wall_time_seconds=float(
                os.environ["TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS"]
            ),
            stdout_stderr_bytes=4 * 1024 * 1024,
            workspace_bytes=8 * 1024 * 1024,
            poll_interval_seconds=0.02,
            termination_grace_seconds=5.0,
        ),
    )

    receipt = runtime.execute(job, InMemoryCancellationSignal())

    if receipt.job_outcome.result_type is OutcomeResultType.FAILED:
        sinks = records.log_sinks.get(job.job_id)
        stdout = b"" if sinks is None else sinks.stdout.data
        stderr = b"" if sinks is None else sinks.stderr.data
        pytest.fail(
            f"generic locator failed: {receipt.job_outcome.error}; "
            f"stdout={stdout.decode('utf-8', 'replace')!r}; "
            f"stderr={stderr.decode('utf-8', 'replace')!r}"
        )
    assert receipt.job_outcome.result_type is OutcomeResultType.COMPLETED
    assert isinstance(receipt.job_outcome.payload, GenericDiagnosisOutcome)
    assert receipt.job_outcome.payload.status is GenericResultStatus.RESOLVED
    assert receipt.job_outcome.payload.conclusion == "generic-skill-input-contract-ok"
    assert (
        receipt.job_outcome.payload.root_cause_analysis
        == "已逐字确认通用定位输入与预期的多行 Unicode 文本一致。"
    )
    assert receipt.job_outcome.payload.skill_name == "generic-problem-locator-smoke"
    assert job.context_snapshot is None
    assert job.evidence_refs == job.attachment_refs == job.previous_outcome_refs == []
    assert job.artifact_refs == []
