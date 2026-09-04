from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    ArtifactKind,
    ArtifactProposal,
    AttachmentRequirementConstraints,
    CaseStatus,
    DiagnosisItemDraft,
    DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceProposal,
    EvidenceSourceType,
    InputRequirementConstraints,
    JobOutcome,
    JobStatus,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ReviewPolicy,
    StagedResourceRef,
    SupplementPolicy,
    TreeManifest,
    TreeManifestEntry,
    TriggerType,
    canonical_json_sha256,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    diagnose_job,
    diagnosis_outcome,
    rebuild,
    snapshot_with_active,
    trigger,
)


OUTCOME_ID = "00000000-0000-0000-0000-000000000030"
REQUIREMENT_ID = "00000000-0000-0000-0000-000000000032"
ATTACHMENT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000033"
LOGPARSE_STAGING_ID = "00000000-0000-0000-0000-000000000034"
LOGPARSE_FACT_ID = "00000000-0000-0000-0000-000000000035"
SECOND_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000036"


def _empty_delta(**changes: object) -> DiagnosisStateDelta:
    values: dict[str, object] = {
        "problem_spec_patch": None,
        "add_user_facts": [],
        "proposed_facts": [],
        "add_active_hypotheses": [],
        "update_hypotheses": [],
        "reject_hypotheses": [],
        "add_open_questions": [],
        "resolve_questions": [],
        "add_pending_requirements": [],
        "fulfill_requirements": [],
        "add_evidence_bindings": [],
    }
    values.update(changes)
    return DiagnosisStateDelta.model_validate(values)


def _diagnosis_job_outcome(
    result_type: OutcomeResultType,
    diagnosis: DiagnosisOutcome,
    *,
    proposed_evidence: list[EvidenceProposal] | None = None,
    proposed_artifacts: list[ArtifactProposal] | None = None,
) -> JobOutcome:
    base = diagnosis_outcome()
    return rebuild(
        base,
        outcome_id=OUTCOME_ID,
        result_type=result_type,
        payload=diagnosis,
        proposed_evidence=proposed_evidence or [],
        proposed_artifacts=proposed_artifacts or [],
        produced_at="2026-07-31T00:03:00.000Z",
        decision_audit=(
            None
            if result_type
            in {OutcomeResultType.NEED_INPUT, OutcomeResultType.NEED_ATTACHMENT}
            else base.decision_audit
        ),
    )


def _logparse_proposals(
    source_job_id: str,
    source_attachment_id: str,
) -> tuple[EvidenceProposal, ArtifactProposal]:
    manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="parse_manifest.json",
                size=17,
                sha256="a" * 64,
            )
        ],
    )
    tree_hash = canonical_json_sha256(manifest)
    artifact = ArtifactProposal(
        proposal_key="logparse_run_new",
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="fixed-logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=17,
        sha256=tree_hash,
        staged_resource_ref=StagedResourceRef(
            staging_id=LOGPARSE_STAGING_ID,
            owner_job_id=source_job_id,
            proposal_key="logparse_run_new",
            resource_kind=ResourceKind.DIRECTORY,
            size=17,
            sha256=tree_hash,
            tree_manifest=manifest,
        ),
        metadata={
            "tree_manifest_sha256": tree_hash,
            "logparse_version_ref": {
                "id": "logparse",
                "version": "1.0.0",
                "content_hash": "f" * 64,
            },
            "parse_manifest_relative_path": "parse_manifest.json",
            "source_attachment_id": source_attachment_id,
            "source_attachment_sha256": "b" * 64,
            "parse_parameters": {"product": "payment-service"},
        },
    )
    evidence = EvidenceProposal(
        proposal_key="logparse_evidence_new",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding={
            "existing_source_ref": None,
            "artifact_proposal_key": artifact.proposal_key,
        },
        locator={
            "kind": "LOGPARSE",
            "relative_path": "targets/timeout.log",
            "start_line": 1,
            "end_line": 1,
            "start_time": None,
            "end_time": None,
        },
        summary="The parsed timeout line fixes the pending RPC.",
        content_hash="c" * 64,
        staged_resource_ref=None,
    )
    return evidence, artifact


def test_specialized_candidate_is_accepted_and_published_when_review_is_disabled() -> None:
    source = rebuild(diagnose_job(), review_policy=ReviewPolicy.NONE)
    outcome = diagnosis_outcome()
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.RESOLVED
    assert plan.next_job_spec is None
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.target_status.value == "ACCEPTED"
    assert plan.final_result_target == plan.candidate_mutation.candidate_binding
    assert plan.accepted_artifact_proposal_keys == [
        "user_result",
        "user_result_archive",
    ]
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_need_input_accepts_requirement_and_ends_the_job() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    requirement = PendingRequirement(
        requirement_id=REQUIREMENT_ID,
        kind=RequirementKind.INPUT,
        name="order_id",
        prompt="Provide the order ID.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=64,
            pattern=None,
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
        supplement_policy=SupplementPolicy.MISSING_ONLY,
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.NEED_INPUT,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[requirement.requirement_id],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Wait for order_id.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "WAITING_INPUT"
    assert plan.job_updates[0].target_status is JobStatus.SUCCEEDED
    assert plan.accepted_state_delta.add_pending_requirements == [requirement]
    assert plan.next_job_spec is None
    assert plan.clear_active_job is True
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_need_input_accepts_multiple_inputs_and_attachment_in_one_wait() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    input_requirements = [
        PendingRequirement(
            requirement_id=requirement_id,
            kind=RequirementKind.INPUT,
            name=name,
            prompt=f"Provide {name}.",
            required=True,
            constraints=InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=1,
                max_utf8_bytes=64,
                pattern=None,
                allowed_values=[],
            ),
            status=RequirementStatus.OPEN,
            requested_by_job_id=source.job_id,
            fulfilled_by_refs=[],
            supplement_policy=SupplementPolicy.MISSING_ONLY,
        )
        for requirement_id, name in (
            (REQUIREMENT_ID, "caller_service"),
            (SECOND_REQUIREMENT_ID, "rpc_method"),
        )
    ]
    attachment = PendingRequirement(
        requirement_id=ATTACHMENT_REQUIREMENT_ID,
        kind=RequirementKind.ATTACHMENT,
        name="log_archive",
        prompt="Attach the fixed log archive.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=["application/gzip"],
            min_count=1,
            max_count=1,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
        supplement_policy=SupplementPolicy.MISSING_ONLY,
    )
    requirements = [*input_requirements, attachment]
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.NEED_INPUT,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=requirements),
            requested_input=[
                requirement.requirement_id for requirement in input_requirements
            ],
            requested_attachments=[attachment.requirement_id],
            candidate_conclusion_draft=None,
            recommended_next_step="Collect the missing inputs and log archive.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.WAITING_INPUT
    assert plan.accepted_state_delta.add_pending_requirements == requirements
    assert plan.next_job_spec is None
    assert plan.clear_active_job is True
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_need_attachment_accepts_the_unique_attachment_requirement() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    requirement = PendingRequirement(
        requirement_id=ATTACHMENT_REQUIREMENT_ID,
        kind=RequirementKind.ATTACHMENT,
        name="log_archive",
        prompt="Attach the fixed log archive.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=["application/gzip"],
            min_count=1,
            max_count=1,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
        supplement_policy=SupplementPolicy.MISSING_ONLY,
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.NEED_ATTACHMENT,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[],
            requested_attachments=[requirement.requirement_id],
            candidate_conclusion_draft=None,
            recommended_next_step="Wait for the fixed log archive.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "WAITING_ATTACHMENT"
    assert plan.accepted_state_delta.add_pending_requirements == [requirement]
    assert plan.job_updates[0].target_status is JobStatus.SUCCEEDED
    assert plan.next_job_spec is None
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


@pytest.mark.parametrize(
    ("kind", "result_type"),
    [
        (RequirementKind.INPUT, OutcomeResultType.NEED_INPUT),
        (RequirementKind.ATTACHMENT, OutcomeResultType.NEED_ATTACHMENT),
    ],
)
def test_waiting_outcome_rejects_non_supplementable_requirement(
    kind: RequirementKind,
    result_type: OutcomeResultType,
) -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    is_input = kind is RequirementKind.INPUT
    requirement = PendingRequirement(
        requirement_id=(
            REQUIREMENT_ID if is_input else ATTACHMENT_REQUIREMENT_ID
        ),
        kind=kind,
        name="order_id" if is_input else "log_archive",
        prompt=(
            "Provide the order ID."
            if is_input
            else "Attach the fixed log archive."
        ),
        required=True,
        constraints=(
            InputRequirementConstraints(
                value_type="STRING",
                min_utf8_bytes=1,
                max_utf8_bytes=64,
                pattern=None,
                allowed_values=[],
            )
            if is_input
            else AttachmentRequirementConstraints(
                allowed_content_types=["application/gzip"],
                min_count=1,
                max_count=1,
            )
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
        supplement_policy=SupplementPolicy.NONE,
    )
    outcome = _diagnosis_job_outcome(
        result_type,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[requirement.requirement_id] if is_input else [],
            requested_attachments=(
                [] if is_input else [requirement.requirement_id]
            ),
            candidate_conclusion_draft=None,
            recommended_next_step="Wait for an input the Skill does not supplement.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"
    assert "MISSING_ONLY" in result.message


def test_logparse_evidence_and_run_are_accepted_atomically_before_waiting() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    source_attachment_id = source.attachment_refs[0]
    evidence, artifact = _logparse_proposals(source.job_id, source_attachment_id)
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=evidence.proposal_key,
    )
    requirement = PendingRequirement(
        requirement_id=REQUIREMENT_ID,
        kind=RequirementKind.INPUT,
        name="order_id",
        prompt="Provide the order ID.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=64,
            pattern=None,
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=source.job_id,
        fulfilled_by_refs=[],
        supplement_policy=SupplementPolicy.MISSING_ONLY,
    )
    draft = DiagnosisItemDraft(
        item_id=LOGPARSE_FACT_ID,
        statement="The parsed log identifies a pending ReserveStock call.",
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.AGENT_OUTCOME,
            source_ref=OUTCOME_ID,
            input_name=None,
        ),
        evidence_bindings=[binding],
        supersedes=[],
    )
    diagnosis = DiagnosisOutcome(
        findings=[],
        state_delta=_empty_delta(
            proposed_facts=[draft],
            add_pending_requirements=[requirement],
        ),
        requested_input=[requirement.requirement_id],
        requested_attachments=[],
        candidate_conclusion_draft=None,
        recommended_next_step="Collect order_id and reuse the parsed run.",
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.NEED_INPUT,
        diagnosis,
        proposed_evidence=[evidence],
        proposed_artifacts=[artifact],
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.accepted_evidence_proposal_keys == [evidence.proposal_key]
    assert plan.accepted_artifact_proposal_keys == [artifact.proposal_key]
    assert plan.accepted_state_delta.proposed_facts == [draft]
    assert plan.accepted_state_delta.add_evidence_bindings == [binding]
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
