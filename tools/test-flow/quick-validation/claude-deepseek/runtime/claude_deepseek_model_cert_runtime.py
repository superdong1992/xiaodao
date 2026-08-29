#!/usr/bin/env python3
"""Run the production Evidence V2 Methods chain for the DeepSeek model cert."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(_REPOSITORY_ROOT))

_SCENARIO_ID = "multiple-rpc-timeouts"
_RELEASE_CASE_RELATIVE = Path("tests/cases/release/rpc-timeout-anonymized")
_CAPTURED_EVIDENCE_FILENAMES = {
    "source_job": "methods-source-job.json",
    "reviewer_job": "methods-reviewer-job.json",
    "evidence_graph": "methods-evidence-graph-v2.json",
    "evaluation_plan": "methods-evaluation-plan-v2.json",
    "limitations": "methods-limitations-v2.json",
    "source_state": "methods-source-state-v2.json",
    "source_outcome": "methods-source-outcome-v2.json",
    "terminal_state": "methods-terminal-state-v2.json",
    "reviewer_outcome": "methods-reviewer-outcome-v2.json",
}
_PUBLIC_METHODS_RESULT_FILENAME = "methods-result-v2.json"
_LOADED_METHODS_FILENAME = "methods.json"

from problem_locator.application.outcome_submission import OutcomeSubmissionService
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import (
    AssetKind,
    Attachment,
    AttachmentStatus,
    CaseAggregate,
    ErrorCode,
    ExecutionStage,
    Job,
    ResourceKind,
    ResourceRef,
    ResolvedAsset,
    StateFile,
    VersionedRef,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.agent_backend import AgentBackend, BackendExecution
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.failures import runtime_failure
from problem_locator.runtime.methods_records_v2 import (
    METHODS_EVALUATION_PLAN_V2_FILENAME,
    METHODS_EVIDENCE_GRAPH_V2_FILENAME,
    METHODS_LIMITATIONS_V2_FILENAME,
    METHODS_STATE_V2_FILENAME,
    method_prompt_filename_v2,
    method_rejected_attempt_filename_v2,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
    read_method_limitations_record_v2,
    read_method_prompt_v2,
    read_method_state_v2,
)
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    FakeLogparseBrokerFactory,
    InMemoryCancellationSignal,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    RecordingDispatcher,
)
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (
    CATALOG_FIXTURES,
    LOGPARSE_ROOT,
    _MethodsTwoPassBackend,
    _json,
    _route_job,
)


class ModelCertRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ModelCertRuntimeError(code, message)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _release_paths(source_root: Path) -> tuple[Path, Path]:
    case_root = source_root / _RELEASE_CASE_RELATIVE
    return case_root / "input/wiki.md", case_root / "scenarios" / _SCENARIO_ID


def _ordinary_empty_directory(root: Path, label: str) -> Path:
    resolved = root.resolve()
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink() or any(resolved.iterdir()):
            _fail("CLAUDE_DEEPSEEK_RUNTIME_ROOT_NOT_EMPTY", f"{label} must be an empty ordinary directory")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    return resolved


def _copy_registration(registration_root: Path, skill_dir: Path) -> str:
    template_path = registration_root / "registration-template.json"
    try:
        registration = json.loads(template_path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ModelCertRuntimeError(
            "CLAUDE_DEEPSEEK_REGISTRATION_INVALID",
            "The generated registration cannot be loaded by the model-cert Runtime",
        ) from exc
    registration_id = registration.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        _fail("CLAUDE_DEEPSEEK_REGISTRATION_INVALID", "The generated registration ID is invalid")
    destination = skill_dir / registration_id
    shutil.copytree(registration_root, destination, symlinks=False)
    return registration_id


def _state_with_aggregate(aggregate: CaseAggregate) -> StateFile:
    value = _json("state.json")
    value["cases"] = {
        aggregate.case.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(value)


def _aligned_aggregate(job: Job, raw: CaseAggregate) -> CaseAggregate:
    snapshot = job.context_snapshot
    if snapshot is None or job.skill_ref is None:
        _fail("CLAUDE_DEEPSEEK_JOB_CONTEXT_INVALID", "The model-cert Job is missing its frozen specialized context")
    value = raw.model_dump(mode="json")
    value["case"].update(
        {
            "active_job_id": job.job_id,
            "status": "RUNNING",
            "selected_skill_ref": job.skill_ref.model_dump(mode="json"),
            "diagnosis_state": {
                "revision": snapshot.diagnosis_state_revision,
                "problem_spec": snapshot.problem_spec.model_dump(mode="json"),
                "user_facts": [item.model_dump(mode="json") for item in snapshot.user_facts],
                "confirmed_facts": [item.model_dump(mode="json") for item in snapshot.confirmed_facts],
                "active_hypotheses": [item.model_dump(mode="json") for item in snapshot.active_hypotheses],
                "rejected_hypotheses": [item.model_dump(mode="json") for item in snapshot.rejected_hypotheses],
                "open_questions": [item.model_dump(mode="json") for item in snapshot.open_questions],
                "pending_requirements": [item.model_dump(mode="json") for item in snapshot.pending_requirements],
                "evidence_refs": list(snapshot.evidence_refs),
                "candidate_conclusion": None,
            },
        }
    )
    value["jobs"] = {job.job_id: job.model_dump(mode="json")}
    return CaseAggregate.model_validate(value)


def _claim_active_review(repository: InMemoryStateRepository) -> Job:
    state = repository.read_snapshot()
    value = state.model_dump(mode="json")
    aggregate = next(iter(value["cases"].values()))
    review_job_id = aggregate["case"]["active_job_id"]
    review = aggregate["jobs"][review_job_id]
    review.update(
        {
            "status": "RUNNING",
            "started_at": "2026-08-29T00:04:30.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-0000-0000-000000000097",
        }
    )
    repository.seed(StateFile.model_validate(value))
    return repository.read_job(review_job_id)


def _catalog(
    work_root: Path,
    registration_root: Path | None,
    broker_factory: FakeLogparseBrokerFactory,
) -> tuple[VersionedAssetCatalog, str]:
    skill_dir = work_root / "skill-dir"
    skill_dir.mkdir(mode=0o700)
    if registration_root is None:
        registration_id = "rpc-log-analysis"
        shutil.copytree(
            CATALOG_FIXTURES / "skill-dir" / registration_id,
            skill_dir / registration_id,
        )
    else:
        registration_id = _copy_registration(registration_root, skill_dir)
    logparse_asset = ResolvedAsset(
        ref=VersionedRef(
            id="logparse-tool/model-cert-fixture",
            version="1.0.0",
            content_hash="f" * 64,
        ),
        asset_kind=AssetKind.LOGPARSE_TOOL,
        root_path=str(LOGPARSE_ROOT),
    )
    return (
        VersionedAssetCatalog(
            skill_dir=skill_dir,
            generic_skill_name="generic-problem-locator-smoke",
            logparse_tool=logparse_asset,
            logparse_broker_factory=broker_factory,
        ),
        registration_id,
    )


def _fact_values(
    scenario_root: Path,
    generated_registration: bool,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if generated_registration:
        driver = json.loads((scenario_root / "driver.json").read_bytes())
        names = driver["initial_user_fact_names"]
        values = driver["initial_user_fact_values"]
        target_contents = {
            "client": (scenario_root / "client.log").read_bytes(),
            "server": (scenario_root / "server.log").read_bytes(),
        }
    else:
        names = [
            "caller_service",
            "problem_time",
            "client_slot",
            "client_process_name",
            "server_slot",
            "server_process_name",
            "server_service",
            "rpc_method",
        ]
        values = [
            "checkout-service",
            "2026-07-31T00:00:00.000Z",
            "client",
            "checkout-service",
            "server",
            "inventory-service",
            "inventory-service",
            "ReserveStock",
        ]
        target_contents = {
            "client": b"RPC DEADLINE EXCEEDED request_id=42\n",
            "server": b"CONNECTION POOL WAIT request_id=42\n",
        }
    facts = []
    for index, (name, value) in enumerate(zip(names, values, strict=True), start=1):
        facts.append(
            {
                "item_id": f"00000000-0000-4000-8000-{index:012d}",
                "statement": value,
                "status": "ACTIVE",
                "provenance": {
                    "source_type": "USER_INPUT",
                    "source_ref": "00000000-0000-4000-8000-000000000001",
                    "input_name": name,
                },
                "evidence_refs": [],
                "created_revision": 1,
                "supersedes": [],
            }
        )
    return facts, target_contents


def _running_job_and_state(
    *,
    scenario_root: Path,
    catalog: VersionedAssetCatalog,
    generated_registration: bool,
    publication_guard: InMemoryPublicationCommitGuard,
) -> tuple[Job, CaseAggregate, InMemoryResourceStore, dict[str, bytes]]:
    skill_refs = [
        ref
        for ref in catalog.route_bindings().available_skill_refs
        if ref.id.startswith("diagnosis-skill/")
    ]
    if len(skill_refs) != 1:
        _fail("CLAUDE_DEEPSEEK_SKILL_CARDINALITY_INVALID", "Model-cert requires exactly one specialized registration")
    facts, target_contents = _fact_values(scenario_root, generated_registration)
    attachment_bytes = b"model-cert deterministic archive descriptor\n"
    attachment_sha256 = _sha256(attachment_bytes)
    attachment_id = "00000000-0000-4000-8000-000000000450"
    payload = _route_job().model_dump(mode="json")
    payload.update(catalog.diagnose_bindings(skill_refs[0]).model_dump(mode="json"))
    payload.update(
        {
            "job_type": "DIAGNOSE",
            "goal": "Run the frozen Evidence V2 model-cert scenario.",
            "status": "RUNNING",
            "started_at": "2026-08-29T00:00:01.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-4000-8000-000000000498",
            "attachment_refs": [attachment_id],
        }
    )
    payload["context_snapshot"]["user_facts"] = facts
    job = Job.model_validate(payload)
    storage_key = f"resources/cases/{job.case_id}/attachments/{attachment_id}/logs.zip"
    attachment = Attachment(
        attachment_id=attachment_id,
        case_id=job.case_id,
        status=AttachmentStatus.READY,
        name="logs.zip",
        content_type="application/zip",
        declared_size=len(attachment_bytes),
        declared_sha256=attachment_sha256,
        size=len(attachment_bytes),
        sha256=attachment_sha256,
        storage_key=storage_key,
        created_at="2026-08-29T00:00:00.000Z",
        updated_at="2026-08-29T00:00:00.000Z",
    )
    state = StateFile.model_validate(_json("state.json"))
    raw = next(iter(state.cases.values())).model_dump(mode="json")
    raw["jobs"] = {job.job_id: job.model_dump(mode="json")}
    raw["attachments"] = {attachment_id: attachment.model_dump(mode="json")}
    aggregate = _aligned_aggregate(job, CaseAggregate.model_validate(raw))
    resources = InMemoryResourceStore(publication_guard=publication_guard)
    resources.seed_formal_resource(
        ResourceRef(
            resource_kind=ResourceKind.FILE,
            storage_key=storage_key,
            size=len(attachment_bytes),
            sha256=attachment_sha256,
        ),
        state_reference_count=1,
        payload=attachment_bytes,
    )
    return job, aggregate, resources, target_contents


def _close_sinks(kwargs: dict[str, Any]) -> None:
    sinks = kwargs["log_sinks"]
    for sink in {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}.values():
        sink.flush()
        sink.close()


def _valid_role_output(
    workspace_root: Path,
    role: str,
    rejected_method_ids: frozenset[str],
) -> list[dict[str, str]]:
    plan = parse_canonical_json_bytes(
        (workspace_root / "inputs/method-evaluation-plan.json").read_bytes()
    )
    evaluations = plan["evaluations"]
    return [
        {
            "evaluation_ref": item["evaluation_ref"],
            "verdict": (
                "REJECTED"
                if item["method_id"] in rejected_method_ids
                else "CONFIRMED"
            ),
            "reason": (
                "冻结 Evidence Graph 满足该方法卡。"
                if role == "SPECIALIST"
                else "盲评确认冻结 Graph 与 Plan 支持该结论。"
            ),
        }
        for item in evaluations
    ]


class _FakeRoleBackend:
    def __init__(
        self,
        role: str,
        *,
        repair: bool,
        rejected_method_ids: frozenset[str],
        protocol_exhausted: bool = False,
        model_failure: bool = False,
        invariant_failure: bool = False,
    ) -> None:
        self.role = role
        self.repair = repair
        self.rejected_method_ids = rejected_method_ids
        self.protocol_exhausted = protocol_exhausted
        self.model_failure = model_failure
        self.invariant_failure = invariant_failure
        self.calls = 0

    def execute(self, **kwargs: Any) -> BackendExecution:
        self.calls += 1
        workspace_root = Path(kwargs["workspace_root"])
        output = (
            workspace_root / "output/method-diagnosis.draft.json"
            if self.role == "SPECIALIST"
            else workspace_root / "output/method-review.draft.json"
        )
        if self.model_failure:
            _close_sinks(kwargs)
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="injected model execution failure",
            )
        if self.invariant_failure:
            _close_sinks(kwargs)
            raise TypeError("injected backend invariant failure")
        value: Any = {"invalid": True} if self.protocol_exhausted or (self.repair and self.calls == 1) else _valid_role_output(
            workspace_root,
            self.role,
            self.rejected_method_ids,
        )
        output.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        _close_sinks(kwargs)
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=0,
            elapsed_seconds=0.01,
        )


class _SpecialistBackend(_MethodsTwoPassBackend):
    def __init__(
        self,
        factory: FakeLogparseBrokerFactory,
        job: Job,
        role_backend: Any,
        target_contents: dict[str, bytes],
    ) -> None:
        super().__init__(factory, job, "completed")
        self.role_backend = role_backend
        self.target_contents = target_contents

    def _run_methods(self, kwargs: dict[str, Any]) -> BackendExecution:
        return self.role_backend.execute(**kwargs)


class _ReviewerBackend:
    def __init__(self, role_backend: Any) -> None:
        self.role_backend = role_backend

    def execute(self, **kwargs: Any) -> BackendExecution:
        return self.role_backend.execute(**kwargs)


def _agent_command(options: argparse.Namespace) -> str:
    wrapper = options.source_root / "tools/test-flow/quick-validation/claude-deepseek/runtime/claude-deepseek-service-wrapper.mjs"
    arguments = [
        str(options.node_entry),
        str(wrapper),
        "--claude-entry",
        str(options.claude_entry),
        "--settings",
        str(options.claude_settings),
        "--config-root",
        str(options.config_root),
        "--private-root",
        str(options.private_root),
        "--evidence-root",
        str(options.evidence_root),
        "--usage-root",
        str(options.usage_root),
        "--run-id",
        options.run_id,
    ]
    return shlex.join(arguments)


def _file_identity(raw: bytes) -> dict[str, Any]:
    return {"size": len(raw), "sha256": _sha256(raw)}


def _write_new_bytes(root: Path, filename: str, raw: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / filename).open("xb") as stream:
        stream.write(raw)


def _required_record_bytes(
    records: InMemoryExecutionRecordStore,
    job_id: str,
    filename: str,
) -> bytes:
    raw = records.read_audit_bytes(job_id, filename)
    if raw is None:
        _fail(
            "CLAUDE_DEEPSEEK_PRODUCTION_RECORD_MISSING",
            f"Production execution record is missing: {job_id}/{filename}",
        )
    return raw


def _scenario_identity(
    *,
    source_wiki: Path,
    scenario_root: Path,
    registration_id: str,
    source_job: Job,
    graph: Any,
    plan: Any,
) -> dict[str, Any]:
    try:
        wiki_bytes = source_wiki.read_bytes()
        driver = json.loads((scenario_root / "driver.json").read_bytes())
    except (OSError, TypeError, ValueError) as exc:
        raise ModelCertRuntimeError(
            "CLAUDE_DEEPSEEK_SCENARIO_INPUT_INVALID",
            "The frozen release Wiki or scenario driver is unavailable",
        ) from exc
    names = driver.get("initial_user_fact_names")
    values = driver.get("initial_user_fact_values")
    if (
        driver.get("scenario_id") != _SCENARIO_ID
        or not isinstance(names, list)
        or not isinstance(values, list)
        or len(names) != len(values)
        or not all(isinstance(item, str) for item in [*names, *values])
    ):
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_DRIVER_INVALID",
            "The release scenario driver does not expose its exact initial user inputs",
        )
    skill_ref = source_job.skill_ref
    if skill_ref is None or skill_ref.id != f"diagnosis-skill/{registration_id}":
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_SKILL_INVALID",
            "The production Job does not bind the validated registration",
        )
    if (
        graph.skill_sha256 != skill_ref.content_hash
        or plan.skill_sha256 != skill_ref.content_hash
        or plan.evidence_graph_ref != graph.graph_ref
    ):
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_GRAPH_PLAN_SKILL_MISMATCH",
            "The production Graph, Plan, and registered Skill identities differ",
        )
    graph_bytes = canonical_json_bytes(graph)
    plan_bytes = canonical_json_bytes(plan)
    user_inputs_bytes = canonical_json_bytes(
        {
            "initial_user_fact_names": names,
            "initial_user_fact_values": values,
        }
    )
    sources = [
        {
            "source_id": item.source_id,
            "content_sha256": item.content_sha256,
        }
        for item in graph.sources
    ]
    if not sources or len({item["source_id"] for item in sources}) != len(sources):
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_SOURCES_INVALID",
            "The production Evidence Graph sources are empty or duplicated",
        )
    return {
        "scenario_id": _SCENARIO_ID,
        "source_wiki_sha256": _sha256(wiki_bytes),
        "registration_id": registration_id,
        "skill_content_sha256": skill_ref.content_hash,
        "user_inputs_sha256": _sha256(user_inputs_bytes),
        "sources": sources,
        "evidence_graph": {
            "ref": graph.graph_ref,
            "canonical_sha256": _sha256(graph_bytes),
            "canonical_size": len(graph_bytes),
        },
        "evaluation_plan": {
            "ref": plan.plan_ref,
            "canonical_sha256": _sha256(plan_bytes),
            "canonical_size": len(plan_bytes),
        },
    }


def _prompt_receipts(
    records: InMemoryExecutionRecordStore,
    *,
    specialist_job_id: str,
    reviewer_job_id: str | None,
) -> list[dict[str, Any]]:
    receipts = []
    jobs = [("SPECIALIST", specialist_job_id)]
    if reviewer_job_id is not None:
        jobs.append(("REVIEWER", reviewer_job_id))
    for role, job_id in jobs:
        for attempt in ("PRIMARY", "REPAIR"):
            raw = read_method_prompt_v2(
                records,
                job_id=job_id,
                role=role,  # type: ignore[arg-type]
                attempt=attempt,  # type: ignore[arg-type]
            )
            if raw is None:
                continue
            rejected = records.read_audit_bytes(
                job_id,
                method_rejected_attempt_filename_v2(
                    role=role,  # type: ignore[arg-type]
                    attempt=attempt,  # type: ignore[arg-type]
                ),
            )
            receipts.append(
                {
                    "role": role,
                    "attempt": attempt,
                    "job_id": job_id,
                    "filename": method_prompt_filename_v2(
                        role=role,  # type: ignore[arg-type]
                        attempt=attempt,  # type: ignore[arg-type]
                    ),
                    "prompt": _file_identity(raw),
                    "rejected_response": None if rejected is None else _file_identity(rejected),
                }
            )
    return receipts


def run(options: argparse.Namespace) -> dict[str, Any]:
    work_root = _ordinary_empty_directory(options.work_root, "model-cert work root")
    runtime_root = work_root / "production-runtime"
    runtime_root.mkdir(mode=0o700)
    broker_factory = FakeLogparseBrokerFactory()
    publication_guard = InMemoryPublicationCommitGuard()
    catalog, registration_id = _catalog(
        work_root,
        options.registration_root,
        broker_factory,
    )
    loaded_registration_root = work_root / "skill-dir" / registration_id
    source_job, aggregate, resources, target_contents = _running_job_and_state(
        scenario_root=options.scenario_root,
        catalog=catalog,
        generated_registration=options.registration_root is not None,
        publication_guard=publication_guard,
    )
    repository = InMemoryStateRepository(_state_with_aggregate(aggregate))
    records = InMemoryExecutionRecordStore()
    pending = source_job.model_dump(mode="json")
    pending.update(
        {
            "status": "PENDING",
            "started_at": None,
            "finished_at": None,
            "runtime_epoch": None,
        }
    )
    records.publish_job(Job.model_validate(pending))
    if options.mode == "fake":
        shared_rejected_method_ids = frozenset(options.fake_rejected_method_id)
        specialist_role = _FakeRoleBackend(
            "SPECIALIST",
            repair=options.fake_repair,
            rejected_method_ids=(
                shared_rejected_method_ids
                | frozenset(options.fake_specialist_rejected_method_id)
            ),
            protocol_exhausted="SPECIALIST" in options.fake_protocol_exhausted_role,
            model_failure="SPECIALIST" in options.fake_model_failure_role,
            invariant_failure="SPECIALIST" in options.fake_server_invariant_role,
        )
        reviewer_role = _FakeRoleBackend(
            "REVIEWER",
            repair=options.fake_repair,
            rejected_method_ids=(
                shared_rejected_method_ids
                | frozenset(options.fake_reviewer_rejected_method_id)
            ),
            protocol_exhausted="REVIEWER" in options.fake_protocol_exhausted_role,
            model_failure="REVIEWER" in options.fake_model_failure_role,
            invariant_failure="REVIEWER" in options.fake_server_invariant_role,
        )
    else:
        command = _agent_command(options)
        specialist_role = AgentBackend(command, parent_environment=dict(os.environ))
        reviewer_role = AgentBackend(command, parent_environment=dict(os.environ))
    specialist_backend = _SpecialistBackend(
        broker_factory,
        source_job,
        specialist_role,
        target_contents,
    )
    if options.fake_no_matching_evidence and options.mode == "fake":
        specialist_backend.target_contents = {
            label: b"no matching method evidence\n" for label in target_contents
        }

    def execute_preprocessing(
        session: object,
        operation: str,
        request_path: str,
        result_path: str,
    ) -> None:
        if (
            operation != "parse-targets"
            or request_path
            != "output/proposals/methods-preprocess/request.json"
            or result_path
            != "output/proposals/methods-preprocess/target_logs.json"
        ):
            _fail(
                "CLAUDE_DEEPSEEK_PREPROCESSING_PORT_INVALID",
                "The deterministic Logparse port received an unexpected request",
            )
        specialist_backend._run_preprocessing(  # noqa: SLF001 - deterministic fixture
            {"workspace_root": getattr(session, "workspace_root")}
        )
        return None

    broker_factory.preprocessing_executor = execute_preprocessing
    specialist_runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=broker_factory,
        execution_records=records,
        clock=FakeClock("2026-08-29T00:03:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="claude-deepseek-cert-specialist"),
        workspace_manager=WorkspaceManager(runtime_root / "specialist"),
        backend=specialist_backend,
    )
    specialist_receipt = specialist_runtime.execute(
        source_job,
        InMemoryCancellationSignal(),
    )
    dispatcher = RecordingDispatcher()
    notifier = InMemoryStateChangeNotifier()
    submission = OutcomeSubmissionService(
        repository,
        resources,
        publication_guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        catalog,
        dispatcher,
        notifier,
        FakeClock("2026-08-29T00:04:00.000Z"),
        DeterministicIdGenerator(seed="claude-deepseek-cert-submission"),
    )
    handoff = submission.submit_outcome(
        specialist_receipt.job_outcome,
        specialist_receipt.outcome_file_ref,
    )
    if handoff.disposition.value != "APPLIED":
        _fail("CLAUDE_DEEPSEEK_SPECIALIST_SUBMISSION_FAILED", "Specialist Evidence V2 Outcome was not applied")
    review_job = None
    reviewer_receipt = None
    if specialist_receipt.job_outcome.methods_review_target is not None:
        review_job = _claim_active_review(repository)
        reviewer_runtime = DiagnosisRuntime(
            state_repository=repository,
            resource_store=resources,
            asset_catalog=catalog,
            logparse_broker_factory=None,
            execution_records=records,
            clock=FakeClock("2026-08-29T00:05:00.000Z"),
            id_generator=DeterministicIdGenerator(seed="claude-deepseek-cert-reviewer"),
            workspace_manager=WorkspaceManager(runtime_root / "reviewer"),
            backend=_ReviewerBackend(reviewer_role),
        )
        reviewer_receipt = reviewer_runtime.execute(
            review_job,
            InMemoryCancellationSignal(),
        )
        reviewer_terminal = submission.submit_outcome(
            reviewer_receipt.job_outcome,
            reviewer_receipt.outcome_file_ref,
        )
        if reviewer_terminal.disposition.value != "APPLIED":
            _fail(
                "CLAUDE_DEEPSEEK_REVIEWER_SUBMISSION_FAILED",
                "Reviewer Evidence V2 Outcome was not applied",
            )
    query = ApplicationQueryService(repository, resources, notifier)
    public_view = query.get_case(source_job.case_id).case_view
    methods_projection = public_view.methods_result
    if methods_projection is None:
        _fail("CLAUDE_DEEPSEEK_METHODS_RESULT_MISSING", "The public Case has no Evidence V2 methods_result")
    methods_result = methods_projection.model_dump(mode="json")
    encoded_public = canonical_json_bytes(public_view)
    if any(term in encoded_public for term in (b"specialist_evaluation", b"reviewer_evaluation", b"candidate_conclusion")):
        _fail("CLAUDE_DEEPSEEK_PRIVATE_RESULT_LEAK", "Public Methods result leaked private role or Candidate state")
    graph = read_method_evidence_graph_v2(records, job_id=source_job.job_id)
    plan = read_method_evaluation_plan_v2(records, job_id=source_job.job_id)
    limitations = read_method_limitations_record_v2(records, job_id=source_job.job_id)
    specialist_state = read_method_state_v2(records, job_id=source_job.job_id)
    terminal_job_id = source_job.job_id if review_job is None else review_job.job_id
    terminal_state = read_method_state_v2(records, job_id=terminal_job_id)
    if graph is None or plan is None or limitations is None or specialist_state is None or terminal_state is None:
        _fail("CLAUDE_DEEPSEEK_PRODUCTION_RECORD_MISSING", "Production Runtime did not persist Graph, Plan, limitations, or Methods state")
    scenario = _scenario_identity(
        source_wiki=options.source_wiki,
        scenario_root=options.scenario_root,
        registration_id=registration_id,
        source_job=source_job,
        graph=graph,
        plan=plan,
    )
    if (
        methods_result["evidence_graph_ref"] != scenario["evidence_graph"]["ref"]
        or methods_result["plan_ref"] != scenario["evaluation_plan"]["ref"]
    ):
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_RESULT_MISMATCH",
            "The final methods_result binds another production Graph or Plan",
        )
    prompts = _prompt_receipts(
        records,
        specialist_job_id=source_job.job_id,
        reviewer_job_id=None if review_job is None else review_job.job_id,
    )
    actual_attempts = [f"{item['role']}:{item['attempt']}" for item in prompts]
    legal_attempts = {
        (),
        ("SPECIALIST:PRIMARY",),
        ("SPECIALIST:PRIMARY", "SPECIALIST:REPAIR"),
        ("SPECIALIST:PRIMARY", "REVIEWER:PRIMARY"),
        ("SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY"),
        ("SPECIALIST:PRIMARY", "REVIEWER:PRIMARY", "REVIEWER:REPAIR"),
        ("SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY", "REVIEWER:REPAIR"),
    }
    if options.mode == "fake" and tuple(actual_attempts) not in legal_attempts:
        _fail("CLAUDE_DEEPSEEK_FAKE_ATTEMPT_MISMATCH", "Deterministic role attempts did not follow the production repair state machine")
    methods_bytes = canonical_json_bytes(methods_result)
    captured_files = {
        "source_job": _required_record_bytes(records, source_job.job_id, "job.json"),
        "evidence_graph": _required_record_bytes(
            records, source_job.job_id, METHODS_EVIDENCE_GRAPH_V2_FILENAME
        ),
        "evaluation_plan": _required_record_bytes(
            records, source_job.job_id, METHODS_EVALUATION_PLAN_V2_FILENAME
        ),
        "limitations": _required_record_bytes(
            records, source_job.job_id, METHODS_LIMITATIONS_V2_FILENAME
        ),
        "source_state": _required_record_bytes(
            records, source_job.job_id, METHODS_STATE_V2_FILENAME
        ),
        "source_outcome": _required_record_bytes(
            records, source_job.job_id, "job_outcome.json"
        ),
    }
    if review_job is not None:
        captured_files.update(
            {
                "reviewer_job": _required_record_bytes(
                    records, review_job.job_id, "job.json"
                ),
                "terminal_state": _required_record_bytes(
                    records, review_job.job_id, METHODS_STATE_V2_FILENAME
                ),
                "reviewer_outcome": _required_record_bytes(
                    records, review_job.job_id, "job_outcome.json"
                ),
            }
        )
    graph_bytes = captured_files["evidence_graph"]
    plan_bytes = captured_files["evaluation_plan"]
    limitations_bytes = captured_files["limitations"]
    specialist_state_bytes = captured_files["source_state"]
    specialist_outcome_bytes = captured_files["source_outcome"]
    loaded_registration = json.loads(
        (loaded_registration_root / "registration-template.json").read_bytes()
    )
    loaded_methods_path = (
        loaded_registration_root
        / Path(loaded_registration["package"]["relative_path"])
        / _LOADED_METHODS_FILENAME
    )
    loaded_methods_bytes = loaded_methods_path.read_bytes()
    capture_root = options.evidence_root or (work_root / "model-cert-evidence")
    for key, raw in captured_files.items():
        _write_new_bytes(capture_root, _CAPTURED_EVIDENCE_FILENAMES[key], raw)
    _write_new_bytes(capture_root, _PUBLIC_METHODS_RESULT_FILENAME, methods_bytes)
    _write_new_bytes(capture_root, _LOADED_METHODS_FILENAME, loaded_methods_bytes)
    record_receipts = {
        "source_job": {"filename": _CAPTURED_EVIDENCE_FILENAMES["source_job"], **_file_identity(captured_files["source_job"])},
        "graph": {"filename": METHODS_EVIDENCE_GRAPH_V2_FILENAME, **_file_identity(graph_bytes)},
        "plan": {"filename": METHODS_EVALUATION_PLAN_V2_FILENAME, **_file_identity(plan_bytes)},
        "limitations": {"filename": METHODS_LIMITATIONS_V2_FILENAME, **_file_identity(limitations_bytes)},
        "specialist_state": {"filename": METHODS_STATE_V2_FILENAME, **_file_identity(specialist_state_bytes)},
        "specialist_outcome": {"filename": "job_outcome.json", **_file_identity(specialist_outcome_bytes)},
        "source_job_id": source_job.job_id,
        "terminal_job_id": terminal_job_id,
    }
    if review_job is not None and reviewer_receipt is not None:
        record_receipts.update(
            {
                "reviewer_job": {"filename": _CAPTURED_EVIDENCE_FILENAMES["reviewer_job"], **_file_identity(captured_files["reviewer_job"])},
                "reviewer_state": {"filename": METHODS_STATE_V2_FILENAME, **_file_identity(captured_files["terminal_state"])},
                "reviewer_outcome": {"filename": "job_outcome.json", **_file_identity(captured_files["reviewer_outcome"])},
            }
        )
    return {
        "schema_version": 1,
        "status": "PASS",
        "execution_mode": "deterministic-zero-model" if options.mode == "fake" else "real-model",
        "production_runtime": "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
        "runtime_driver": "claude-deepseek-model-cert-v1",
        "scenario_id": _SCENARIO_ID,
        "registration_id": registration_id,
        "scenario": scenario,
        "logparse_mode": "deterministic-fixture",
        "model_invocations": 0 if options.mode == "fake" else len(prompts),
        "role_attempts": prompts,
        "repair_counts": {
            "specialist": int("SPECIALIST:REPAIR" in actual_attempts),
            "reviewer": int("REVIEWER:REPAIR" in actual_attempts),
        },
        "records": record_receipts,
        "methods_result": methods_result,
        "methods_result_identity": {
            **_file_identity(methods_bytes),
            "case_id": methods_result["case_id"],
            "source_job_id": methods_result["source_job_id"],
            "result_ref": methods_result["result_ref"],
            "status": methods_result["status"],
            "evaluation_id": methods_result["evaluation_id"],
            "plan_ref": methods_result["plan_ref"],
            "evidence_graph_ref": methods_result["evidence_graph_ref"],
            "diagnostic_id": methods_result["diagnostic_id"],
        },
        "captured_execution_files": {
            key: {"filename": _CAPTURED_EVIDENCE_FILENAMES[key], **_file_identity(raw)}
            for key, raw in captured_files.items()
        },
        "captured_public_methods_result": {
            "filename": _PUBLIC_METHODS_RESULT_FILENAME,
            **_file_identity(methods_bytes),
        },
        "captured_loaded_methods": {
            "filename": _LOADED_METHODS_FILENAME,
            **_file_identity(loaded_methods_bytes),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "real"), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-wiki", type=Path)
    parser.add_argument("--scenario-root", type=Path)
    parser.add_argument("--registration-root", type=Path)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument("--node-entry", type=Path)
    parser.add_argument("--claude-entry", type=Path)
    parser.add_argument("--claude-settings", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--usage-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--fake-repair", action="store_true")
    parser.add_argument("--fake-rejected-method-id", action="append", default=[])
    parser.add_argument(
        "--fake-specialist-rejected-method-id", action="append", default=[]
    )
    parser.add_argument(
        "--fake-reviewer-rejected-method-id", action="append", default=[]
    )
    parser.add_argument(
        "--fake-protocol-exhausted-role",
        choices=("SPECIALIST", "REVIEWER"),
        action="append",
        default=[],
    )
    parser.add_argument(
        "--fake-model-failure-role",
        choices=("SPECIALIST", "REVIEWER"),
        action="append",
        default=[],
    )
    parser.add_argument(
        "--fake-server-invariant-role",
        choices=("SPECIALIST", "REVIEWER"),
        action="append",
        default=[],
    )
    parser.add_argument("--fake-no-matching-evidence", action="store_true")
    return parser


def _validated_options() -> argparse.Namespace:
    options = _parser().parse_args()
    options.source_root = options.source_root.resolve()
    default_wiki, default_scenario = _release_paths(options.source_root)
    options.source_wiki = (
        default_wiki if options.source_wiki is None else options.source_wiki.resolve()
    )
    options.scenario_root = (
        default_scenario
        if options.scenario_root is None
        else options.scenario_root.resolve()
    )
    if not options.source_wiki.is_file():
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_WIKI_MISSING",
            "The frozen release scenario Wiki is unavailable",
        )
    if not options.scenario_root.is_dir():
        _fail(
            "CLAUDE_DEEPSEEK_SCENARIO_ROOT_MISSING",
            "The frozen release scenario directory is unavailable",
        )
    if options.registration_root is not None:
        options.registration_root = options.registration_root.resolve()
    if options.mode == "real":
        required = (
            "registration_root",
            "node_entry",
            "claude_entry",
            "claude_settings",
            "config_root",
            "private_root",
            "evidence_root",
            "usage_root",
            "run_id",
        )
        if any(getattr(options, name) in (None, "") for name in required):
            _fail("CLAUDE_DEEPSEEK_REAL_INPUT_MISSING", "Real model-cert Runtime inputs are incomplete")
        for name in ("node_entry", "claude_entry", "claude_settings"):
            value = getattr(options, name).resolve()
            if not value.is_file():
                _fail("CLAUDE_DEEPSEEK_REAL_FILE_MISSING", f"{name} is unavailable")
            setattr(options, name, value)
        for name in ("config_root", "private_root", "evidence_root", "usage_root"):
            value = getattr(options, name).resolve()
            if not value.is_dir():
                _fail("CLAUDE_DEEPSEEK_REAL_DIRECTORY_MISSING", f"{name} is unavailable")
            setattr(options, name, value)
    return options


def main() -> int:
    try:
        options = _validated_options()
        receipt = run(options)
        raw = canonical_json_bytes(receipt)
        options.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with options.receipt_path.open("xb") as stream:
            stream.write(raw)
        sys.stdout.buffer.write(raw)
        return 0
    except BaseException as exc:
        value = {
            "schema_version": 1,
            "status": "FAIL",
            "code": getattr(exc, "code", "CLAUDE_DEEPSEEK_MODEL_CERT_RUNTIME_FAILED"),
            "message": str(exc),
        }
        sys.stderr.buffer.write(canonical_json_bytes(value))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
