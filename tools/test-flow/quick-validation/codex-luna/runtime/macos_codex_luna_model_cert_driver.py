#!/usr/bin/env python3
"""Run the production Evidence V2 Methods chain for the Codex/Luna cert.

The driver owns only test inputs and in-memory Ports.  Graph, Plan, role
validation, protocol repair, consensus, State, Outcome, and the public
``methods_result`` projection are produced by the production runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any, Literal


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "src"))
sys.path.insert(0, os.fspath(REPOSITORY_ROOT))

from problem_locator.application.outcome_submission import (  # noqa: E402
    OutcomeSubmissionService,
)
from problem_locator.application.queries import ApplicationQueryService  # noqa: E402
from problem_locator.contracts import (  # noqa: E402
    AssetKind,
    Attachment,
    AttachmentStatus,
    CaseAggregate,
    ErrorCode,
    ExecutionLogSinks,
    ExecutionStage,
    Job,
    MethodEvaluationPlanV2,
    ResourceKind,
    ResourceRef,
    ResolvedAsset,
    StateFile,
    VersionedRef,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.domain import (  # noqa: E402
    DomainCoordinator,
    PureContextSnapshotProjector,
)
from problem_locator.runtime.agent_backend import (  # noqa: E402
    AgentBackend,
    BackendExecution,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog  # noqa: E402
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime  # noqa: E402
from problem_locator.runtime.failures import runtime_failure  # noqa: E402
from problem_locator.runtime.methods_records_v2 import (  # noqa: E402
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
from problem_locator.runtime.workspace import WorkspaceManager  # noqa: E402
from tests.deterministic.contracts.fakes import (  # noqa: E402
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
from tests.deterministic.unit.runtime.test_diagnosis_runtime import (  # noqa: E402
    CATALOG_FIXTURES,
    LOGPARSE_ROOT,
    _MethodsTwoPassBackend,
    _json,
    _route_job,
)


Role = Literal["SPECIALIST", "REVIEWER"]
Attempt = Literal["PRIMARY", "REPAIR"]

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


class ModelCertRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ModelCertRuntimeError(code, message)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _file_identity(raw: bytes) -> dict[str, object]:
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


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
        raise RuntimeError(
            f"Production execution record is missing: {job_id}/{filename}"
        )
    return raw


def _ordinary_empty_directory(root: Path, label: str) -> Path:
    resolved = root.resolve()
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink() or any(resolved.iterdir()):
            _fail(
                "CODEX_LUNA_MODEL_CERT_RUNTIME_ROOT_NOT_EMPTY",
                f"{label} must be an empty ordinary directory",
            )
    else:
        resolved.mkdir(parents=True, mode=0o700)
    return resolved


def _copy_registration(registration_root: Path, skill_dir: Path) -> str:
    try:
        registration = json.loads(
            (registration_root / "registration-template.json").read_bytes()
        )
    except (OSError, ValueError, TypeError) as exc:
        raise ModelCertRuntimeError(
            "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID",
            "The generated registration cannot be loaded by the model-cert Runtime",
        ) from exc
    registration_id = registration.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        _fail(
            "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID",
            "The generated registration ID is invalid",
        )
    shutil.copytree(
        registration_root,
        skill_dir / registration_id,
        symlinks=False,
    )
    return registration_id


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
            id="logparse-tool/codex-luna-model-cert-fixture",
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
    source_root: Path,
    generated_registration: bool,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if generated_registration:
        scenario = (
            source_root
            / "tests/cases/release/rpc-timeout-anonymized/scenarios/multiple-rpc-timeouts"
        )
        driver = json.loads((scenario / "driver.json").read_bytes())
        names = driver["initial_user_fact_names"]
        values = driver["initial_user_fact_values"]
        target_contents = {
            "client": (scenario / "client.log").read_bytes(),
            "server": (scenario / "server.log").read_bytes(),
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


def _method_role(prompt: str) -> tuple[Role, Attempt]:
    marker = "<<<METHODS_EVIDENCE_V2_ROLE>>>"
    if prompt.count(marker) != 1 or not prompt.rstrip().endswith(
        "<<<END METHODS_EVIDENCE_V2_ROLE>>>"
    ):
        raise RuntimeError("backend received a non-Evidence-V2 role prompt")
    if "Role: Specialist. Attempt: primary evaluation." in prompt:
        return "SPECIALIST", "PRIMARY"
    if "Role: Specialist. Attempt: only repair." in prompt:
        return "SPECIALIST", "REPAIR"
    if "Role: Reviewer. Attempt: primary evaluation." in prompt:
        return "REVIEWER", "PRIMARY"
    if "Role: Reviewer. Attempt: only repair." in prompt:
        return "REVIEWER", "REPAIR"
    raise RuntimeError("backend received an unknown Evidence V2 role/attempt")


def _close_sinks(sinks: ExecutionLogSinks) -> None:
    unique = {id(sinks.stdout): sinks.stdout, id(sinks.stderr): sinks.stderr}
    for sink in unique.values():
        sink.flush()
        sink.close()


class FakeModelRoleBackend:
    """Zero-model role backend used only to exercise the production runtime."""

    def __init__(
        self,
        *,
        invalid_primary_roles: frozenset[Role] = frozenset(),
        rejected_method_ids: frozenset[str] = frozenset(),
        protocol_exhausted_roles: frozenset[Role] = frozenset(),
        model_failure_roles: frozenset[Role] = frozenset(),
        invariant_failure_roles: frozenset[Role] = frozenset(),
        no_matching_evidence: bool = False,
    ) -> None:
        self.invalid_primary_roles = invalid_primary_roles
        self.rejected_method_ids = rejected_method_ids
        self.protocol_exhausted_roles = protocol_exhausted_roles
        self.model_failure_roles = model_failure_roles
        self.invariant_failure_roles = invariant_failure_roles
        self.no_matching_evidence = no_matching_evidence
        self.invocations: list[dict[str, object]] = []

    def execute(self, **kwargs: Any) -> BackendExecution:
        prompt = str(kwargs["prompt"])
        role, attempt = _method_role(prompt)
        workspace = Path(kwargs["workspace_root"])
        plan = parse_canonical_json_bytes(
            (workspace / "inputs/method-evaluation-plan.json").read_bytes(),
            MethodEvaluationPlanV2,
        )
        self.invocations.append(
            {
                "ordinal": len(self.invocations) + 1,
                "role": role,
                "attempt": attempt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "prompt_size": len(prompt.encode("utf-8")),
                "workspace": workspace,
            }
        )
        output = (
            workspace / "output/method-diagnosis.draft.json"
            if role == "SPECIALIST"
            else workspace / "output/method-review.draft.json"
        )
        if role in self.model_failure_roles:
            _close_sinks(kwargs["log_sinks"])
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_EXIT_FAILED,
                message="injected model execution failure",
            )
        if role in self.invariant_failure_roles:
            _close_sinks(kwargs["log_sinks"])
            raise TypeError("injected backend invariant failure")
        if role in self.protocol_exhausted_roles or (
            attempt == "PRIMARY" and role in self.invalid_primary_roles
        ):
            response: object = {"invalid": "production parser must reject this root"}
        else:
            response = [
                {
                    "evaluation_ref": item.evaluation_ref,
                    "verdict": (
                        "REJECTED"
                        if item.method_id in self.rejected_method_ids
                        else "CONFIRMED"
                    ),
                    "reason": (
                        (
                            "冻结 Evidence Graph 不满足该方法的确认条件。"
                            if role == "SPECIALIST"
                            else "盲评确认冻结 Graph 与 Plan 不满足该方法的确认条件。"
                        )
                        if item.method_id in self.rejected_method_ids
                        else (
                            "冻结 Evidence Graph 满足该方法卡。"
                            if role == "SPECIALIST"
                            else "盲评确认冻结 Graph 与 Plan 支持该结论。"
                        )
                    ),
                }
                for item in plan.evaluations
            ]
        output.write_bytes(_canonical_json(response))
        _close_sinks(kwargs["log_sinks"])
        return BackendExecution(
            returncode=0,
            stdout_stderr_bytes=0,
            workspace_bytes=output.stat().st_size,
            elapsed_seconds=0.01,
        )


class EvidenceV2CertBackend:
    """Keep deterministic preprocessing outside the two model role calls."""

    def __init__(
        self,
        *,
        preprocessor: _MethodsTwoPassBackend,
        role_backend: AgentBackend | FakeModelRoleBackend,
    ) -> None:
        self.preprocessor = preprocessor
        self.role_backend = role_backend
        self.preprocessing_calls = 0

    def execute(self, **kwargs: Any) -> BackendExecution:
        if kwargs.get("broker_environment") is not None:
            self.preprocessing_calls += 1
            return self.preprocessor.execute(**kwargs)
        _method_role(str(kwargs["prompt"]))
        return self.role_backend.execute(**kwargs)


def _state_with_aggregate(aggregate: CaseAggregate) -> StateFile:
    value = json.loads(
        (
            REPOSITORY_ROOT
            / "tests/fixtures/contracts/positive/state.json"
        ).read_bytes()
    )
    value["cases"] = {
        aggregate.case.case_id: aggregate.model_dump(mode="json"),
    }
    return StateFile.model_validate(value)


def _aligned_aggregate(job: Job, raw: CaseAggregate) -> CaseAggregate:
    snapshot = job.context_snapshot
    if snapshot is None or job.skill_ref is None:
        raise RuntimeError("source Job is not bound to one Methods skill")
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


def _running_job_and_state(
    *,
    source_root: Path,
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
        _fail(
            "CODEX_LUNA_MODEL_CERT_SKILL_CARDINALITY_INVALID",
            "Model-cert requires exactly one specialized registration",
        )
    facts, target_contents = _fact_values(source_root, generated_registration)
    attachment_bytes = b"model-cert deterministic archive descriptor\n"
    attachment_sha256 = hashlib.sha256(attachment_bytes).hexdigest()
    attachment_id = "00000000-0000-4000-8000-000000000450"
    payload = _route_job().model_dump(mode="json")
    payload.update(catalog.diagnose_bindings(skill_refs[0]).model_dump(mode="json"))
    payload.update(
        {
            "job_type": "DIAGNOSE",
            "goal": "Run the frozen Evidence V2 Codex/Luna model-cert scenario.",
            "status": "RUNNING",
            "started_at": "2026-08-29T00:00:01.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-4000-8000-000000000498",
            "attachment_refs": [attachment_id],
        }
    )
    payload["context_snapshot"]["user_facts"] = facts
    job = Job.model_validate(payload)
    storage_key = (
        f"resources/cases/{job.case_id}/attachments/{attachment_id}/logs.zip"
    )
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


def _claim_active_review(repository: InMemoryStateRepository) -> Job:
    state = repository.read_snapshot()
    value = state.model_dump(mode="json")
    aggregate = next(iter(value["cases"].values()))
    review_job_id = aggregate["case"]["active_job_id"]
    if not isinstance(review_job_id, str):
        raise RuntimeError("Specialist did not publish one Reviewer Job")
    review = aggregate["jobs"][review_job_id]
    review.update(
        {
            "status": "RUNNING",
            "started_at": "2026-08-29T00:04:30.000Z",
            "finished_at": None,
            "runtime_epoch": "00000000-0000-4000-8000-000000000097",
        }
    )
    repository.seed(StateFile.model_validate(value))
    return repository.read_job(review_job_id)


def _pending_job(job: Job) -> Job:
    value = job.model_dump(mode="json")
    value.update(
        {
            "status": "PENDING",
            "started_at": None,
            "finished_at": None,
            "runtime_epoch": None,
        }
    )
    return Job.model_validate(value)


def _prompt_receipts(
    records: InMemoryExecutionRecordStore,
    *,
    specialist_job_id: str,
    reviewer_job_id: str | None,
) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
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
                    "rejected_response": (
                        None if rejected is None else _file_identity(rejected)
                    ),
                }
            )
    return receipts


def _agent_command(options: argparse.Namespace) -> str:
    wrapper = (
        options.source_root
        / "tools/test-flow/quick-validation/codex-luna/runtime"
        / "macos-codex-luna-model-cert-wrapper.mjs"
    )
    arguments = [
        str(options.node_entry),
        str(wrapper),
        "--codex-entry",
        str(options.codex_entry),
        "--auth-source",
        str(options.auth_source),
        "--skill-source",
        str(options.skill_source),
        "--expected-cli-version",
        options.expected_cli_version,
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


def run_production_model_cert(
    *,
    work_root: Path,
    role_backend: AgentBackend | FakeModelRoleBackend,
    source_root: Path = REPOSITORY_ROOT,
    registration_root: Path | None = None,
    evidence_root: Path | None = None,
    execution_mode: Literal["deterministic-zero-model", "real-model"] = (
        "deterministic-zero-model"
    ),
) -> dict[str, object]:
    work_root = _ordinary_empty_directory(work_root, "model-cert work root")
    broker_factory = FakeLogparseBrokerFactory()
    guard = InMemoryPublicationCommitGuard()
    catalog, registration_id = _catalog(
        work_root,
        registration_root,
        broker_factory,
    )
    loaded_registration_root = work_root / "skill-dir" / registration_id
    source_job, aggregate, resources, target_contents = _running_job_and_state(
        source_root=source_root,
        catalog=catalog,
        generated_registration=registration_root is not None,
        publication_guard=guard,
    )
    repository = InMemoryStateRepository(_state_with_aggregate(aggregate))
    records = InMemoryExecutionRecordStore()
    records.publish_job(_pending_job(source_job))
    preprocessor = _MethodsTwoPassBackend(
        broker_factory,
        source_job,
        "completed",
    )
    preprocessor.target_contents = target_contents
    if getattr(role_backend, "no_matching_evidence", False):
        preprocessor.target_contents = {
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
            raise RuntimeError(
                "Production preprocessing requested an unexpected broker operation"
            )
        preprocessor._run_preprocessing(  # noqa: SLF001 - test-flow fixture port
            {"workspace_root": getattr(session, "workspace_root")}
        )
        return None

    broker_factory.preprocessing_executor = execute_preprocessing
    backend = EvidenceV2CertBackend(
        preprocessor=preprocessor,
        role_backend=role_backend,
    )
    specialist_runtime = DiagnosisRuntime(
        state_repository=repository,
        resource_store=resources,
        asset_catalog=catalog,
        logparse_broker_factory=broker_factory,
        execution_records=records,
        clock=FakeClock("2026-08-29T00:03:00.000Z"),
        id_generator=DeterministicIdGenerator(seed="p2-model-cert-specialist"),
        workspace_manager=WorkspaceManager(work_root / "specialist-runtime"),
        backend=backend,
    )
    specialist_receipt = specialist_runtime.execute(
        source_job,
        InMemoryCancellationSignal(),
    )
    notifier = InMemoryStateChangeNotifier()
    submission = OutcomeSubmissionService(
        repository,
        resources,
        guard,
        records,
        DomainCoordinator(),
        PureContextSnapshotProjector(),
        catalog,
        RecordingDispatcher(),
        notifier,
        FakeClock("2026-08-29T00:04:00.000Z"),
        DeterministicIdGenerator(seed="p2-model-cert-submission"),
    )
    handoff = submission.submit_outcome(
        specialist_receipt.job_outcome,
        specialist_receipt.outcome_file_ref,
    )
    if handoff.disposition.value != "APPLIED":
        raise RuntimeError(
            "Production Specialist Outcome was not applied: "
            f"disposition={handoff.disposition.value}"
        )
    review_job = None
    reviewer_receipt = None
    terminal = handoff
    if specialist_receipt.job_outcome.methods_review_target is not None:
        if handoff.case_view.active_job is None:
            raise RuntimeError("Production submission did not expose the Reviewer Job")
        review_job = _claim_active_review(repository)
        reviewer_runtime = DiagnosisRuntime(
            state_repository=repository,
            resource_store=resources,
            asset_catalog=catalog,
            logparse_broker_factory=None,
            execution_records=records,
            clock=FakeClock("2026-08-29T00:05:00.000Z"),
            id_generator=DeterministicIdGenerator(seed="p2-model-cert-reviewer"),
            workspace_manager=WorkspaceManager(work_root / "reviewer-runtime"),
            backend=backend,
        )
        reviewer_receipt = reviewer_runtime.execute(
            review_job,
            InMemoryCancellationSignal(),
        )
        terminal = submission.submit_outcome(
            reviewer_receipt.job_outcome,
            reviewer_receipt.outcome_file_ref,
        )
        if terminal.disposition.value != "APPLIED":
            raise RuntimeError(
                "Production Reviewer Outcome was not applied: "
                f"disposition={terminal.disposition.value}"
            )
    query = ApplicationQueryService(repository, resources, notifier)
    public_case = query.get_case(source_job.case_id).case_view
    methods_result = public_case.methods_result
    if methods_result is None:
        raise RuntimeError(
            "Production query omitted terminal methods_result: "
            f"specialist_result={specialist_receipt.job_outcome.result_type.value}; "
            f"specialist_error={specialist_receipt.job_outcome.error.code.value if specialist_receipt.job_outcome.error is not None else None}; "
            f"specialist_terminal={specialist_receipt.job_outcome.methods_terminal_projection is not None}; "
            f"specialist_review={specialist_receipt.job_outcome.methods_review_target is not None}; "
            f"submission={handoff.disposition.value}; "
            f"submitted_status={handoff.case_view.status.value if handoff.case_view is not None else None}; "
            f"submitted_methods={handoff.case_view.methods_result is not None if handoff.case_view is not None else None}; "
            f"queried_status={public_case.status.value}"
        )
    graph = read_method_evidence_graph_v2(records, job_id=source_job.job_id)
    plan = read_method_evaluation_plan_v2(records, job_id=source_job.job_id)
    limitations = read_method_limitations_record_v2(
        records,
        job_id=source_job.job_id,
    )
    source_state = read_method_state_v2(records, job_id=source_job.job_id)
    terminal_job_id = source_job.job_id if review_job is None else review_job.job_id
    terminal_state = read_method_state_v2(records, job_id=terminal_job_id)
    if (
        graph is None
        or plan is None
        or limitations is None
        or source_state is None
        or terminal_state is None
    ):
        raise RuntimeError("Production execution records are incomplete")
    if (
        source_job.skill_ref is None
        or source_job.skill_ref.content_hash != graph.skill_sha256
        or graph.skill_sha256 != plan.skill_sha256
        or graph.graph_ref != plan.evidence_graph_ref
    ):
        raise RuntimeError(
            "Production Skill, Evidence Graph, and Evaluation Plan identities differ"
        )
    if (
        methods_result.evidence_graph_ref != graph.graph_ref
        or methods_result.plan_ref != plan.plan_ref
    ):
        raise RuntimeError(
            "Public methods_result does not bind the production Graph and Plan"
        )
    prompts = _prompt_receipts(
        records,
        specialist_job_id=source_job.job_id,
        reviewer_job_id=None if review_job is None else review_job.job_id,
    )
    projection_bytes = canonical_json_bytes(methods_result)
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
    source_state_bytes = captured_files["source_state"]
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
    capture_root = evidence_root or (work_root / "model-cert-evidence")
    for key, raw in captured_files.items():
        _write_new_bytes(capture_root, _CAPTURED_EVIDENCE_FILENAMES[key], raw)
    _write_new_bytes(
        capture_root,
        _PUBLIC_METHODS_RESULT_FILENAME,
        projection_bytes,
    )
    _write_new_bytes(
        capture_root,
        _LOADED_METHODS_FILENAME,
        loaded_methods_bytes,
    )
    actual_attempts = {
        f"{item['role']}:{item['attempt']}"
        for item in prompts
    }
    selected_registration = (
        registration_root / "registration-template.json"
        if registration_root is not None
        else CATALOG_FIXTURES
        / "skill-dir"
        / registration_id
        / "registration-template.json"
    )
    registration = json.loads(selected_registration.read_bytes())
    if registration_root is not None:
        driver = json.loads(
            (
                source_root
                / "tests/cases/release/rpc-timeout-anonymized/scenarios"
                / "multiple-rpc-timeouts/driver.json"
            ).read_bytes()
        )
        user_inputs = {
            "initial_user_fact_names": driver["initial_user_fact_names"],
            "initial_user_fact_values": driver["initial_user_fact_values"],
        }
    else:
        frozen_facts = source_job.context_snapshot.user_facts  # type: ignore[union-attr]
        user_inputs = {
            "initial_user_fact_names": [
                item.provenance.input_name for item in frozen_facts
            ],
            "initial_user_fact_values": [item.statement for item in frozen_facts],
        }
    record_receipts: dict[str, object] = {
        "source_job": {
            "filename": _CAPTURED_EVIDENCE_FILENAMES["source_job"],
            **_file_identity(captured_files["source_job"]),
        },
        "source_job_id": source_job.job_id,
        "terminal_job_id": terminal_job_id,
        "graph": {
            "filename": METHODS_EVIDENCE_GRAPH_V2_FILENAME,
            "ref": graph.graph_ref,
            **_file_identity(graph_bytes),
        },
        "plan": {
            "filename": METHODS_EVALUATION_PLAN_V2_FILENAME,
            "ref": plan.plan_ref,
            **_file_identity(plan_bytes),
        },
        "source_state": {
            "filename": METHODS_STATE_V2_FILENAME,
            "status": source_state.status,
            **_file_identity(source_state_bytes),
        },
        "limitations": {
            "filename": METHODS_LIMITATIONS_V2_FILENAME,
            **_file_identity(limitations_bytes),
        },
        "specialist_outcome": {
            "filename": "job_outcome.json",
            "result_type": specialist_receipt.job_outcome.result_type.value,
            **_file_identity(specialist_outcome_bytes),
        },
    }
    if review_job is not None and reviewer_receipt is not None:
        record_receipts.update(
            {
                "reviewer_job": {
                    "filename": _CAPTURED_EVIDENCE_FILENAMES["reviewer_job"],
                    **_file_identity(captured_files["reviewer_job"]),
                },
                "terminal_state": {
                    "filename": METHODS_STATE_V2_FILENAME,
                    "status": terminal_state.status,
                    **_file_identity(captured_files["terminal_state"]),
                },
                "reviewer_outcome": {
                    "filename": "job_outcome.json",
                    "result_type": reviewer_receipt.job_outcome.result_type.value,
                    **_file_identity(captured_files["reviewer_outcome"]),
                },
            }
        )
    result = {
        "schema_version": 1,
        "receipt_type": "codex-luna-evidence-v2-runtime-result",
        "status": "PASS",
        "execution_mode": execution_mode,
        "runtime_driver": "codex-luna-model-cert-v1",
        "production_runtime": (
            "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime"
        ),
        "scenario_id": (
            "multiple-rpc-timeouts"
            if registration_root is not None
            else "deterministic-rpc-timeout"
        ),
        "registration_id": registration_id,
        "scenario": {
            "scenario_id": "multiple-rpc-timeouts",
            "source_wiki_sha256": registration["package"][
                "source_wiki_sha256"
            ],
            "registration_id": registration_id,
            "skill_content_sha256": source_job.skill_ref.content_hash,  # type: ignore[union-attr]
            "user_inputs_sha256": hashlib.sha256(
                canonical_json_bytes(user_inputs)
            ).hexdigest(),
            "sources": [
                {
                    "source_id": source.source_id,
                    "content_sha256": source.content_sha256,
                }
                for source in graph.sources
            ],
            "evidence_graph": {
                "ref": graph.graph_ref,
                "canonical_sha256": hashlib.sha256(graph_bytes).hexdigest(),
                "canonical_size": len(graph_bytes),
            },
            "evaluation_plan": {
                "ref": plan.plan_ref,
                "canonical_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "canonical_size": len(plan_bytes),
            },
        },
        "logparse_mode": "deterministic-fixture",
        "preprocessing_calls": backend.preprocessing_calls,
        "model_invocations": 0 if execution_mode == "deterministic-zero-model" else len(prompts),
        "role_attempts": prompts,
        "repair_counts": {
            "specialist": int("SPECIALIST:REPAIR" in actual_attempts),
            "reviewer": int("REVIEWER:REPAIR" in actual_attempts),
        },
        "records": record_receipts,
        "public_case_status": terminal.case_view.status.value,
        "methods_result": methods_result.model_dump(mode="json"),
        "methods_result_identity": {
            **_file_identity(projection_bytes),
            "case_id": methods_result.case_id,
            "source_job_id": methods_result.source_job_id,
            "result_ref": methods_result.result_ref,
            "evaluation_id": methods_result.evaluation_id,
            "status": methods_result.status,
            "plan_ref": methods_result.plan_ref,
            "evidence_graph_ref": methods_result.evidence_graph_ref,
            "diagnostic_id": methods_result.diagnostic_id,
        },
        "captured_execution_files": {
            key: {
                "filename": _CAPTURED_EVIDENCE_FILENAMES[key],
                **_file_identity(raw),
            }
            for key, raw in captured_files.items()
        },
        "captured_public_methods_result": {
            "filename": _PUBLIC_METHODS_RESULT_FILENAME,
            **_file_identity(projection_bytes),
        },
        "captured_loaded_methods": {
            "filename": _LOADED_METHODS_FILENAME,
            **_file_identity(loaded_methods_bytes),
        },
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "real"), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--registration-root", type=Path)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument("--node-entry", type=Path)
    parser.add_argument("--codex-entry", type=Path)
    parser.add_argument("--auth-source", type=Path)
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--expected-cli-version")
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--usage-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--fake-repair", action="store_true")
    parser.add_argument(
        "--fake-invalid-primary-role",
        choices=("SPECIALIST", "REVIEWER"),
        action="append",
        default=[],
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
    parser.add_argument(
        "--fake-rejected-method-id",
        action="append",
        default=[],
    )
    return parser


def _validated_options(argv: list[str]) -> argparse.Namespace:
    options = _parser().parse_args(argv)
    options.source_root = options.source_root.resolve()
    options.work_root = options.work_root.resolve()
    options.receipt_path = options.receipt_path.resolve()
    if options.registration_root is not None:
        options.registration_root = options.registration_root.resolve()
    if options.mode == "real":
        required = (
            "registration_root",
            "node_entry",
            "codex_entry",
            "auth_source",
            "skill_source",
            "expected_cli_version",
            "private_root",
            "evidence_root",
            "usage_root",
            "run_id",
        )
        if any(getattr(options, name) in (None, "") for name in required):
            _fail(
                "CODEX_LUNA_MODEL_CERT_REAL_INPUT_MISSING",
                "Real model-cert Runtime inputs are incomplete",
            )
        for name in (
            "node_entry",
            "codex_entry",
            "auth_source",
            "skill_source",
        ):
            value = getattr(options, name).resolve()
            if not value.is_file():
                _fail(
                    "CODEX_LUNA_MODEL_CERT_REAL_FILE_MISSING",
                    f"{name} is unavailable",
                )
            setattr(options, name, value)
        for name in ("private_root", "evidence_root", "usage_root"):
            value = getattr(options, name).resolve()
            if not value.is_dir():
                _fail(
                    "CODEX_LUNA_MODEL_CERT_REAL_DIRECTORY_MISSING",
                    f"{name} is unavailable",
                )
            setattr(options, name, value)
    return options


def main(argv: list[str] | None = None) -> int:
    try:
        values = _validated_options(sys.argv[1:] if argv is None else argv)
        if values.mode == "fake":
            invalid_roles = set(values.fake_invalid_primary_role)
            if values.fake_repair:
                invalid_roles.update(("SPECIALIST", "REVIEWER"))
            role_backend: AgentBackend | FakeModelRoleBackend = (
                FakeModelRoleBackend(
                    invalid_primary_roles=frozenset(invalid_roles),
                    rejected_method_ids=frozenset(
                        values.fake_rejected_method_id
                    ),
                    protocol_exhausted_roles=frozenset(
                        values.fake_protocol_exhausted_role
                    ),
                    model_failure_roles=frozenset(
                        values.fake_model_failure_role
                    ),
                    invariant_failure_roles=frozenset(
                        values.fake_server_invariant_role
                    ),
                    no_matching_evidence=values.fake_no_matching_evidence,
                )
            )
            execution_mode: Literal["deterministic-zero-model", "real-model"] = (
                "deterministic-zero-model"
            )
        else:
            role_backend = AgentBackend(
                _agent_command(values),
                parent_environment=dict(os.environ),
            )
            execution_mode = "real-model"
        result = run_production_model_cert(
            work_root=values.work_root,
            role_backend=role_backend,
            source_root=values.source_root,
            registration_root=values.registration_root,
            evidence_root=values.evidence_root,
            execution_mode=execution_mode,
        )
        raw = canonical_json_bytes(result)
        values.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with values.receipt_path.open("xb") as stream:
            stream.write(raw)
        sys.stdout.buffer.write(raw)
        return 0
    except BaseException as exc:
        failure = {
            "schema_version": 1,
            "status": "FAIL",
            "code": getattr(
                exc,
                "code",
                "CODEX_LUNA_MODEL_CERT_RUNTIME_FAILED",
            ),
            "message": str(exc),
        }
        sys.stderr.buffer.write(canonical_json_bytes(failure))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
