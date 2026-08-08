from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    ArtifactKind,
    ArtifactProposal,
    AttachmentRequirementConstraints,
    CandidateMutationAction,
    CandidateStatus,
    DiagnosisItem,
    DiagnosisItemChange,
    DiagnosisItemDraft,
    DiagnosisItemStatus,
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
    JobType,
    OutcomeResultType,
    PendingRequirement,
    ProblemSpecPatch,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    StagedResourceRef,
    SupplementPolicy,
    TreeManifest,
    TreeManifestEntry,
    TriggerType,
    canonical_json_sha256,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector

from ._builders import (
    continuation,
    diagnose_job,
    diagnosis_outcome,
    rebuild,
    review_job,
    route_job,
    runtime_bindings,
    snapshot_with_active,
    state_from_job,
    trigger,
)


OUTCOME_ID = "00000000-0000-0000-0000-000000000030"
QUESTION_ID = "00000000-0000-0000-0000-000000000031"
REQUIREMENT_ID = "00000000-0000-0000-0000-000000000032"
ATTACHMENT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000033"
LOGPARSE_STAGING_ID = "00000000-0000-0000-0000-000000000034"
LOGPARSE_FACT_ID = "00000000-0000-0000-0000-000000000035"


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


def test_completed_semantic_progress_continues_in_a_new_diagnosis_job() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    question = DiagnosisItemDraft(
        item_id=QUESTION_ID,
        statement="Which retry budget was active?",
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.AGENT_OUTCOME,
            source_ref=OUTCOME_ID,
            input_name=None,
        ),
        evidence_bindings=[],
        supersedes=[],
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_open_questions=[question]),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Investigate the retry budget.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.accepted_state_delta.add_open_questions == [question]
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.DIAGNOSE
    assert plan.next_job_spec.target_state_revision == snapshot.case.diagnosis_state.revision + 1
    assert plan.next_job_spec.previous_outcome_refs[0] == outcome.outcome_id
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_completed_without_candidate_or_semantic_progress_is_rejected() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Repeat the same work.",
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
    assert result.retryable is False


def test_noop_hypothesis_update_cannot_fake_semantic_progress() -> None:
    source = diagnose_job()
    state = state_from_job(source)
    hypothesis = DiagnosisItem(
        item_id="00000000-0000-0000-0000-000000000036",
        statement="The retry budget is exhausted.",
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.AGENT_OUTCOME,
            source_ref="00000000-0000-0000-0000-000000000020",
            input_name=None,
        ),
        evidence_refs=[state.evidence_refs[0]],
        created_revision=state.revision,
        supersedes=[],
    )
    state = rebuild(state, active_hypotheses=[hypothesis])
    source = rebuild(
        source,
        context_snapshot=PureContextSnapshotProjector().project(state),
    )
    snapshot = snapshot_with_active(source, state=state)
    noop = DiagnosisItemChange(
        item_id=hypothesis.item_id,
        statement=None,
        reason="No projected field changed.",
        evidence_bindings=[],
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(update_hypotheses=[noop]),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Repeat the same work.",
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


def test_same_target_problem_patch_is_semantic_progress() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    patch = ProblemSpecPatch(
        actual_behavior="The payment request now times out after five seconds."
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(problem_spec_patch=patch),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Re-evaluate the timeout evidence.",
        ),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.accepted_state_delta.problem_spec_patch == patch
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.target_state_revision == (
        snapshot.case.diagnosis_state.revision + 1
    )


@pytest.mark.parametrize(
    ("patch", "expected_code"),
    [
        (
            ProblemSpecPatch(actual_behavior="The payment request times out."),
            "VALIDATION_ERROR",
        ),
        (
            ProblemSpecPatch(statement="Diagnose a different checkout failure."),
            "NEW_CASE_REQUIRED",
        ),
    ],
)
def test_noop_or_stable_target_patch_produces_no_transition_plan(
    patch: ProblemSpecPatch,
    expected_code: str,
) -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(problem_spec_patch=patch),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Do not create a looping Job.",
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
    assert result.code.value == expected_code
    assert result.retryable is False


def test_reroute_clears_skill_and_only_carries_the_incoming_outcome() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.REROUTE,
        DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Select a skill for the newly identified subsystem.",
        ),
    )
    route = rebuild(route_job(), case_id=source.case_id)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.ROUTE: runtime_bindings(route)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.selected_skill_update is not None
    assert plan.selected_skill_update.action.value == "CLEAR"
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.ROUTE
    assert plan.next_job_spec.evidence_bindings == []
    assert plan.next_job_spec.attachment_refs == []
    assert plan.next_job_spec.artifact_bindings == []
    assert plan.next_job_spec.previous_outcome_refs == [outcome.outcome_id]
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_same_round_logparse_continuation_never_infers_attachment_metadata() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    evidence, artifact = _logparse_proposals(
        source.job_id,
        source.attachment_refs[0],
    )
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=evidence.proposal_key,
    )
    question = DiagnosisItemDraft(
        item_id=LOGPARSE_FACT_ID,
        statement="Which retry budget applied to this parsed request?",
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
        state_delta=_empty_delta(add_open_questions=[question]),
        requested_input=[],
        requested_attachments=[],
        candidate_conclusion_draft=None,
        recommended_next_step="Investigate the retry budget.",
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        diagnosis,
        proposed_evidence=[evidence],
        proposed_artifacts=[artifact],
    )
    resources = continuation(incoming_outcome_id=outcome.outcome_id)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=resources,
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.attachment_refs == resources.attachment_refs == []
    assert [
        item.accepted_proposal_key for item in plan.next_job_spec.evidence_bindings
    ] == [evidence.proposal_key]
    assert [
        item.accepted_proposal_key for item in plan.next_job_spec.artifact_bindings
    ] == [artifact.proposal_key]
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_shared_logparse_run_cannot_leave_a_bound_evidence_unaccepted() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    evidence, artifact = _logparse_proposals(
        source.job_id,
        source.attachment_refs[0],
    )
    unaccepted_evidence = rebuild(
        evidence,
        proposal_key="logparse_evidence_unaccepted",
    )
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=evidence.proposal_key,
    )
    question = DiagnosisItemDraft(
        item_id=LOGPARSE_FACT_ID,
        statement="Which retry budget applied to this parsed request?",
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
        state_delta=_empty_delta(add_open_questions=[question]),
        requested_input=[],
        requested_attachments=[],
        candidate_conclusion_draft=None,
        recommended_next_step="Investigate the retry budget.",
    )
    outcome = _diagnosis_job_outcome(
        OutcomeResultType.COMPLETED,
        diagnosis,
        proposed_evidence=[evidence, unaccepted_evidence],
        proposed_artifacts=[artifact],
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.DIAGNOSE: runtime_bindings(source)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"
    assert "LOGPARSE_RUN" in result.message


def test_candidate_and_user_result_are_accepted_before_review() -> None:
    source = diagnose_job()
    outcome = diagnosis_outcome()
    snapshot = snapshot_with_active(source)
    reviewer = review_job()
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.REVIEW: runtime_bindings(reviewer)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    candidate = outcome.payload.candidate_conclusion_draft
    assert candidate is not None
    assert plan.target_case_status.value == "REVIEWING"
    assert plan.accepted_candidate_proposal_key == candidate.proposal_key
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.action is CandidateMutationAction.INSTALL
    assert plan.candidate_mutation.target_status is CandidateStatus.REVIEWING
    assert len(plan.accepted_artifact_proposal_keys) == 1
    assert plan.next_job_spec is not None
    assert plan.next_job_spec.job_type is JobType.REVIEW
    assert plan.next_job_spec.previous_outcome_refs == [outcome.outcome_id]
    assert plan.next_job_spec.review_target_binding is not None
    assert (
        plan.next_job_spec.review_target_binding.accepted_candidate_proposal_key
        == candidate.proposal_key
    )
    assert validate_transition_plan_for_outcome(plan, outcome) is plan


def test_new_evidence_candidate_is_fixed_for_review_but_user_result_is_not_input() -> None:
    source = diagnose_job()
    snapshot = snapshot_with_active(source)
    base = diagnosis_outcome()
    diagnosis = base.payload
    assert isinstance(diagnosis, DiagnosisOutcome)
    candidate = diagnosis.candidate_conclusion_draft
    assert candidate is not None
    proposal = EvidenceProposal(
        proposal_key="candidate_evidence",
        source_type=EvidenceSourceType.PREVIOUS_OUTCOME,
        source_binding={
            "existing_source_ref": source.previous_outcome_refs[0],
            "artifact_proposal_key": None,
        },
        locator={
            "kind": "PREVIOUS_OUTCOME",
            "json_pointer": "/payload/findings/0",
        },
        summary="The fixed prior finding supports the candidate.",
        content_hash="e" * 64,
        staged_resource_ref=None,
    )
    new_binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=proposal.proposal_key,
    )
    existing_binding = candidate.supporting_evidence_bindings[0]
    mapping = rebuild(
        candidate.completion_criteria_mapping[0],
        evidence_bindings=[new_binding],
    )
    candidate = rebuild(
        candidate,
        supporting_evidence_bindings=[existing_binding, new_binding],
        completion_criteria_mapping=[mapping],
    )
    diagnosis = rebuild(diagnosis, candidate_conclusion_draft=candidate)
    outcome = rebuild(
        base,
        payload=diagnosis,
        proposed_evidence=[proposal],
    )
    reviewer = review_job()
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        bindings={JobType.REVIEW: runtime_bindings(reviewer)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.accepted_evidence_proposal_keys == [proposal.proposal_key]
    assert plan.accepted_state_delta.add_evidence_bindings == [new_binding]
    assert plan.next_job_spec is not None
    assert [
        binding.existing_resource_id or f"proposal:{binding.accepted_proposal_key}"
        for binding in plan.next_job_spec.evidence_bindings
    ] == [snapshot.case.diagnosis_state.evidence_refs[0], "proposal:candidate_evidence"]
    assert all(
        binding.accepted_proposal_key != "user_result"
        for binding in plan.next_job_spec.artifact_bindings
    )
    assert validate_transition_plan_for_outcome(plan, outcome) is plan
