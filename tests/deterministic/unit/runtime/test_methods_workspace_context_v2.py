from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CaseAggregate,
    Job,
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
from problem_locator.runtime.workspace import WorkspaceManager

from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "tests/fixtures/contracts/positive"
USER_FACT_VALUE = "threshold=37"
PRIVATE_TARGET_SENTINEL = "private-unmatched-target-bytes"
PRIVATE_STATE_SENTINEL = "private-candidate-state-sentinel"


class _UnusedResourceStore:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"empty Methods Workspace must not call {name}")


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
    value["jobs"] = jobs
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
        "created_revision": 2,
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
            "attachment_refs": [],
            "artifact_refs": [],
            "evidence_refs": [],
            "previous_outcome_refs": [],
            "base_state_revision": 2,
        }
    )
    specialist_snapshot = _fixture("job-diagnose.json", Job).context_snapshot
    assert specialist_snapshot is not None
    snapshot_value = specialist_snapshot.model_dump(mode="json")
    snapshot_value["user_facts"] = [_user_fact()]
    snapshot_value["evidence_refs"] = []
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
            "created_revision": 2,
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
        _UnusedResourceStore(),  # type: ignore[arg-type]
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
    graph = scan_method_evidence_v2(skill=skill, target_logs=frozen.target_logs)
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
            "base_state_revision": 2,
            "review_target": None,
            "methods_review_target": MethodsReviewTargetV2(
                schema_version=2,
                evaluation_id="00000000-0000-0000-0000-000000000071",
                source_job_id=specialist.job_id,
                graph_ref=graph.graph_ref,
                plan_ref=plan.plan_ref,
                skill_ref=skill_ref,
                reviewed_state_revision=2,
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
        specialist_workspace,
        job=specialist,
        methods_evidence_graph=graph,
        methods_evaluation_plan=plan,
    ).materials
    reviewer_materials = reviewer_assets.bind_workspace(
        reviewer_workspace,
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
        assert "target_logs_path" not in context.body
        assert "inputs/target_logs.json" not in context.body
        assert "inputs/logparse-receipt.json" not in context.body
        assert "candidate_conclusion_id" not in context.body
    assert '"role":"SPECIALIST"' in specialist_context.body
    assert '"role":"REVIEWER"' in reviewer_context.body


def test_single_field_mutations_are_rejected_from_production_baseline(
    tmp_path: Path,
) -> None:
    catalog, _, specialist, _, workspace, graph, plan, _ = _jobs(tmp_path)
    WorkspaceManager.publish_methods_specialist_inputs_v2(
        workspace,
        specialist,
        evidence_graph=graph,
        evaluation_plan=plan,
    )
    resolved = RuntimeAssetResolver(catalog).resolve_job(specialist)
    materials = resolved.bind_workspace(
        workspace,
        job=specialist,
        methods_evidence_graph=graph,
        methods_evaluation_plan=plan,
    ).materials

    assert specialist.skill_ref is not None
    wrong_ref = specialist.skill_ref.model_copy(update={"content_hash": "f" * 64})
    wrong_job = specialist.model_copy(update={"skill_ref": wrong_ref})
    with pytest.raises(ValueError, match="do not match"):
        resolved.bind_workspace(
            workspace,
            job=wrong_job,
            methods_evidence_graph=graph,
            methods_evaluation_plan=plan,
        )

    wrong_plan = plan.model_copy(update={"evidence_graph_ref": "graph-" + "e" * 64})
    with pytest.raises(ValueError, match="do not match"):
        resolved.bind_workspace(
            workspace,
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
