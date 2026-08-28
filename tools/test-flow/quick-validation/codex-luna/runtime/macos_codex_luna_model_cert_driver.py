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
    CaseAggregate,
    ExecutionLogSinks,
    Job,
    MethodEvaluationPlanV2,
    ResourceKind,
    ResourceRef,
    StateFile,
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
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime  # noqa: E402
from problem_locator.runtime.methods_records_v2 import (  # noqa: E402
    METHODS_EVALUATION_PLAN_V2_FILENAME,
    METHODS_EVIDENCE_GRAPH_V2_FILENAME,
    METHODS_LIMITATIONS_V2_FILENAME,
    METHODS_STATE_V2_FILENAME,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
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
    _MethodsTwoPassBackend,
    _claimed_logparse_job_state_and_resources,
    _logparse_catalog,
)


Role = Literal["SPECIALIST", "REVIEWER"]
Attempt = Literal["PRIMARY", "REPAIR"]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


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
    ) -> None:
        self.invalid_primary_roles = invalid_primary_roles
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
        if attempt == "PRIMARY" and role in self.invalid_primary_roles:
            response: object = {"invalid": "production parser must reject this root"}
        else:
            response = [
                {
                    "evaluation_ref": item.evaluation_ref,
                    "verdict": "CONFIRMED",
                    "reason": (
                        "The frozen Evidence V2 evaluation satisfies the method."
                        if role == "SPECIALIST"
                        else "Independent blind review confirms the frozen evaluation."
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


def run_production_model_cert(
    *,
    work_root: Path,
    role_backend: AgentBackend | FakeModelRoleBackend,
) -> dict[str, object]:
    work_root.mkdir(parents=True, exist_ok=False)
    broker_factory = FakeLogparseBrokerFactory()
    catalog = _logparse_catalog(work_root / "catalog", broker_factory)
    source_job, raw_aggregate, _ = (
        _claimed_logparse_job_state_and_resources(catalog)
    )
    aggregate = _aligned_aggregate(source_job, raw_aggregate)
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    attachment = next(iter(aggregate.attachments.values()))
    attachment_payload = b"request timed out while calling inventory\n"
    resources.seed_formal_resource(
        ResourceRef(
            resource_kind=ResourceKind.FILE,
            storage_key=attachment.storage_key,
            size=attachment.size,
            sha256=attachment.sha256,
        ),
        state_reference_count=1,
        payload=attachment_payload,
    )
    repository = InMemoryStateRepository(_state_with_aggregate(aggregate))
    records = InMemoryExecutionRecordStore()
    records.publish_job(_pending_job(source_job))
    preprocessor = _MethodsTwoPassBackend(
        broker_factory,
        source_job,
        "completed",
    )
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
    if specialist_receipt.job_outcome.methods_review_target is None:
        raise RuntimeError(
            "Specialist did not produce the production Reviewer handoff: "
            f"result_type={specialist_receipt.job_outcome.result_type.value}; "
            f"error={specialist_receipt.job_outcome.error}"
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
    query = ApplicationQueryService(repository, resources, notifier)
    public_case = query.get_case(source_job.case_id).case_view
    methods_result = public_case.methods_result
    if methods_result is None:
        raise RuntimeError("Production query omitted terminal methods_result")
    graph = read_method_evidence_graph_v2(records, job_id=source_job.job_id)
    plan = read_method_evaluation_plan_v2(records, job_id=source_job.job_id)
    source_state = read_method_state_v2(records, job_id=source_job.job_id)
    terminal_state = read_method_state_v2(records, job_id=review_job.job_id)
    if graph is None or plan is None or source_state is None or terminal_state is None:
        raise RuntimeError("Production execution records are incomplete")
    projection_bytes = canonical_json_bytes(methods_result)
    invocation_records = getattr(role_backend, "invocations", None)
    result = {
        "schema_version": 1,
        "receipt_type": "codex-luna-evidence-v2-runtime-result",
        "status": "PASS",
        "runtime_driver": "test-flow",
        "production_runtime": True,
        "logparse_mode": "deterministic-fixture",
        "preprocessing_calls": backend.preprocessing_calls,
        "model_role_invocations": (
            [
                {
                    key: value
                    for key, value in item.items()
                    if key != "workspace"
                }
                for item in invocation_records
            ]
            if isinstance(invocation_records, list)
            else None
        ),
        "records": {
            "source_job_id": source_job.job_id,
            "terminal_job_id": review_job.job_id,
            "graph": {
                "filename": METHODS_EVIDENCE_GRAPH_V2_FILENAME,
                "ref": graph.graph_ref,
            },
            "plan": {
                "filename": METHODS_EVALUATION_PLAN_V2_FILENAME,
                "ref": plan.plan_ref,
            },
            "source_state": {
                "filename": METHODS_STATE_V2_FILENAME,
                "status": source_state.status,
            },
            "terminal_state": {
                "filename": METHODS_STATE_V2_FILENAME,
                "status": terminal_state.status,
            },
            "limitations": {"filename": METHODS_LIMITATIONS_V2_FILENAME},
            "specialist_outcome": {
                "filename": "job_outcome.json",
                "result_type": specialist_receipt.job_outcome.result_type.value,
            },
            "reviewer_outcome": {
                "filename": "job_outcome.json",
                "result_type": reviewer_receipt.job_outcome.result_type.value,
            },
        },
        "public_case_status": terminal.case_view.status.value,
        "methods_result": methods_result.model_dump(mode="json"),
        "methods_result_identity": {
            "canonical_sha256": hashlib.sha256(projection_bytes).hexdigest(),
            "canonical_size": len(projection_bytes),
            "case_id": methods_result.case_id,
            "source_job_id": methods_result.source_job_id,
            "result_ref": methods_result.result_ref,
            "evaluation_id": methods_result.evaluation_id,
            "status": methods_result.status,
            "plan_ref": methods_result.plan_ref,
            "evidence_graph_ref": methods_result.evidence_graph_ref,
            "diagnostic_id": methods_result.diagnostic_id,
        },
        "legacy_surfaces": {
            "candidate": False,
            "grounding": False,
            "partial_status": False,
            "artifact_result": False,
        },
    }
    return result


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backend-command")
    mode.add_argument("--fake-role-backend", action="store_true")
    parser.add_argument(
        "--fake-invalid-primary-role",
        choices=("SPECIALIST", "REVIEWER"),
        action="append",
        default=[],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    values = _arguments(sys.argv[1:] if argv is None else argv)
    if values.fake_role_backend:
        role_backend: AgentBackend | FakeModelRoleBackend = FakeModelRoleBackend(
            invalid_primary_roles=frozenset(values.fake_invalid_primary_role),
        )
    else:
        role_backend = AgentBackend(str(values.backend_command))
    result = run_production_model_cert(
        work_root=values.work_root.resolve(),
        role_backend=role_backend,
    )
    values.output.parent.mkdir(parents=True, exist_ok=True)
    values.output.write_bytes(_canonical_json(result))
    sys.stdout.buffer.write(
        _canonical_json(
            {
                "status": result["status"],
                "output": os.fspath(values.output.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
