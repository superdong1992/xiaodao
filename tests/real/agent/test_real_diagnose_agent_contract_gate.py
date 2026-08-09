from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    AttachmentRequirementConstraints,
    AttachmentFilenameSuffix,
    ArtifactKind,
    CancellationReason,
    ContextSectionKind,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisOutcome,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    ExecutionLogSinks,
    EvidenceSourceType,
    JOB_STDOUT_STDERR_BYTES,
    Job,
    JobOutcome,
    JobStatus,
    JobType,
    InputRequirementConstraints,
    LogparseParseClaim,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    VersionedRef,
    WorkspaceInputManifest,
    WorkspaceAttachmentInput,
    WorkspacePreviousOutcomeInput,
    canonical_json_bytes,
    default_resource_limits,
    parse_canonical_json_bytes,
    validate_logparse_claim_for_job,
    workspace_attachment_relative_path,
)
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecutionLimits
from problem_locator.runtime.catalog import hash_product_directory
from problem_locator.runtime.context_builder import ContextBuilder, ContextMaterials
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import read_agent_output


ROOT = Path(__file__).resolve().parents[3]
DIAGNOSE_JOB = ROOT / "tests/fixtures/contracts/positive/job-diagnose.json"
ASSET_ROOT = ROOT / "src/problem_locator/runtime/assets"
GENERATOR_PATH = (
    ROOT
    / ".claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py"
)
GENERATION_SPEC_ROOT = (
    ROOT / "tests/fixtures/components/diagnosis-generator/specs"
)
SKILL_PRODUCT_SHA256 = (
    "eaa059e98e2fde9b923e0bce3e860422b2944aeabe939b57920793f70337b618"
)
PARAMETER_GROUP_A = {
    "caller_service",
    "problem_time",
    "rpc_method",
    "server_service",
}
REAL_ARCHIVE = (
    ROOT
    / "tests/fixtures/components/logparse/real/synthetic-rpc-service-takeover.zip.b64"
)
REAL_ARCHIVE_SHA256 = (
    "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064"
)
FIRST_LOG_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000173"
FIRST_LOG_INPUT_SOURCE_ID = "00000000-0000-0000-0000-000000000175"


@pytest.fixture(scope="module")
def diagnosis_generator() -> Any:
    module_spec = importlib.util.spec_from_file_location(
        "_problem_locator_real_agent_generate_v3",
        GENERATOR_PATH,
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


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


def _initial_diagnose_job(skill_ref: VersionedRef) -> Job:
    payload = json.loads(DIAGNOSE_JOB.read_bytes())
    payload.update(
        {
            "artifact_refs": [],
            "attachment_refs": [],
            "evidence_refs": [],
            "goal": "Collect the fixed parameter group A for the routed RPC timeout.",
            "logparse_product": "compact",
            "previous_outcome_refs": [],
            "skill_ref": skill_ref.model_dump(mode="json"),
        }
    )
    payload["context_snapshot"].update(
        {
            "active_hypotheses": [],
            "candidate_conclusion": None,
            "confirmed_facts": [],
            "evidence_refs": [],
            "open_questions": [],
            "pending_requirements": [],
            "rejected_hypotheses": [],
            "user_facts": [],
        }
    )
    return Job.model_validate(payload)


def _first_log_diagnose_job(
    skill_ref: VersionedRef,
    logparse_tool_ref: VersionedRef,
) -> Job:
    payload = json.loads(DIAGNOSE_JOB.read_bytes())
    payload.update(
        {
            "artifact_refs": [],
            "attachment_refs": [FIRST_LOG_ATTACHMENT_ID],
            "available_skill_refs": [],
            "base_state_revision": 5,
            "evidence_refs": [],
            "goal": "Parse the fixed archive, preserve client evidence, and request order_id.",
            "job_id": "00000000-0000-0000-0000-000000000171",
            "logparse_product": "compact",
            "logparse_tool_ref": logparse_tool_ref.model_dump(mode="json"),
            "previous_outcome_refs": [],
            "skill_ref": skill_ref.model_dump(mode="json"),
            "started_at": "2026-07-31T00:01:01.000Z",
            "status": JobStatus.RUNNING.value,
            "runtime_epoch": "00000000-0000-0000-0000-000000000170",
        }
    )
    values = {
        "caller_service": "checkout-synthetic",
        "server_service": "inventory-synthetic",
        "rpc_method": "ReserveStock",
        "problem_time": "2026-07-31T00:00:03.000Z",
    }
    snapshot = payload["context_snapshot"]
    snapshot.update(
        {
            "active_hypotheses": [],
            "candidate_conclusion": None,
            "confirmed_facts": [],
            "diagnosis_state_revision": 5,
            "evidence_refs": [],
            "open_questions": [],
            "pending_requirements": [],
            "rejected_hypotheses": [],
            "user_facts": [
                DiagnosisItem(
                    item_id=f"00000000-0000-0000-0000-{180 + index:012d}",
                    statement=value,
                    status=DiagnosisItemStatus.ACTIVE,
                    provenance=DiagnosisProvenance(
                        source_type=DiagnosisProvenanceType.USER_INPUT,
                        source_ref=FIRST_LOG_INPUT_SOURCE_ID,
                        input_name=name,
                    ),
                    evidence_refs=[],
                    created_revision=5,
                    supersedes=[],
                ).model_dump(mode="json")
                for index, (name, value) in enumerate(values.items())
            ],
        }
    )
    return Job.model_validate(payload)


@pytest.mark.parametrize(
    "spec_name",
    [
        pytest.param("rpc-service-takeover.json", id="rpc-service-takeover"),
        pytest.param("database-deadlock.json", id="database-deadlock"),
        pytest.param("manual-triage.json", id="manual-triage"),
    ],
)
def test_real_agent_v3_requirement_isolation_gate(
    tmp_path: Path,
    diagnosis_generator: Any,
    spec_name: str,
) -> None:
    """Run one real Agent against each generated v3 requirement contract."""

    if os.environ.get("S08_REAL_DIAGNOSE_AGENT_V3_MATRIX_GATE") != "1":
        pytest.skip("requires the explicitly configured v3 Agent matrix gate")
    command = os.environ.get("S08_REAL_DIAGNOSE_AGENT_COMMAND")
    assert command, "S08_REAL_DIAGNOSE_AGENT_COMMAND is required"

    generation_spec = diagnosis_generator.load_generation_spec(
        GENERATION_SPEC_ROOT / spec_name
    )
    generated = diagnosis_generator.generate_diagnosis_skill(
        generation_spec,
        tmp_path / "generated-skills",
    )
    skill_path = generated.skill_dir
    manifest_value = json.loads(
        (skill_path / "diagnosis-skill.json").read_text(encoding="utf-8")
    )
    product_hash = hash_product_directory(skill_path)
    assert product_hash == generated.product_sha256
    skill_ref = VersionedRef(
        id=manifest_value["id"],
        version=manifest_value["version"],
        content_hash=product_hash,
    )

    requires_logparse = manifest_value["requires_logparse"]
    job_payload = json.loads(DIAGNOSE_JOB.read_bytes())
    job_payload.update(
        {
            "artifact_refs": [],
            "attachment_refs": [],
            "evidence_refs": [],
            "goal": "Request only the generated Skill's missing INITIAL inputs.",
            "logparse_product": (
                manifest_value.get("logparse_product", "default")
                if requires_logparse
                else None
            ),
            "logparse_tool_ref": (
                job_payload["logparse_tool_ref"] if requires_logparse else None
            ),
            "previous_outcome_refs": [],
            "skill_ref": skill_ref.model_dump(mode="json"),
        }
    )
    job_payload["context_snapshot"].update(
        {
            "active_hypotheses": [],
            "candidate_conclusion": None,
            "confirmed_facts": [],
            "evidence_refs": [],
            "open_questions": [],
            "pending_requirements": [],
            "rejected_hypotheses": [],
            "user_facts": [],
        }
    )
    job = Job.model_validate(job_payload)
    workspace_manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[],
    )
    materials = ContextMaterials(
        profile=(ASSET_ROOT / "profiles/specialist/profile.md").read_text(
            encoding="utf-8"
        ),
        tool_bundle=(
            ASSET_ROOT / "tool-bundles/diagnose/tool-bundle.json"
        ).read_text(encoding="utf-8"),
        output_contract=(
            ASSET_ROOT / "output-contracts/diagnose/output-contract.md"
        ).read_text(encoding="utf-8"),
        manifest=workspace_manifest,
        skill=(skill_path / "SKILL.md").read_text(encoding="utf-8"),
    )
    context = ContextBuilder().build(job, materials)
    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "runtime/tool-state").mkdir(parents=True)
    (workspace / "output/proposals").mkdir(parents=True)
    (workspace / "inputs/manifest.json").write_bytes(
        canonical_json_bytes(workspace_manifest)
    )
    (workspace / "runtime/context.txt").write_text(
        context.body,
        encoding="utf-8",
        newline="\n",
    )

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
            resource_limits=job.resource_limits,
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
            f"real v3 matrix Agent failed with {exc.failure.code.value}; "
            f"stdout_bytes={len(stdout.data)}; stderr_bytes={len(stderr.data)}"
        )

    assert execution.returncode == 0
    validated = read_agent_output(workspace, job, workspace_manifest)
    assert validated.outcome.result_type is OutcomeResultType.NEED_INPUT
    assert isinstance(validated.outcome.payload, DiagnosisOutcome)
    diagnosis = validated.outcome.payload
    expected = [
        item
        for item in manifest_value["requirements"]
        if item["stage"] == "INITIAL" and item["kind"] == "INPUT"
    ]
    actual = diagnosis.state_delta.add_pending_requirements
    assert [item.name for item in actual] == [item["name"] for item in expected]
    actual_by_name = {item.name: item for item in actual}
    for declared in expected:
        requirement = actual_by_name[declared["name"]]
        assert requirement.kind is RequirementKind.INPUT
        assert requirement.status is RequirementStatus.OPEN
        assert requirement.prompt == declared["prompt"]
        assert requirement.requested_by_job_id == job.job_id
        assert requirement.fulfilled_by_refs == []
        assert requirement.supplement_policy.value == declared["supplement_policy"]
        assert isinstance(requirement.constraints, InputRequirementConstraints)
        assert requirement.constraints.model_dump(mode="json") == declared["constraints"]
    assert set(diagnosis.requested_input) == {
        requirement.requirement_id for requirement in actual
    }
    assert diagnosis.requested_attachments == []
    assert diagnosis.candidate_conclusion_draft is None
    assert validated.outcome.proposed_evidence_drafts == []
    assert validated.outcome.proposed_artifact_drafts == []
    if manifest_value["capability"] != "service-takeover":
        rpc_names = PARAMETER_GROUP_A | {"order_id"}
        assert rpc_names.isdisjoint(actual_by_name)
        assert not any(
            name.encode("utf-8") in validated.canonical_bytes for name in rpc_names
        )
    assert stdout.closed is True and stderr.closed is True


def test_real_first_log_diagnose_agent_produces_valid_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast real seam: DeepSeek + generated Skill + broker + pinned Logparse."""

    if os.environ.get("S08_REAL_FIRST_LOG_AGENT_GATE") != "1":
        pytest.skip("requires the explicitly configured first-log Agent gate")
    command = os.environ.get("S08_REAL_DIAGNOSE_AGENT_COMMAND")
    assert command, "S08_REAL_DIAGNOSE_AGENT_COMMAND is required"
    skill_path_value = os.environ.get("S08_REAL_DIAGNOSE_SKILL_PATH")
    assert skill_path_value, "S08_REAL_DIAGNOSE_SKILL_PATH is required"
    raw_configuration_names = (
        "LOGPARSE_REPO",
        "LOGPARSE_CONFIG_PATH",
        "LOGPARSE_PYTHON",
    )
    assert all(os.environ.get(name) for name in raw_configuration_names)
    logparse_repo, logparse_config, logparse_python = (
        Path(os.environ[name]) for name in raw_configuration_names
    )
    for name in raw_configuration_names:
        monkeypatch.delenv(name)

    skill_path = Path(skill_path_value)
    assert hash_product_directory(skill_path) == SKILL_PRODUCT_SHA256
    skill_ref = VersionedRef(
        id="diagnose-service-takeover",
        version="4.0.0",
        content_hash=SKILL_PRODUCT_SHA256,
    )
    logparse_asset, broker_factory = build_logparse_runtime(
        logparse_repo,
        logparse_config,
        logparse_python,
    )
    job = _first_log_diagnose_job(skill_ref, logparse_asset.ref)
    archive_bytes = base64.b64decode(
        b"".join(REAL_ARCHIVE.read_bytes().split()),
        validate=True,
    )
    assert hashlib.sha256(archive_bytes).hexdigest() == REAL_ARCHIVE_SHA256
    attachment = WorkspaceAttachmentInput(
        input_kind="ATTACHMENT",
        resource_id=FIRST_LOG_ATTACHMENT_ID,
        relative_path=workspace_attachment_relative_path(
            FIRST_LOG_ATTACHMENT_ID,
            AttachmentFilenameSuffix.ZIP,
        ),
        resource_kind=ResourceKind.FILE,
        size=len(archive_bytes),
        sha256=REAL_ARCHIVE_SHA256,
        content_type="application/zip",
        filename_suffix=AttachmentFilenameSuffix.ZIP,
    )
    manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[attachment],
    )
    materials = ContextMaterials(
        profile=(ASSET_ROOT / "profiles/specialist/profile.md").read_text(
            encoding="utf-8"
        ),
        tool_bundle=(
            ASSET_ROOT / "tool-bundles/diagnose/tool-bundle.json"
        ).read_text(encoding="utf-8"),
        output_contract=(
            ASSET_ROOT / "output-contracts/diagnose/output-contract.md"
        ).read_text(encoding="utf-8"),
        manifest=manifest,
        skill=(skill_path / "SKILL.md").read_text(encoding="utf-8"),
    )
    context = ContextBuilder().build(job, materials)

    workspace = tmp_path / "first-log-workspace"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "runtime/tool-state").mkdir(parents=True)
    (workspace / "output/proposals").mkdir(parents=True)
    manifest_path = workspace / "inputs/manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    manifest_path.chmod(0o444)
    attachment_path = workspace / attachment.relative_path
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(archive_bytes)
    attachment_path.chmod(0o444)
    (workspace / "runtime/context.txt").write_text(
        context.body,
        encoding="utf-8",
        newline="\n",
    )

    stdout = _Sink()
    stderr = _Sink()
    signal = _Signal()
    session = broker_factory.open(job, workspace, manifest, signal)
    broker_environment = session.agent_environment()
    try:
        execution = AgentBackend(command).execute(
            prompt=context.body,
            workspace_root=workspace,
            cancellation=signal,
            log_sinks=ExecutionLogSinks(
                stdout=stdout,
                stderr=stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=job.resource_limits,
            broker_environment=broker_environment,
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
            "real first-log DIAGNOSE Agent failed with "
            f"{exc.failure.code.value}; stdout_bytes={len(stdout.data)}; "
            f"stderr_bytes={len(stderr.data)}"
        )
    finally:
        session.close()

    assert execution.returncode == 0
    request_bytes = session.parse_request_bytes()
    assert request_bytes is not None
    claim = parse_canonical_json_bytes(
        (workspace / "runtime/tool-state/logparse-parse.claim").read_bytes(),
        model_type=LogparseParseClaim,
    )
    validated = read_agent_output(
        workspace,
        job,
        manifest,
        secrets=tuple(broker_environment.values()),
    )
    assert validate_logparse_claim_for_job(
        claim,
        job,
        manifest,
        request_bytes,
        validated.outcome,
    ) == claim
    assert validated.outcome.result_type is OutcomeResultType.NEED_INPUT
    assert isinstance(validated.outcome.payload, DiagnosisOutcome)
    diagnosis = validated.outcome.payload
    assert diagnosis.candidate_conclusion_draft is None
    assert diagnosis.requested_attachments == []
    accepted_evidence = diagnosis.state_delta.add_evidence_bindings
    assert len(accepted_evidence) == 1
    assert accepted_evidence[0].existing_evidence_id is None
    evidence_proposal_key = accepted_evidence[0].evidence_proposal_key
    assert evidence_proposal_key is not None
    added = diagnosis.state_delta.add_pending_requirements
    assert len(added) == 1
    assert added[0].name == "order_id"
    assert added[0].kind is RequirementKind.INPUT
    assert diagnosis.requested_input == [added[0].requirement_id]

    assert len(validated.outcome.proposed_evidence_drafts) == 1
    evidence = validated.outcome.proposed_evidence_drafts[0]
    assert evidence.proposal_key == evidence_proposal_key
    assert evidence.source_type is EvidenceSourceType.LOGPARSE
    assert evidence.workspace_relative_path is None
    assert evidence.declared_size is None
    assert evidence.declared_sha256 is None
    assert len(validated.outcome.proposed_artifact_drafts) == 1
    artifact = validated.outcome.proposed_artifact_drafts[0]
    artifact_proposal_key = artifact.proposal_key
    assert evidence.source_binding.artifact_proposal_key == artifact_proposal_key
    assert artifact.artifact_kind is ArtifactKind.LOGPARSE_RUN
    assert artifact.content_type == (
        "application/vnd.problem-locator.logparse-run+directory"
    )
    assert artifact.resource_kind is ResourceKind.DIRECTORY
    assert artifact.workspace_relative_path == (
        f"output/proposals/{artifact_proposal_key}/tree"
    )
    assert artifact.declared_size is None
    assert artifact.declared_sha256 is None
    broker_result = parse_canonical_json_bytes(
        (
            workspace
            / f"output/proposals/{artifact_proposal_key}/target_logs.json"
        ).read_bytes()
    )
    assert isinstance(broker_result, dict)
    assert artifact.model_dump(mode="json") == broker_result[
        "logparse_run_artifact_draft"
    ]
    assert len(validated.proposal_resources) == 1
    resource = validated.proposal_resources[0]
    assert resource.proposal_key == artifact_proposal_key
    assert resource.sha256 == artifact.metadata.tree_manifest_sha256
    assert resource.size > 0
    assert stdout.closed is True and stderr.closed is True


def test_real_diagnose_agent_requests_parameter_group_a_from_generated_skill(
    tmp_path: Path,
) -> None:
    if os.environ.get("S08_REAL_DIAGNOSE_AGENT_GATE") != "1":
        pytest.skip("requires the explicitly configured real DIAGNOSE Agent gate")
    command = os.environ.get("S08_REAL_DIAGNOSE_AGENT_COMMAND")
    assert command, "S08_REAL_DIAGNOSE_AGENT_COMMAND is required"
    skill_path_value = os.environ.get("S08_REAL_DIAGNOSE_SKILL_PATH")
    assert skill_path_value, "S08_REAL_DIAGNOSE_SKILL_PATH is required"

    skill_path = Path(skill_path_value)
    assert hash_product_directory(skill_path) == SKILL_PRODUCT_SHA256
    skill_ref = VersionedRef(
        id="diagnose-service-takeover",
        version="4.0.0",
        content_hash=SKILL_PRODUCT_SHA256,
    )
    job = _initial_diagnose_job(skill_ref)
    assert job.job_type is JobType.DIAGNOSE
    manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        entries=[],
    )
    materials = ContextMaterials(
        profile=(ASSET_ROOT / "profiles/specialist/profile.md").read_text(
            encoding="utf-8"
        ),
        tool_bundle=(
            ASSET_ROOT / "tool-bundles/diagnose/tool-bundle.json"
        ).read_text(encoding="utf-8"),
        output_contract=(
            ASSET_ROOT / "output-contracts/diagnose/output-contract.md"
        ).read_text(encoding="utf-8"),
        manifest=manifest,
        skill=(skill_path / "SKILL.md").read_text(encoding="utf-8"),
    )
    context = ContextBuilder().build(job, materials)
    assert "{{S00_" not in context.body
    assert context.body.count("<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>") == 1
    assert context.body.count("<<<BEGIN S00 USER RESULT SCHEMA>>>") == 1

    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    runtime = workspace / "runtime"
    output = workspace / "output"
    inputs.mkdir(parents=True)
    (runtime / "tool-state").mkdir(parents=True)
    (output / "proposals").mkdir(parents=True)
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path = inputs / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o444)
    (runtime / "context.txt").write_text(
        context.body, encoding="utf-8", newline="\n"
    )

    stdout = _Sink()
    stderr = _Sink()
    started_at = datetime.now(UTC)
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
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
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
            "real DIAGNOSE Agent Backend failed with "
            f"{exc.failure.code.value}; stdout_bytes={len(stdout.data)}; "
            f"stderr_bytes={len(stderr.data)}"
        )

    assert execution.returncode == 0
    finished_at = datetime.now(UTC)
    validated = read_agent_output(workspace, job, manifest)
    assert validated.canonical_bytes == (output / "job_outcome.json").read_bytes()
    assert validated.outcome.result_type is OutcomeResultType.NEED_INPUT
    assert isinstance(validated.outcome.payload, DiagnosisOutcome)
    assert validated.outcome.error is None
    assert validated.outcome.outcome_id not in {job.job_id, job.case_id}
    produced_at = datetime.strptime(
        validated.outcome.produced_at,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ).replace(tzinfo=UTC)
    assert started_at - timedelta(seconds=1) <= produced_at
    assert produced_at <= finished_at + timedelta(seconds=1)
    assert validated.outcome.payload.findings == []
    assert validated.outcome.payload.candidate_conclusion_draft is None
    requirements = validated.outcome.payload.state_delta.add_pending_requirements
    assert len(requirements) == len(PARAMETER_GROUP_A)
    assert {item.name for item in requirements} == PARAMETER_GROUP_A
    assert all(item.kind is RequirementKind.INPUT for item in requirements)
    assert all(item.status is RequirementStatus.OPEN for item in requirements)
    assert all(item.requested_by_job_id == job.job_id for item in requirements)
    assert all(item.fulfilled_by_refs == [] for item in requirements)
    assert all(item.supplement_policy.value == "MISSING_ONLY" for item in requirements)
    assert all(
        isinstance(item.constraints, InputRequirementConstraints)
        and item.constraints.value_type == "STRING"
        and item.constraints.allowed_values == []
        for item in requirements
    )
    requirements_by_name = {item.name: item for item in requirements}
    assert requirements_by_name["problem_time"].constraints.pattern == (
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
    )
    assert all(
        item.constraints.pattern is None
        for name, item in requirements_by_name.items()
        if name != "problem_time"
    )
    assert set(validated.outcome.payload.requested_input) == {
        item.requirement_id for item in requirements
    }
    assert validated.outcome.payload.requested_attachments == []
    assert validated.outcome.consumed_evidence_refs == []
    assert validated.outcome.proposed_evidence_drafts == []
    assert validated.outcome.proposed_artifact_drafts == []
    delta = validated.outcome.payload.state_delta.model_dump(mode="json")
    for field_name, value in delta.items():
        if field_name != "add_pending_requirements":
            assert value is None or value == []
    assert list((runtime / "tool-state").iterdir()) == []
    assert list((output / "proposals").iterdir()) == []
    assert (inputs / "manifest.json").read_bytes() == manifest_bytes
    assert (runtime / "context.txt").read_text(encoding="utf-8") == context.body
    assert sorted(path.name for path in workspace.iterdir()) == [
        "inputs",
        "output",
        "runtime",
    ]
    assert stdout.closed is True and stderr.closed is True

    # Persist the first *real* Agent result across the same contract seam used
    # by the service.  This is intentionally derived from ``validated`` rather
    # than assembling a target Outcome for the continuation.
    previous_payload = validated.outcome.model_dump(mode="json")
    assert previous_payload.pop("proposed_evidence_drafts") == []
    assert previous_payload.pop("proposed_artifact_drafts") == []
    previous_payload["proposed_evidence"] = []
    previous_payload["proposed_artifacts"] = []
    previous_outcome = JobOutcome.model_validate(previous_payload)
    previous_outcome_bytes = canonical_json_bytes(previous_outcome)

    supplement_trigger_id = "00000000-0000-0000-0000-000000000090"
    parameter_values = {
        "caller_service": "checkout-synthetic",
        "problem_time": "2026-07-31T00:00:03.000Z",
        "rpc_method": "ReserveStock",
        "server_service": "inventory-synthetic",
    }
    continuation_revision = job.base_state_revision + 2
    user_facts: list[DiagnosisItem] = []
    fact_ids_by_name: dict[str, str] = {}
    for ordinal, name in enumerate(sorted(PARAMETER_GROUP_A), start=91):
        fact_id = f"00000000-0000-0000-0000-{ordinal:012d}"
        fact_ids_by_name[name] = fact_id
        user_facts.append(
            DiagnosisItem(
                item_id=fact_id,
                statement=parameter_values[name],
                status=DiagnosisItemStatus.ACTIVE,
                provenance=DiagnosisProvenance(
                    source_type=DiagnosisProvenanceType.USER_INPUT,
                    source_ref=supplement_trigger_id,
                    input_name=name,
                ),
                evidence_refs=[],
                created_revision=continuation_revision,
                supersedes=[],
            )
        )

    fulfilled_requirements = []
    for requirement in requirements:
        fulfilled_requirements.append(
            type(requirement).model_validate(
                {
                    **requirement.model_dump(mode="json"),
                    "status": RequirementStatus.FULFILLED.value,
                    "fulfilled_by_refs": [fact_ids_by_name[requirement.name]],
                }
            )
        )

    continuation_payload = job.model_dump(mode="json")
    continuation_job_id = "00000000-0000-0000-0000-000000000012"
    continuation_payload.update(
        {
            "base_state_revision": continuation_revision,
            "created_at": "2026-07-31T00:02:00.000Z",
            "goal": "Request the required log archive before parsing.",
            "job_id": continuation_job_id,
            "previous_outcome_refs": [previous_outcome.outcome_id],
        }
    )
    continuation_payload["context_snapshot"].update(
        {
            "diagnosis_state_revision": continuation_revision,
            "pending_requirements": [
                item.model_dump(mode="json") for item in fulfilled_requirements
            ],
            "user_facts": [item.model_dump(mode="json") for item in user_facts],
        }
    )
    continuation_job = Job.model_validate(continuation_payload)
    assert continuation_job.previous_outcome_refs == [
        previous_outcome.outcome_id
    ]
    assert all(
        item.status is RequirementStatus.FULFILLED
        for item in continuation_job.context_snapshot.pending_requirements
    )
    assert {
        item.provenance.input_name
        for item in continuation_job.context_snapshot.user_facts
    } == PARAMETER_GROUP_A

    previous_relative_path = (
        f"inputs/outcomes/{previous_outcome.outcome_id}/job_outcome.json"
    )
    continuation_manifest = WorkspaceInputManifest(
        schema_version=1,
        job_id=continuation_job.job_id,
        case_id=continuation_job.case_id,
        job_type=continuation_job.job_type,
        logparse_tool_ref=continuation_job.logparse_tool_ref,
        logparse_product=continuation_job.logparse_product,
        entries=[
            WorkspacePreviousOutcomeInput(
                input_kind="PREVIOUS_OUTCOME",
                resource_id=previous_outcome.outcome_id,
                relative_path=previous_relative_path,
                resource_kind=ResourceKind.FILE,
                size=len(previous_outcome_bytes),
                sha256=hashlib.sha256(previous_outcome_bytes).hexdigest(),
                source_job_id=previous_outcome.job_id,
                result_type=previous_outcome.result_type,
            )
        ],
    )
    continuation_materials = ContextMaterials(
        profile=materials.profile,
        tool_bundle=materials.tool_bundle,
        output_contract=materials.output_contract,
        manifest=continuation_manifest,
        previous_outcomes=(previous_outcome,),
        skill=materials.skill,
    )
    continuation_context = ContextBuilder().build(
        continuation_job,
        continuation_materials,
    )
    assert continuation_context.body.count(
        "<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>"
    ) == 1
    continuation_section_kinds = [
        section.kind for section in continuation_context.sections
    ]
    assert continuation_section_kinds.count(ContextSectionKind.PREVIOUS_OUTCOME) == 1
    assert continuation_section_kinds.count(ContextSectionKind.OUTPUT_CONTRACT) == 1
    assert continuation_section_kinds.count(ContextSectionKind.RESOURCE_MANIFEST) == 1
    assert continuation_section_kinds.index(
        ContextSectionKind.PREVIOUS_OUTCOME
    ) < continuation_section_kinds.index(ContextSectionKind.OUTPUT_CONTRACT)
    assert continuation_section_kinds.index(
        ContextSectionKind.OUTPUT_CONTRACT
    ) < continuation_section_kinds.index(ContextSectionKind.RESOURCE_MANIFEST)

    continuation_workspace = tmp_path / "continuation-workspace"
    continuation_inputs = continuation_workspace / "inputs"
    continuation_runtime = continuation_workspace / "runtime"
    continuation_output = continuation_workspace / "output"
    continuation_inputs.mkdir(parents=True)
    (continuation_runtime / "tool-state").mkdir(parents=True)
    (continuation_output / "proposals").mkdir(parents=True)
    continuation_manifest_bytes = canonical_json_bytes(continuation_manifest)
    continuation_manifest_path = continuation_inputs / "manifest.json"
    continuation_manifest_path.write_bytes(continuation_manifest_bytes)
    continuation_manifest_path.chmod(0o444)
    previous_path = continuation_workspace / previous_relative_path
    previous_path.parent.mkdir(parents=True)
    previous_path.write_bytes(previous_outcome_bytes)
    previous_path.chmod(0o444)
    (continuation_runtime / "context.txt").write_text(
        continuation_context.body,
        encoding="utf-8",
        newline="\n",
    )

    continuation_stdout = _Sink()
    continuation_stderr = _Sink()
    continuation_started_at = datetime.now(UTC)
    try:
        continuation_execution = AgentBackend(command).execute(
            prompt=continuation_context.body,
            workspace_root=continuation_workspace,
            cancellation=_Signal(),
            log_sinks=ExecutionLogSinks(
                stdout=continuation_stdout,
                stderr=continuation_stderr,
                combined_limit_bytes=JOB_STDOUT_STDERR_BYTES,
            ),
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
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
            "real continuation DIAGNOSE Agent Backend failed with "
            f"{exc.failure.code.value}; "
            f"stdout_bytes={len(continuation_stdout.data)}; "
            f"stderr_bytes={len(continuation_stderr.data)}"
        )

    assert continuation_execution.returncode == 0
    continuation_finished_at = datetime.now(UTC)
    continuation_validated = read_agent_output(
        continuation_workspace,
        continuation_job,
        continuation_manifest,
    )
    assert continuation_validated.canonical_bytes == (
        continuation_output / "job_outcome.json"
    ).read_bytes()
    assert (
        continuation_validated.outcome.result_type
        is OutcomeResultType.NEED_ATTACHMENT
    )
    assert isinstance(continuation_validated.outcome.payload, DiagnosisOutcome)
    assert continuation_validated.outcome.error is None
    assert continuation_validated.outcome.outcome_id not in {
        continuation_job.job_id,
        continuation_job.case_id,
        previous_outcome.outcome_id,
    }
    continuation_produced_at = datetime.strptime(
        continuation_validated.outcome.produced_at,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ).replace(tzinfo=UTC)
    assert continuation_started_at - timedelta(seconds=1) <= continuation_produced_at
    assert continuation_produced_at <= continuation_finished_at + timedelta(seconds=1)

    continuation_diagnosis = continuation_validated.outcome.payload
    assert continuation_diagnosis.findings == []
    assert continuation_diagnosis.candidate_conclusion_draft is None
    assert continuation_diagnosis.requested_input == []
    attachment_requirements = (
        continuation_diagnosis.state_delta.add_pending_requirements
    )
    assert len(attachment_requirements) == 1
    log_archive = attachment_requirements[0]
    assert log_archive.name == "log_archive"
    assert log_archive.kind is RequirementKind.ATTACHMENT
    assert log_archive.status is RequirementStatus.OPEN
    assert log_archive.requested_by_job_id == continuation_job.job_id
    assert log_archive.fulfilled_by_refs == []
    assert isinstance(
        log_archive.constraints,
        AttachmentRequirementConstraints,
    )
    assert log_archive.constraints.allowed_content_types == [
        "application/gzip",
        "application/zip",
        "application/x-tar",
    ]
    assert log_archive.constraints.min_count == 1
    assert log_archive.constraints.max_count == 1
    assert continuation_diagnosis.requested_attachments == [
        log_archive.requirement_id
    ]
    assert continuation_validated.outcome.consumed_evidence_refs == []
    assert continuation_validated.outcome.proposed_evidence_drafts == []
    assert continuation_validated.outcome.proposed_artifact_drafts == []
    continuation_delta = continuation_diagnosis.state_delta.model_dump(mode="json")
    for field_name, value in continuation_delta.items():
        if field_name != "add_pending_requirements":
            assert value is None or value == []

    assert list((continuation_runtime / "tool-state").iterdir()) == []
    assert list((continuation_output / "proposals").iterdir()) == []
    assert (
        continuation_inputs / "manifest.json"
    ).read_bytes() == continuation_manifest_bytes
    assert previous_path.read_bytes() == previous_outcome_bytes
    assert (continuation_runtime / "context.txt").read_text(
        encoding="utf-8"
    ) == continuation_context.body
    assert sorted(path.name for path in continuation_workspace.iterdir()) == [
        "inputs",
        "output",
        "runtime",
    ]
    assert continuation_stdout.closed is True
    assert continuation_stderr.closed is True
