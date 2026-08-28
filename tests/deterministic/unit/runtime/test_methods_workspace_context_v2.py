from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CaseAggregate,
    ErrorCode,
    ExecutionStage,
    Job,
    JobOutcome,
    MaterializedPath,
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
    MethodsReviewTargetV2,
    StateFile,
    VersionedRef,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.context_builder import ContextBuilder
from problem_locator.runtime.context_policy import RuntimeAssetResolver
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_records_v2 import (
    publish_method_evaluation_plan_v2,
    publish_method_evidence_graph_v2,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.workspace import WorkspaceManager
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.execution_records import FileExecutionRecordStore

from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "tests/fixtures/contracts/positive"
USER_FACT_VALUE = "threshold=37"
PRIVATE_TARGET_SENTINEL = "private-unmatched-target-bytes"
PRIVATE_STATE_SENTINEL = "private-candidate-state-sentinel"
PRIVATE_CANDIDATE_SENTINEL = "The inventory RPC exceeded its deadline."
PRIVATE_ATTACHMENT_SENTINEL = b"private attachment input"
PRIVATE_ARTIFACT_SENTINEL = b"private artifact input"
PRIVATE_EVIDENCE_SENTINEL = b"private evidence input"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000060"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
PREVIOUS_OUTCOME_ID = "00000000-0000-0000-0000-000000000023"


class _UnusedResourceStore:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"empty Methods Workspace must not call {name}")


class _ResourceStore:
    def __init__(self) -> None:
        self.payloads = {
            "resources/private-attachment": PRIVATE_ATTACHMENT_SENTINEL,
            "resources/private-artifact": PRIVATE_ARTIFACT_SENTINEL,
            "resources/private-evidence": PRIVATE_EVIDENCE_SENTINEL,
        }

    def materialize_read_only(self, resource_ref, destination: Path) -> MaterializedPath:
        payload = self.payloads[resource_ref.storage_key]
        assert len(payload) == resource_ref.size
        assert hashlib.sha256(payload).hexdigest() == resource_ref.sha256
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(0o444)
        return MaterializedPath(path=str(destination), read_only=True)


def _fixture(name: str, model_type):
    return parse_canonical_json_bytes(
        (CONTRACTS / name).read_bytes(), model_type=model_type
    )


def _aggregate(job: Job, *, source_job: Job | None = None) -> CaseAggregate:
    state = _fixture("state.json", StateFile)
    aggregate = next(iter(state.cases.values()))
    value = aggregate.model_dump(mode="json")
    snapshot = job.context_snapshot
    assert snapshot is not None
    value["case"].update(
        {
            "active_job_id": job.job_id,
            "status": "REVIEWING" if job.job_type.value == "REVIEW" else "RUNNING",
            "selected_skill_ref": job.skill_ref.model_dump(mode="json"),
            "diagnosis_state": {
                "revision": snapshot.diagnosis_state_revision,
                "problem_spec": snapshot.problem_spec.model_dump(mode="json"),
                "user_facts": [item.model_dump(mode="json") for item in snapshot.user_facts],
                "confirmed_facts": [
                    item.model_dump(mode="json") for item in snapshot.confirmed_facts
                ],
                "active_hypotheses": [
                    item.model_dump(mode="json") for item in snapshot.active_hypotheses
                ],
                "rejected_hypotheses": [
                    item.model_dump(mode="json") for item in snapshot.rejected_hypotheses
                ],
                "open_questions": [
                    item.model_dump(mode="json") for item in snapshot.open_questions
                ],
                "pending_requirements": [
                    item.model_dump(mode="json") for item in snapshot.pending_requirements
                ],
                "evidence_refs": list(snapshot.evidence_refs),
                "candidate_conclusion": (
                    None
                    if snapshot.candidate_conclusion is None
                    else snapshot.candidate_conclusion.model_dump(mode="json")
                ),
            },
        }
    )
    jobs = {job.job_id: job.model_dump(mode="json")}
    if source_job is not None:
        source_value = source_job.model_dump(mode="json")
        source_value.update(
            {
                "status": "SUCCEEDED",
                "started_at": "2026-07-31T00:01:10.000Z",
                "finished_at": "2026-07-31T00:01:20.000Z",
                "runtime_epoch": "00000000-0000-0000-0000-000000000099",
            }
        )
        jobs[source_job.job_id] = source_value
    resource_owner = source_job or job
    if resource_owner.previous_outcome_refs:
        prior = _fixture("job-route.json", Job).model_dump(mode="json")
        prior.update(
            {
                "status": "FAILED",
                "started_at": "2026-07-31T00:00:10.000Z",
                "finished_at": "2026-07-31T00:00:30.000Z",
                "runtime_epoch": "00000000-0000-0000-0000-000000000098",
            }
        )
        jobs[prior["job_id"]] = prior
    value["jobs"] = jobs
    if resource_owner.attachment_refs:
        attachment_sha = hashlib.sha256(PRIVATE_ATTACHMENT_SENTINEL).hexdigest()
        value["attachments"] = {
            ATTACHMENT_ID: {
                "attachment_id": ATTACHMENT_ID,
                "case_id": job.case_id,
                "status": "READY",
                "name": "private.txt",
                "content_type": "text/plain",
                "declared_size": len(PRIVATE_ATTACHMENT_SENTINEL),
                "declared_sha256": attachment_sha,
                "size": len(PRIVATE_ATTACHMENT_SENTINEL),
                "sha256": attachment_sha,
                "storage_key": "resources/private-attachment",
                "created_at": "2026-07-31T00:00:00.000Z",
                "updated_at": "2026-07-31T00:00:00.000Z",
            }
        }
    if resource_owner.artifact_refs:
        artifact_sha = hashlib.sha256(PRIVATE_ARTIFACT_SENTINEL).hexdigest()
        value["artifacts"] = {
            ARTIFACT_ID: {
                "artifact_id": ARTIFACT_ID,
                "case_id": job.case_id,
                "kind": "DIAGNOSTIC_EXPORT",
                "name": "private-artifact.json",
                "content_type": "application/json",
                "resource_kind": "FILE",
                "size": len(PRIVATE_ARTIFACT_SENTINEL),
                "sha256": artifact_sha,
                "storage_key": "resources/private-artifact",
                "metadata": {
                    "schema_version": 1,
                    "format_id": "private-diagnostic-v1",
                    "description": "Private pre-V2 artifact.",
                },
                "created_by_job_id": resource_owner.job_id,
                "created_at": "2026-07-31T00:00:40.000Z",
            }
        }
    if resource_owner.context_snapshot and resource_owner.context_snapshot.evidence_refs:
        evidence_sha = hashlib.sha256(PRIVATE_EVIDENCE_SENTINEL).hexdigest()
        value["evidence"] = {
            EVIDENCE_ID: {
                "evidence_id": EVIDENCE_ID,
                "case_id": job.case_id,
                "source_type": "TOOL_OUTPUT",
                "source_ref": ARTIFACT_ID,
                "locator": {
                    "kind": "TOOL_OUTPUT",
                    "relative_path": "private-evidence.json",
                    "json_pointer": None,
                },
                "summary": "Private pre-V2 Evidence.",
                "collected_at": "2026-07-31T00:00:40.000Z",
                "content_hash": evidence_sha,
                "resource_ref": {
                    "resource_kind": "FILE",
                    "storage_key": "resources/private-evidence",
                    "size": len(PRIVATE_EVIDENCE_SENTINEL),
                    "sha256": evidence_sha,
                },
            }
        }
    if resource_owner.previous_outcome_refs:
        outcome = _fixture("job-outcome-failure.json", JobOutcome)
        outcome_bytes = canonical_json_bytes(outcome)
        outcome_sha = hashlib.sha256(outcome_bytes).hexdigest()
        value["outcomes"] = {
            PREVIOUS_OUTCOME_ID: outcome.model_dump(mode="json")
        }
        value["outcome_processing_records"] = {
            PREVIOUS_OUTCOME_ID: {
                "outcome_id": PREVIOUS_OUTCOME_ID,
                "job_id": outcome.job_id,
                "outcome_hash": outcome_sha,
                "outcome_file_ref": {
                    "relative_key": f"jobs/{outcome.job_id}/job_outcome.json",
                    "size": len(outcome_bytes),
                    "sha256": outcome_sha,
                },
                "disposition": "STALE",
                "processed_at": "2026-07-31T00:00:40.000Z",
                "error_code": None,
                "accepted_evidence_ids": [],
                "accepted_artifact_ids": [],
                "generated_artifact_ids": [],
                "created_job_id": None,
                "reason": "Stored prior Outcome for hard-cut coverage.",
            }
        }
    return CaseAggregate.model_validate(value)


def _user_fact() -> dict[str, object]:
    return {
        "item_id": "00000000-0000-0000-0000-000000000074",
        "statement": USER_FACT_VALUE,
        "status": "ACTIVE",
        "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "00000000-0000-0000-0000-000000000090",
            "input_name": "threshold_config",
        },
        "evidence_refs": [],
        "created_revision": 3,
        "supersedes": [],
    }


def _jobs(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skill = load_test_methods_skill(
        skills_root,
        name="workspace-context-v2",
        methods=(("slow-execution", "API_COMPLETE"),),
    )
    catalog = VersionedAssetCatalog(
        skill_dir=skills_root,
        generic_skill_name="workspace-context-v2",
    )
    skill_ref = VersionedRef(
        id="diagnosis-skill/workspace-context-v2",
        version=skill.registration.version,
        content_hash=skill.combined_sha256,
    )

    specialist_value = _fixture("job-diagnose.json", Job).model_dump(mode="json")
    specialist_value.update(catalog.diagnose_bindings(skill_ref).model_dump(mode="json"))
    specialist_value.update(
        {
            "attachment_refs": [ATTACHMENT_ID],
            "artifact_refs": [ARTIFACT_ID],
            "evidence_refs": [EVIDENCE_ID],
            "previous_outcome_refs": [PREVIOUS_OUTCOME_ID],
            "base_state_revision": 3,
        }
    )
    specialist_snapshot = _fixture("job-review.json", Job).context_snapshot
    assert specialist_snapshot is not None
    snapshot_value = specialist_snapshot.model_dump(mode="json")
    snapshot_value["user_facts"] = [_user_fact()]
    snapshot_value["active_hypotheses"] = [
        {
            "item_id": "00000000-0000-0000-0000-000000000075",
            "statement": PRIVATE_STATE_SENTINEL,
            "status": "ACTIVE",
            "provenance": {
                "source_type": "AGENT_OUTCOME",
                "source_ref": "00000000-0000-0000-0000-000000000010",
                "input_name": None,
            },
            "evidence_refs": [],
            "created_revision": 3,
            "supersedes": [],
        }
    ]
    specialist_value["context_snapshot"] = snapshot_value
    specialist = Job.model_validate(specialist_value)

    target_bytes = (
        b"API_COMPLETE request_id=req-1\n"
        + PRIVATE_TARGET_SENTINEL.encode("utf-8")
        + b"\n"
    )
    specialist_manager = WorkspaceManager(tmp_path / "specialist-data")
    specialist_workspace = specialist_manager.prepare(
        specialist,
        _aggregate(specialist),
        _ResourceStore(),  # type: ignore[arg-type]
    )
    frozen = specialist_manager.freeze_methods_inputs(
        specialist_workspace,
        request={
            "schema_version": 1,
            "job_id": specialist.job_id,
            "case_id": specialist.case_id,
            "private_preprocess_field": PRIVATE_TARGET_SENTINEL,
        },
        target_logs=(("server", "server", target_bytes),),
        receipt_context={
            "job_id": specialist.job_id,
            "case_id": specialist.case_id,
            "registration_id": skill.registration_id,
            "operation": "target-logs",
            "broker_request_sha256": "a" * 64,
            "broker_audit_sha256": "b" * 64,
        },
    )
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=frozen.target_logs,
        limitations=("Only the frozen target set was evaluated.",),
    )
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)

    review_value = _fixture("job-review.json", Job).model_dump(mode="json")
    review_value.update(catalog.review_bindings(skill_ref).model_dump(mode="json"))
    review_snapshot = snapshot_value.copy()
    review_snapshot.update(
        {
            "candidate_conclusion": None,
            "evidence_refs": [],
            "confirmed_facts": [],
            "active_hypotheses": [],
            "rejected_hypotheses": [],
            "open_questions": [],
            "pending_requirements": [],
        }
    )
    review_value.update(
        {
            "context_snapshot": review_snapshot,
            "attachment_refs": [],
            "artifact_refs": [],
            "evidence_refs": [],
            "previous_outcome_refs": [],
            "base_state_revision": 3,
            "review_target": None,
            "methods_review_target": MethodsReviewTargetV2(
                schema_version=2,
                evaluation_id="00000000-0000-0000-0000-000000000071",
                source_job_id=specialist.job_id,
                graph_ref=graph.graph_ref,
                plan_ref=plan.plan_ref,
                skill_ref=skill_ref,
                reviewed_state_revision=3,
            ).model_dump(mode="json"),
        }
    )
    reviewer = Job.model_validate(review_value)
    return catalog, skill, specialist, reviewer, specialist_workspace, graph, plan, target_bytes


def test_role_workspaces_hard_cut_to_one_graph_plan_and_real_cards(
    tmp_path: Path,
) -> None:
    (
        catalog,
        skill,
        specialist,
        reviewer,
        specialist_workspace,
        graph,
        plan,
        target_bytes,
    ) = _jobs(tmp_path)
    inputs = specialist_workspace.root / "inputs"
    assert (inputs / "target_logs.json").exists()
    assert (inputs / "logparse-receipt.json").exists()
    assert (inputs / "target-logs/server.log").read_bytes() == target_bytes
    assert (
        inputs / f"attachments/{ATTACHMENT_ID}/payload"
    ).read_bytes() == PRIVATE_ATTACHMENT_SENTINEL
    assert (
        inputs / f"artifacts/{ARTIFACT_ID}/payload"
    ).read_bytes() == PRIVATE_ARTIFACT_SENTINEL
    assert (
        inputs / f"evidence/{EVIDENCE_ID}/payload"
    ).read_bytes() == PRIVATE_EVIDENCE_SENTINEL
    assert (inputs / f"outcomes/{PREVIOUS_OUTCOME_ID}/job_outcome.json").exists()

    specialist_receipt = WorkspaceManager.publish_methods_specialist_inputs_v2(
        specialist_workspace,
        specialist,
        evidence_graph=graph,
        evaluation_plan=plan,
    )

    request = parse_canonical_json_bytes(specialist_receipt.request_bytes)
    assert request["job"]["job_id"] == specialist.job_id
    assert request["user_facts"] == [
        {
            "name": "threshold_config",
            "value": USER_FACT_VALUE,
            "source_fact_id": "00000000-0000-0000-0000-000000000074",
        }
    ]
    assert set(request) == {"schema_version", "job", "user_facts"}
    assert parse_canonical_json_bytes(
        (inputs / "method-evidence-graph.json").read_bytes(),
        model_type=MethodEvidenceGraphV2,
    ) == graph
    assert parse_canonical_json_bytes(
        (inputs / "method-evaluation-plan.json").read_bytes(),
        model_type=MethodEvaluationPlanV2,
    ) == plan
    assert not (inputs / "target_logs.json").exists()
    assert not (inputs / "logparse-receipt.json").exists()
    assert not (inputs / "target-logs").exists()
    for category in ("attachments", "evidence", "artifacts", "outcomes"):
        assert not (inputs / category).exists()
    assert specialist_receipt.workspace.manifest.entries == []
    assert specialist_receipt.workspace.manifest.resolved_logparse_plan is None
    assert (inputs / "manifest.json").read_bytes() == (
        specialist_receipt.workspace.manifest_bytes
    )
    assert specialist_receipt.workspace.attachments == ()
    assert specialist_receipt.workspace.evidence == ()
    assert specialist_receipt.workspace.artifacts == ()
    assert specialist_receipt.workspace.previous_outcomes == ()

    reviewer_manager = WorkspaceManager(tmp_path / "reviewer-data")
    reviewer_workspace = reviewer_manager.prepare(
        reviewer,
        _aggregate(reviewer, source_job=specialist),
        _UnusedResourceStore(),  # type: ignore[arg-type]
        methods_evaluation_plan=plan,
    )
    reviewer_request_before = (reviewer_workspace.root / "inputs/request.json").read_bytes()
    with pytest.raises(ValueError, match="forbids legacy"):
        reviewer_manager.freeze_methods_review_inputs(
            reviewer_workspace,
            diagnosis_bytes=b"private specialist result",
            grounding_audit_bytes=b"private grounding audit",
        )
    reviewer_receipt = reviewer_manager.publish_methods_reviewer_inputs_v2(
        reviewer_workspace,
        reviewer,
        evidence_graph=graph,
        evaluation_plan=plan,
    )
    assert reviewer_receipt.request_bytes == reviewer_request_before
    reviewer_request = parse_canonical_json_bytes(reviewer_request_before)
    assert reviewer_request["job"]["job_id"] == reviewer.job_id
    assert reviewer_request["user_facts"] == request["user_facts"]
    review_inputs = reviewer_workspace.root / "inputs"
    assert parse_canonical_json_bytes(
        (review_inputs / "method-evidence-graph.json").read_bytes(),
        model_type=MethodEvidenceGraphV2,
    ) == graph
    assert parse_canonical_json_bytes(
        (review_inputs / "method-evaluation-plan.json").read_bytes(),
        model_type=MethodEvaluationPlanV2,
    ) == plan
    assert not (review_inputs / "method-diagnosis.json").exists()
    assert not (review_inputs / "method-grounding-audit.json").exists()

    specialist_assets = RuntimeAssetResolver(catalog).resolve_job(specialist)
    reviewer_assets = RuntimeAssetResolver(catalog).resolve_job(reviewer)
    specialist_materials = specialist_assets.bind_workspace(
        specialist_receipt.workspace,
        job=specialist,
        methods_evidence_graph=graph,
        methods_evaluation_plan=plan,
    ).materials
    reviewer_materials = reviewer_assets.bind_workspace(
        reviewer_receipt.workspace,
        job=reviewer,
        methods_evidence_graph=graph,
        methods_evaluation_plan=plan,
    ).materials
    specialist_context = ContextBuilder().build(specialist, specialist_materials)
    reviewer_context = ContextBuilder().build(reviewer, reviewer_materials)

    for context in (specialist_context, reviewer_context):
        assert USER_FACT_VALUE in context.body
        assert graph.graph_ref in context.body
        assert plan.plan_ref in context.body
        assert skill.methods.methods[0].id in context.body
        card_text = (skill.package_root / skill.methods.methods[0].reference).read_text(
            encoding="utf-8"
        )
        assert card_text.splitlines()[0] in context.body
        assert PRIVATE_TARGET_SENTINEL not in context.body
        assert PRIVATE_STATE_SENTINEL not in context.body
        assert PRIVATE_CANDIDATE_SENTINEL not in context.body
        assert PRIVATE_ATTACHMENT_SENTINEL.decode() not in context.body
        assert PRIVATE_ARTIFACT_SENTINEL.decode() not in context.body
        assert PRIVATE_EVIDENCE_SENTINEL.decode() not in context.body
        assert "target_logs_path" not in context.body
        assert "inputs/target_logs.json" not in context.body
        assert "inputs/logparse-receipt.json" not in context.body
        assert "candidate_conclusion_id" not in context.body
    assert '"role":"SPECIALIST"' in specialist_context.body
    assert '"role":"REVIEWER"' in reviewer_context.body


def test_review_context_policy_declares_candidate_free_methods_v2() -> None:
    policy = (
        ROOT
        / "src/problem_locator/runtime/assets/context-policies/review/policy.md"
    ).read_text(encoding="utf-8")

    assert "Methods V2 review remains Candidate-free" in policy
    assert "Preserve the candidate," not in policy


def test_single_field_mutations_are_rejected_from_production_baseline(
    tmp_path: Path,
) -> None:
    catalog, _, specialist, _, workspace, graph, plan, _ = _jobs(tmp_path)
    receipt = WorkspaceManager.publish_methods_specialist_inputs_v2(
        workspace,
        specialist,
        evidence_graph=graph,
        evaluation_plan=plan,
    )
    resolved = RuntimeAssetResolver(catalog).resolve_job(specialist)
    materials = resolved.bind_workspace(
        receipt.workspace,
        job=specialist,
        methods_evidence_graph=graph,
        methods_evaluation_plan=plan,
    ).materials

    assert specialist.skill_ref is not None
    wrong_ref = specialist.skill_ref.model_copy(update={"content_hash": "f" * 64})
    wrong_job = specialist.model_copy(update={"skill_ref": wrong_ref})
    with pytest.raises(ValueError, match="do not match"):
        resolved.bind_workspace(
            receipt.workspace,
            job=wrong_job,
            methods_evidence_graph=graph,
            methods_evaluation_plan=plan,
        )

    wrong_plan = plan.model_copy(update={"evidence_graph_ref": "graph-" + "e" * 64})
    with pytest.raises(ValueError, match="do not match"):
        resolved.bind_workspace(
            receipt.workspace,
            job=specialist,
            methods_evidence_graph=graph,
            methods_evaluation_plan=wrong_plan,
        )

    cards = materials.methods_method_cards
    mutated_cards = (
        cards[0].model_copy(update={"content": cards[0].content + "\nchanged\n"}),
        *cards[1:],
    )
    with pytest.raises(ValueError, match="method set"):
        ContextBuilder().build(
            specialist,
            replace(materials, methods_method_cards=mutated_cards),
        )


def test_fresh_restart_publishes_recorded_graph_plan_without_preprocess_inputs(
    tmp_path: Path,
) -> None:
    (
        catalog,
        skill,
        specialist,
        _,
        _,
        source_graph,
        source_plan,
        _,
    ) = _jobs(tmp_path / "source-run")
    records_root = tmp_path / "execution-records"
    records_root.mkdir()
    records = FileExecutionRecordStore(
        records_root,
        StorageCoordinationLock(),
    )
    graph_file_ref = publish_method_evidence_graph_v2(
        records,
        job_id=specialist.job_id,
        graph=source_graph,
    )
    plan_file_ref = publish_method_evaluation_plan_v2(
        records,
        job_id=specialist.job_id,
        plan=source_plan,
    )
    assert graph_file_ref.relative_key.startswith(f"jobs/{specialist.job_id}/")
    assert plan_file_ref.relative_key.startswith(f"jobs/{specialist.job_id}/")
    recorded_graph = read_method_evidence_graph_v2(
        records,
        job_id=specialist.job_id,
    )
    recorded_plan = read_method_evaluation_plan_v2(
        records,
        job_id=specialist.job_id,
    )
    assert recorded_graph is not None
    assert recorded_plan is not None
    assert recorded_graph == source_graph
    assert recorded_plan == source_plan
    assert read_method_evidence_graph_v2(
        records,
        job_id="00000000-0000-0000-0000-000000000099",
    ) is None

    manager = WorkspaceManager(tmp_path / "restart-data")
    fresh = manager.prepare(
        specialist,
        _aggregate(specialist),
        _ResourceStore(),  # type: ignore[arg-type]
    )
    inputs = fresh.root / "inputs"
    for category in ("attachments", "evidence", "artifacts", "outcomes"):
        assert (inputs / category).exists()
    for absent in (
        "request.json",
        "target_logs.json",
        "logparse-receipt.json",
        "target-logs",
    ):
        assert not (inputs / absent).exists()

    receipt = manager.publish_methods_specialist_inputs_v2(
        fresh,
        specialist,
        evidence_graph=recorded_graph,
        evaluation_plan=recorded_plan,
    )

    assert {path.name for path in inputs.iterdir()} == {
        "manifest.json",
        "request.json",
        "method-evidence-graph.json",
        "method-evaluation-plan.json",
    }
    assert receipt.evidence_graph_bytes == canonical_json_bytes(recorded_graph)
    assert receipt.evaluation_plan_bytes == canonical_json_bytes(recorded_plan)
    assert receipt.workspace.manifest.entries == []
    assert receipt.workspace.attachments == ()
    assert receipt.workspace.evidence == ()
    assert receipt.workspace.artifacts == ()
    assert receipt.workspace.previous_outcomes == ()
    request = parse_canonical_json_bytes(receipt.request_bytes)
    assert request["job"]["job_id"] == specialist.job_id
    assert request["user_facts"][0]["value"] == USER_FACT_VALUE

    materials = RuntimeAssetResolver(catalog).resolve_job(specialist).bind_workspace(
        receipt.workspace,
        job=specialist,
        methods_evidence_graph=recorded_graph,
        methods_evaluation_plan=recorded_plan,
    ).materials
    context = ContextBuilder().build(specialist, materials)
    assert recorded_graph.graph_ref in context.body
    assert recorded_plan.plan_ref in context.body
    assert skill.methods.methods[0].id in context.body
    for private in (
        PRIVATE_TARGET_SENTINEL,
        PRIVATE_STATE_SENTINEL,
        PRIVATE_CANDIDATE_SENTINEL,
        PRIVATE_ATTACHMENT_SENTINEL.decode(),
        PRIVATE_ARTIFACT_SENTINEL.decode(),
        PRIVATE_EVIDENCE_SENTINEL.decode(),
    ):
        assert private not in context.body


def test_specialist_publish_rejects_one_missing_preprocess_input(
    tmp_path: Path,
) -> None:
    _, _, specialist, _, workspace, graph, plan, _ = _jobs(tmp_path)
    inputs = workspace.root / "inputs"
    missing = inputs / "logparse-receipt.json"
    inputs.chmod(0o755)
    missing.chmod(0o644)
    missing.unlink()
    inputs.chmod(0o555)

    with pytest.raises(RuntimeExecutionError) as caught:
        WorkspaceManager.publish_methods_specialist_inputs_v2(
            workspace,
            specialist,
            evidence_graph=graph,
            evaluation_plan=plan,
        )

    assert caught.value.failure.stage is ExecutionStage.WORKSPACE_PREPARE
    assert caught.value.failure.code is ErrorCode.WORKSPACE_PREPARE_FAILED
    assert (inputs / "request.json").exists()
    assert (inputs / "target_logs.json").exists()
    assert (inputs / "target-logs/server.log").exists()
    for category in ("attachments", "evidence", "artifacts", "outcomes"):
        assert (inputs / category).exists()
