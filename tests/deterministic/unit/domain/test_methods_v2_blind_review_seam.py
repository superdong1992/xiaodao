from __future__ import annotations

import hashlib

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
    AgentJobOutcomeDraftV2,
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
    MethodsReviewerInputV2,
    ReviewOutcomeTriggerPayload,
    TriggerType,
    VersionedRef,
    WorkspaceInputManifest,
    validate_transition_plan_for_outcome,
    validate_outcome_for_job,
    validate_workspace_manifest_for_job,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from problem_locator.domain.methods_state_v2 import (
    accept_specialist_evaluation_v2,
    finalize_reviewer_consensus_v2,
    start_method_state_v2,
)
from problem_locator.runtime.context_builder import (
    ContextBuilder,
    ContextMaterials,
    build_methods_review_method_cards_v2,
    build_methods_reviewer_manifest_v2,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_evaluation_v2 import (
    evaluate_method_role_v2,
    resolve_method_consensus_v2,
)
from problem_locator.runtime.methods_outcome_v2 import (
    build_method_terminal_result_v2,
)
from problem_locator.runtime.methods_skill import load_specialized_skill_registration
from tests.deterministic.unit.runtime.test_methods_skill import _write_registration

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


def _source_job(skill):
    source = diagnose_job()
    assert source.context_snapshot is not None
    skill_ref = VersionedRef(
        id=skill.registration_id,
        version=skill.registration.version,
        content_hash=skill.combined_sha256,
    )
    snapshot_value = source.context_snapshot.model_dump(mode="python")
    snapshot_value["evidence_refs"] = []
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
        skill_ref=skill_ref,
        evidence_refs=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_refs=[],
    )


def _flow_inputs(tmp_path):
    skill = load_specialized_skill_registration(
        _write_registration(tmp_path / "skills")
    )
    source = _source_job(skill)
    content = (
        b"API_COMPLETE request_id=req-1\n"
        b"UNRELATED_POSITIVE request_id=req-2\n"
    )
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
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    return source, skill, graph, plan


def _review_bindings(source):
    assert source.skill_ref is not None
    return runtime_bindings(
        rebuild(
            review_job(),
            skill_ref=source.skill_ref,
        )
    )


def _specialist_handoff(inputs) -> tuple[JobOutcome, object, object]:
    source, _, graph, plan = inputs
    base = diagnosis_outcome()
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": "CONFIRMED",
                "supporting_event_refs": list(item.evidence_event_refs),
                "reason": f"private specialist handoff reason {item.method_id}",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    pending_state = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=source.case_id,
            source_job_id=source.job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    outcome = build_methods_specialist_handoff_outcome_v2(
        source,
        outcome_id=base.outcome_id,
        pending_state=pending_state,
        graph=graph,
        plan=plan,
        produced_at=base.produced_at,
    )
    assert outcome.methods_review_target is not None
    return outcome, graph, plan


def _plan_and_review_job(inputs):
    source, _, _, _ = inputs
    snapshot = snapshot_with_active(source)
    outcome, graph, evaluation_plan = _specialist_handoff(inputs)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.REVIEW: _review_bindings(source)},
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


def test_diagnose_creates_candidate_free_blind_review_job(tmp_path) -> None:
    inputs = _flow_inputs(tmp_path)
    snapshot, outcome, plan, state, job, _, _ = _plan_and_review_job(inputs)

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


def test_reviewer_context_receives_compact_evaluation_input_and_method_cards(
    tmp_path,
) -> None:
    inputs = _flow_inputs(tmp_path)
    source, skill, _, _ = inputs
    _, outcome, _, _, job, graph, plan = _plan_and_review_job(inputs)
    assert job.methods_review_target is not None
    cards = build_methods_review_method_cards_v2(
        skill=skill,
        target=job.methods_review_target,
        plan=plan,
    )
    manifest = build_methods_reviewer_manifest_v2(
        job,
        method_ids=tuple(item.method_id for item in plan.evaluations),
    )
    validate_workspace_manifest_for_job(manifest, job)
    combined_skill = (
        (skill.package_root / "SKILL.md").read_text(encoding="utf-8").rstrip()
        + "\n\n"
        + "\n\n".join(item.content.rstrip() for item in cards)
        + "\n"
    )
    context = ContextBuilder().build(
        job,
        ContextMaterials(
            profile="reviewer profile",
            tool_bundle="reviewer tools",
            output_contract="reviewer output contract",
            manifest=manifest,
            previous_outcomes=(),
            evidence=(),
            skill=combined_skill,
            methods_evidence_graph=graph,
            methods_evaluation_plan=plan,
            methods_skill=skill,
            methods_method_cards=cards,
        ),
    )

    assert outcome.outcome_id not in context.body
    assert outcome.payload is None
    assert "candidate_conclusion_id" not in context.body
    assert graph.graph_ref in context.body
    assert plan.plan_ref in context.body
    assert '"evaluation_input"' in context.body
    assert '"observations"' in context.body
    assert '"markers"' in context.body
    assert '"evaluations"' in context.body
    assert '"events"' in context.body
    assert all(item.method_id in context.body for item in cards)
    assert all(context.body.count(item.content.splitlines()[0]) == 1 for item in cards)
    assert USER_FACT_VALUE not in context.body
    assert "threshold_config" not in context.body
    assert job.context_snapshot is not None
    assert job.context_snapshot.user_facts == source.context_snapshot.user_facts
    assert manifest.entries == []
    assert manifest.review_subject is None


def test_methods_target_is_server_only_and_never_falls_back_to_candidate(
    tmp_path,
) -> None:
    inputs = _flow_inputs(tmp_path)
    source, _, _, _ = inputs
    outcome, _, _ = _specialist_handoff(inputs)
    assert outcome.methods_review_target is not None
    target = outcome.methods_review_target
    agent_schema = AgentJobOutcome.model_json_schema()["properties"]
    assert "methods_review_target" not in agent_schema
    assert "methods_reviewer_result" not in agent_schema
    assert "methods_terminal_projection" not in agent_schema
    assert (
        "methods_terminal_projection"
        not in AgentJobOutcomeDraftV2.model_json_schema()["properties"]
    )
    agent = fixture(AgentJobOutcome, "agent-job-outcome-diagnosis.json")
    agent_value = agent.model_dump(mode="python")
    agent_value["methods_review_target"] = target.model_dump(mode="python")
    with pytest.raises(ValueError, match="extra"):
        AgentJobOutcome.model_validate(agent_value)
    agent_value = agent.model_dump(mode="python")
    agent_value["methods_terminal_projection"] = {
        "schema_version": 2,
        "result_ref": "result-" + "0" * 64,
    }
    with pytest.raises(ValueError, match="extra"):
        AgentJobOutcome.model_validate(agent_value)

    without_target_value = outcome.model_dump(mode="python")
    without_target_value.pop("methods_review_target")
    with pytest.raises(ValueError, match="non-failed outcomes require a payload"):
        JobOutcome.model_validate(without_target_value)

    snapshot = snapshot_with_active(source)
    legacy_candidate_outcome = diagnosis_outcome()
    missing_target_request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=legacy_candidate_outcome),
        bindings={JobType.REVIEW: _review_bindings(source)},
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
        bindings={JobType.REVIEW: _review_bindings(source)},
        continuation_resources=continuation(
            incoming_outcome_id=mutated.outcome_id,
            job=source,
        ),
        occurred_at=mutated.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert "Methods V2" in result.message


def test_server_can_express_reviewer_result_without_candidate_assessment(
    tmp_path,
) -> None:
    inputs = _flow_inputs(tmp_path)
    _, _, _, state, job, graph, plan = _plan_and_review_job(inputs)
    assert job.methods_review_target is not None
    specialist = evaluate_method_role_v2(
        role="SPECIALIST",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": "CONFIRMED",
                "supporting_event_refs": list(item.evidence_event_refs),
                "reason": f"private specialist reason {item.method_id}",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    evaluation = evaluate_method_role_v2(
        role="REVIEWER",
        plan=plan,
        response=[
            {
                "evaluation_ref": item.evaluation_ref,
                "verdict": "CONFIRMED",
                "supporting_event_refs": list(item.evidence_event_refs),
                "reason": f"private reviewer reason {item.method_id}",
            }
            for item in plan.evaluations
        ],
        attempt="PRIMARY",
    )
    pending = accept_specialist_evaluation_v2(
        state=start_method_state_v2(
            case_id=inputs[0].case_id,
            source_job_id=inputs[0].job_id,
            evaluation_id=EVALUATION_ID,
            plan=plan,
        ),
        evaluation=specialist,
    )
    consensus = resolve_method_consensus_v2(
        plan=plan,
        first=specialist,
        second=evaluation,
    )
    terminal_state = finalize_reviewer_consensus_v2(
        state=pending,
        plan=plan,
        reviewer_evaluation=evaluation,
        consensus=consensus,
    )
    terminal_result = build_method_terminal_result_v2(
        state=terminal_state,
        plan=plan,
        evidence=graph,
        terminal_job_id=job.job_id,
        limitations=("server limitation",),
        reasons=(),
    )
    outcome = build_methods_reviewer_outcome_v2(
        job,
        outcome_id=REVIEW_OUTCOME_ID,
        terminal_state=terminal_state,
        terminal_result=terminal_result,
        plan=plan,
        evidence=graph,
        produced_at="2026-07-31T00:03:30.000Z",
    )
    assert outcome.methods_terminal_projection is not None
    terminal_projection = outcome.methods_terminal_projection
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

    assert not isinstance(result, ApplicationError)
    assert result.target_case_status is CaseStatus.RESOLVED
    assert result.methods_terminal_projection == terminal_projection
    assert result.candidate_mutation is None
    assert result.final_result_target is None
    assert result.next_job_spec is None
    assert outcome.payload is None
    assert outcome.methods_terminal_projection == terminal_projection
    assert outcome.methods_terminal_projection.reasons == ()
    serialized = outcome.model_dump_json()
    assert "private specialist reason" not in serialized
    assert "private reviewer reason" in serialized
    assert "private reviewer reason" not in terminal_projection.model_dump_json()
    assert outcome.methods_reviewer_result.target == job.methods_review_target


def test_single_field_mutations_break_target_manifest_and_method_set(tmp_path) -> None:
    inputs = _flow_inputs(tmp_path)
    _, skill, _, _ = inputs
    outcome, _, _ = _specialist_handoff(inputs)
    assert outcome.methods_review_target is not None
    wrong_source = outcome.methods_review_target.model_copy(
        update={"source_job_id": "00000000-0000-0000-0000-000000000099"}
    )
    outcome_value = outcome.model_dump(mode="python")
    outcome_value["methods_review_target"] = wrong_source
    with pytest.raises(ValueError, match="matching completed DIAGNOSE"):
        JobOutcome.model_validate(outcome_value)

    _, _, _, _, job, graph, plan = _plan_and_review_job(inputs)
    assert job.methods_review_target is not None
    planned_method_ids = tuple(item.method_id for item in plan.evaluations)
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
            method_ids=planned_method_ids,
        ),
    )
    with pytest.raises(ValueError, match="must match"):
        validate_workspace_manifest_for_job(bad_manifest, job)

    valid_manifest = build_methods_reviewer_manifest_v2(
        job,
        method_ids=planned_method_ids,
    )
    wrong_digest_target = job.methods_review_target.model_copy(
        update={
            "skill_ref": job.methods_review_target.skill_ref.model_copy(
                update={"content_hash": "e" * 64}
            )
        }
    )
    with pytest.raises(ValueError, match="does not match"):
        build_methods_review_method_cards_v2(
            skill=skill,
            target=wrong_digest_target,
            plan=plan,
        )

    wrong_method_item = plan.evaluations[0].model_copy(
        update={"method_id": "missing-method"}
    )
    wrong_method_plan = plan.model_copy(
        update={"evaluations": (wrong_method_item, *plan.evaluations[1:])}
    )
    with pytest.raises(ValueError, match="absent from its Skill"):
        build_methods_review_method_cards_v2(
            skill=skill,
            target=job.methods_review_target,
            plan=wrong_method_plan,
        )

    cards = build_methods_review_method_cards_v2(
        skill=skill,
        target=job.methods_review_target,
        plan=plan,
    )
    mutated_cards = (
        cards[0].model_copy(update={"content": cards[0].content + "\nmutated\n"}),
        *cards[1:],
    )
    with pytest.raises(ValueError, match="method set"):
        ContextBuilder().build(
            job,
            ContextMaterials(
                profile="reviewer profile",
                tool_bundle="reviewer tools",
                output_contract="reviewer output contract",
                manifest=valid_manifest,
                skill=(skill.package_root / "SKILL.md").read_text(encoding="utf-8"),
                methods_evidence_graph=graph,
                methods_evaluation_plan=plan,
                methods_skill=skill,
                methods_method_cards=mutated_cards,
            ),
        )
