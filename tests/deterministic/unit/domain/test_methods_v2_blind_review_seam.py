from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from problem_locator.application.formalization import (
    apply_diagnosis_state_delta,
    build_job,
    build_methods_reviewer_outcome_v2,
    build_methods_specialist_handoff_outcome_v2,
)
from problem_locator.application.mutations import apply_transition_plan_to_case
from problem_locator.contracts import (
    AgentJobOutcome,
    ApplicationError,
    CaseSnapshot,
    CaseStatus,
    ContextSnapshot,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    JobOutcome,
    JobType,
    MethodsReviewMethodCardV2,
    MethodsReviewerInputV2,
    MethodsReviewTargetV2,
    ReviewOutcomeTriggerPayload,
    TriggerType,
    VersionedRef,
    WorkspaceInputManifest,
    validate_transition_plan_for_outcome,
    validate_outcome_for_job,
    validate_workspace_manifest_for_job,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.runtime.context_builder import (
    ContextBuilder,
    ContextMaterials,
    build_methods_reviewer_manifest_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_evaluation_v2 import evaluate_method_role_v2
from problem_locator.runtime.methods_skill import (
    MethodCardV1,
    MethodsManifestV1,
    PreprocessingBindingV1,
    RegistrationTemplateV1,
    ResolvedSpecializedSkillV1,
    RuntimeRoleBindingV1,
)

from ._builders import (
    continuation,
    diagnose_job,
    diagnosis_outcome,
    fixture,
    review_job,
    runtime_bindings,
    rebuild,
    snapshot_with_active,
    state_from_job,
    trigger,
)


REVIEW_JOB_ID = "00000000-0000-0000-0000-000000000013"
EVALUATION_ID = "00000000-0000-0000-0000-000000000071"
OTHER_EVALUATION_ID = "00000000-0000-0000-0000-000000000072"
REVIEW_OUTCOME_ID = "00000000-0000-0000-0000-000000000073"
USER_FACT_ID = "00000000-0000-0000-0000-000000000074"
USER_FACT_VALUE = "threshold=37"


def _source_job():
    source = diagnose_job()
    assert source.context_snapshot is not None
    snapshot_value = source.context_snapshot.model_dump(mode="python")
    snapshot_value["user_facts"] = [
        DiagnosisItem(
            item_id=USER_FACT_ID,
            statement=USER_FACT_VALUE,
            status=DiagnosisItemStatus.ACTIVE,
            provenance=DiagnosisProvenance(
                source_type=DiagnosisProvenanceType.USER_INPUT,
                source_ref="00000000-0000-0000-0000-000000000090",
                input_name="threshold_config",
            ),
            evidence_refs=[],
            created_revision=source.base_state_revision,
            supersedes=[],
        )
    ]
    return rebuild(
        source,
        context_snapshot=ContextSnapshot.model_validate(snapshot_value),
    )


def _production_graph_and_plan():
    source = _source_job()
    assert source.skill_ref is not None
    role = RuntimeRoleBindingV1("profile", "tools", "policy", "output")
    method = MethodCardV1(
        id="blind-method",
        title="blind-method",
        reference="references/blind-method.md",
        priority=1,
        evidence_markers=("BLIND_MARKER",),
    )
    skill = ResolvedSpecializedSkillV1(
        registration_root=Path("registration"),
        package_root=Path("package"),
        registration=RegistrationTemplateV1(
            registration_id="blind-review-test",
            version="1.0.0",
            capability="test",
            deployment_scope="PRODUCTION",
            summary="test",
            package_relative_path="package/blind-review-test",
            skill_name="blind-review-test",
            source_wiki_sha256="1" * 64,
            diagnose=role,
            review=role,
            preprocessing=PreprocessingBindingV1(False, None, (), None),
        ),
        methods=MethodsManifestV1(
            skill_name="blind-review-test",
            source_wiki_sha256="1" * 64,
            required_user_inputs=(),
            required_artifacts=(),
            log_derived_fields=(),
            shared_references=(),
            methods=(method,),
        ),
        registration_sha256="2" * 64,
        package_tree_sha256="3" * 64,
        combined_sha256=source.skill_ref.content_hash,
    )
    content = b"BLIND_MARKER request_id=req-1\n"
    graph = scan_method_evidence_v2(
        skill=skill,
        target_logs=(
            FrozenTargetLogV1(
                source_id="server",
                relative_path="logs/server.log",
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            ),
        ),
    )
    return graph, build_method_evaluation_plan_v2(skill=skill, evidence=graph)


def _target() -> tuple[MethodsReviewTargetV2, object, object]:
    source = _source_job()
    graph, plan = _production_graph_and_plan()
    assert source.skill_ref is not None
    return (
        MethodsReviewTargetV2(
            schema_version=2,
            evaluation_id=EVALUATION_ID,
            source_job_id=source.job_id,
            graph_ref=graph.graph_ref,
            plan_ref=plan.plan_ref,
            skill_ref=source.skill_ref,
            reviewed_state_revision=source.base_state_revision,
        ),
        graph,
        plan,
    )


def _specialist_handoff() -> tuple[JobOutcome, object, object]:
    target, graph, plan = _target()
    source = _source_job()
    base = diagnosis_outcome()
    outcome = build_methods_specialist_handoff_outcome_v2(
        source,
        outcome_id=base.outcome_id,
        evaluation_id=target.evaluation_id,
        graph=graph,
        plan=plan,
        produced_at=base.produced_at,
    )
    assert outcome.methods_review_target == target
    return outcome, graph, plan


def _plan_and_review_job():
    source = _source_job()
    snapshot = snapshot_with_active(source)
    outcome, graph, evaluation_plan = _specialist_handoff()
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.REVIEW: runtime_bindings(review_job())},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )
    plan = DomainCoordinator().plan(snapshot, request)
    assert not isinstance(plan, ApplicationError)
    assert plan.next_job_spec is not None
    state = apply_diagnosis_state_delta(
        state_from_job(source),
        plan.accepted_state_delta,
        evidence_ids_by_proposal_key={},
        expected_target_revision=plan.next_job_spec.target_state_revision,
    )
    job = build_job(
        plan.next_job_spec,
        job_id=REVIEW_JOB_ID,
        case_id=source.case_id,
        created_at=outcome.produced_at,
        target_diagnosis_state=state,
        projector=PureContextSnapshotProjector(),
        existing_evidence_ids=set(state.evidence_refs),
        evidence_ids_by_proposal_key={},
        existing_artifact_ids=set(),
        artifact_ids_by_proposal_key={},
        existing_candidate=state.candidate_conclusion,
        candidates_by_proposal_key={},
    )
    return snapshot, outcome, plan, state, job, graph, evaluation_plan


def test_diagnose_creates_candidate_free_blind_review_job() -> None:
    snapshot, outcome, plan, state, job, _, _ = _plan_and_review_job()

    assert validate_transition_plan_for_outcome(plan, outcome) is plan
    assert plan.target_case_status is CaseStatus.REVIEWING
    assert plan.accepted_candidate_proposal_key is None
    assert plan.candidate_mutation is None
    assert plan.accepted_evidence_proposal_keys == []
    assert plan.accepted_artifact_proposal_keys == []
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.review_target_binding is None
    assert plan.next_job_spec.methods_review_target == outcome.methods_review_target
    assert plan.next_job_spec.previous_outcome_refs == []
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.artifact_bindings == []
    assert job.job_id != outcome.job_id
    assert job.context_snapshot is not None
    assert job.context_snapshot.candidate_conclusion is None
    assert job.review_target is None
    assert job.methods_review_target == outcome.methods_review_target
    assert job.previous_outcome_refs == job.evidence_refs == job.artifact_refs == []

    reviewing_case = apply_transition_plan_to_case(
        snapshot.case,
        plan,
        state,
        created_job=job,
        processed_at=outcome.produced_at,
    )
    assert (
        CaseSnapshot(
            case=reviewing_case,
            active_job=job,
            resume_source_job=None,
            replacement_job_ids_by_source={},
        ).case.status
        is CaseStatus.REVIEWING
    )


def test_reviewer_context_receives_only_graph_plan_and_method_cards() -> None:
    _, outcome, _, _, job, graph, plan = _plan_and_review_job()
    assert job.methods_review_target is not None
    manifest = build_methods_reviewer_manifest_v2(
        job,
        method_ids=tuple(item.method_id for item in plan.evaluations),
    )
    validate_workspace_manifest_for_job(manifest, job)
    context = ContextBuilder().build(
        job,
        ContextMaterials(
            profile="reviewer profile",
            tool_bundle="reviewer tools",
            output_contract="reviewer output contract",
            manifest=manifest,
            previous_outcomes=(),
            evidence=(),
            skill="same pinned Skill entry",
            methods_evidence_graph=graph,
            methods_evaluation_plan=plan,
            methods_method_cards=(
                MethodsReviewMethodCardV2(
                    method_id="blind-method",
                    content="BLIND_MARKER means the method must be evaluated.",
                ),
            ),
        ),
    )

    assert outcome.outcome_id not in context.body
    assert outcome.payload is None
    assert "candidate_conclusion_id" not in context.body
    assert graph.graph_ref in context.body
    assert plan.plan_ref in context.body
    assert "blind-method" in context.body
    assert USER_FACT_VALUE in context.body
    assert "threshold_config" in context.body
    assert job.context_snapshot is not None
    assert job.context_snapshot.user_facts == _source_job().context_snapshot.user_facts
    assert manifest.entries == []
    assert manifest.review_subject is None


def test_methods_target_is_server_only_and_never_falls_back_to_candidate() -> None:
    target, _, _ = _target()
    agent_schema = AgentJobOutcome.model_json_schema()["properties"]
    assert "methods_review_target" not in agent_schema
    assert "methods_reviewer_result" not in agent_schema
    agent = fixture(AgentJobOutcome, "agent-job-outcome-diagnosis.json")
    agent_value = agent.model_dump(mode="python")
    agent_value["methods_review_target"] = target.model_dump(mode="python")
    with pytest.raises(ValueError, match="extra"):
        AgentJobOutcome.model_validate(agent_value)

    outcome, _, _ = _specialist_handoff()
    assert outcome.methods_review_target is not None
    without_target_value = outcome.model_dump(mode="python")
    without_target_value.pop("methods_review_target")
    with pytest.raises(ValueError, match="non-failed outcomes require a payload"):
        JobOutcome.model_validate(without_target_value)

    source = _source_job()
    snapshot = snapshot_with_active(source)
    legacy_candidate_outcome = diagnosis_outcome()
    missing_target_request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=legacy_candidate_outcome),
        bindings={JobType.REVIEW: runtime_bindings(review_job())},
        continuation_resources=continuation(
            incoming_outcome_id=legacy_candidate_outcome.outcome_id,
            job=source,
        ),
        occurred_at=legacy_candidate_outcome.produced_at,
    )
    missing_target_result = DomainCoordinator().plan(
        snapshot,
        missing_target_request,
    )
    assert isinstance(missing_target_result, ApplicationError)
    assert "requires a server-created review target" in missing_target_result.message

    mismatched_skill = VersionedRef(
        id=outcome.methods_review_target.skill_ref.id,
        version=outcome.methods_review_target.skill_ref.version,
        content_hash="f" * 64,
    )
    mutated_target = outcome.methods_review_target.model_copy(
        update={"skill_ref": mismatched_skill}
    )
    mutated = outcome.model_copy(update={"methods_review_target": mutated_target})
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=mutated),
        bindings={JobType.REVIEW: runtime_bindings(review_job())},
        continuation_resources=continuation(
            incoming_outcome_id=mutated.outcome_id,
            job=source,
        ),
        occurred_at=mutated.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert "Methods V2" in result.message


def test_server_can_express_reviewer_result_without_candidate_assessment() -> None:
    _, _, _, state, job, _, plan = _plan_and_review_job()
    assert job.methods_review_target is not None
    evaluation = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": "CONFIRMED",
                "reason": f"reviewed {item.method_id}",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    outcome = build_methods_reviewer_outcome_v2(
        job,
        outcome_id=REVIEW_OUTCOME_ID,
        evaluation=evaluation,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert validate_outcome_for_job(job, outcome) is outcome

    snapshot = snapshot_with_active(
        job,
        status=CaseStatus.REVIEWING,
        state=state,
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert "consensus terminal transition is not integrated" in result.message
    assert outcome.payload is None
    assert outcome.methods_reviewer_result.target == job.methods_review_target


def test_single_field_mutations_break_target_manifest_and_method_set() -> None:
    outcome, _, _ = _specialist_handoff()
    assert outcome.methods_review_target is not None
    wrong_source = outcome.methods_review_target.model_copy(
        update={"source_job_id": "00000000-0000-0000-0000-000000000099"}
    )
    outcome_value = outcome.model_dump(mode="python")
    outcome_value["methods_review_target"] = wrong_source
    with pytest.raises(ValueError, match="matching completed DIAGNOSE"):
        JobOutcome.model_validate(outcome_value)

    _, _, _, _, job, graph, plan = _plan_and_review_job()
    assert job.methods_review_target is not None
    wrong_target = job.methods_review_target.model_copy(
        update={"evaluation_id": OTHER_EVALUATION_ID}
    )
    bad_manifest = WorkspaceInputManifest(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=JobType.REVIEW,
        logparse_tool_ref=None,
        logparse_product=None,
        entries=[],
        review_subject=None,
        methods_reviewer_input=MethodsReviewerInputV2(
            schema_version=2,
            review_job_id=job.job_id,
            case_id=job.case_id,
            target=wrong_target,
            method_ids=("blind-method",),
        ),
    )
    with pytest.raises(ValueError, match="must match"):
        validate_workspace_manifest_for_job(bad_manifest, job)

    valid_manifest = build_methods_reviewer_manifest_v2(
        job,
        method_ids=("blind-method",),
    )
    with pytest.raises(ValueError, match="method set"):
        ContextBuilder().build(
            job,
            ContextMaterials(
                profile="reviewer profile",
                tool_bundle="reviewer tools",
                output_contract="reviewer output contract",
                manifest=valid_manifest,
                skill="same pinned Skill entry",
                methods_evidence_graph=graph,
                methods_evaluation_plan=plan,
                methods_method_cards=(
                    MethodsReviewMethodCardV2(
                        method_id="wrong-method",
                        content="One mutated card ID.",
                    ),
                ),
            ),
        )
