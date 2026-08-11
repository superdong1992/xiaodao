from __future__ import annotations

import pytest

from problem_locator.application.formalization import (
    apply_candidate_mutation,
    apply_case_failure_update,
    apply_diagnosis_state_delta,
    apply_selected_skill_update,
    build_job,
    formalize_accepted_artifacts,
    formalize_accepted_candidate,
    formalize_accepted_evidence,
    resolve_final_result,
)
from problem_locator.contracts.enums import (
    ArtifactKind,
    CandidateMutationAction,
    CandidateStatus,
    DiagnosisItemStatus,
    DiagnosisProvenanceType,
    EvidenceSourceType,
    FieldUpdateAction,
    JobType,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    ResourceType,
)
from problem_locator.contracts.limits import default_resource_limits
from problem_locator.contracts.models import (
    Artifact,
    ArtifactProposal,
    CandidateConclusionDraft,
    CandidateMutation,
    CaseFailure,
    CaseFailureUpdate,
    CompletionCriterionDraftMapping,
    ContextSnapshot,
    DiagnosticExportMetadata,
    DiagnosisItem,
    DiagnosisItemChange,
    DiagnosisItemDraft,
    DiagnosisProvenance,
    DiagnosisState,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceProposal,
    EvidenceSourceBinding,
    InputRequirementConstraints,
    JobSpec,
    LogparseEvidenceLocator,
    LogparseParseParameters,
    LogparseRunMetadata,
    PendingRequirement,
    PlannedResourceBinding,
    PlannedResourceTarget,
    ProblemSpec,
    ProblemSpecPatch,
    RequirementFulfillment,
    ResourceRef,
    ReviewTargetBinding,
    SelectedSkillUpdate,
    StagedResourceRef,
    ToolOutputEvidenceLocator,
    VersionedRef,
)
from problem_locator.contracts.serialization import canonical_json_sha256


def _id(value: int) -> str:
    return f"00000000-0000-0000-0000-{value:012d}"


CASE_ID = _id(1)
JOB_ID = _id(11)
OUTCOME_ID = _id(21)
EVIDENCE_0 = _id(40)
EVIDENCE_1 = _id(41)
EVIDENCE_2 = _id(42)
ARTIFACT_0 = _id(50)
ARTIFACT_1 = _id(51)
ARTIFACT_2 = _id(52)
CANDIDATE_ID = _id(80)
OCCURRED_AT = "2026-07-31T00:01:30.000Z"


def _ref(name: str, marker: str) -> VersionedRef:
    return VersionedRef(id=name, version="1.0.0", content_hash=marker * 64)


def _provenance() -> DiagnosisProvenance:
    return DiagnosisProvenance(
        source_type=DiagnosisProvenanceType.AGENT_OUTCOME,
        source_ref=OUTCOME_ID,
        input_name=None,
    )


def _item(item_id: int, statement: str) -> DiagnosisItem:
    return DiagnosisItem(
        item_id=_id(item_id),
        statement=statement,
        status=DiagnosisItemStatus.ACTIVE,
        provenance=_provenance(),
        evidence_refs=[EVIDENCE_0],
        created_revision=1,
        supersedes=[],
    )


def _requirement(requirement_id: int, name: str) -> PendingRequirement:
    return PendingRequirement(
        requirement_id=_id(requirement_id),
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
        requested_by_job_id=JOB_ID,
        fulfilled_by_refs=[],
    )


def _state() -> DiagnosisState:
    return DiagnosisState(
        revision=1,
        problem_spec=ProblemSpec(
            revision=1,
            statement="RPC timeout",
            expected_behavior="The request succeeds.",
            actual_behavior="The request times out.",
            scope="payment to inventory",
            goals=["Locate the cause."],
            non_goals=[],
            constraints=[],
            completion_criteria=["The cause is evidenced."],
        ),
        user_facts=[],
        confirmed_facts=[],
        active_hypotheses=[_item(300, "First hypothesis"), _item(301, "Second hypothesis")],
        rejected_hypotheses=[],
        open_questions=[_item(400, "Which request failed?")],
        pending_requirements=[_requirement(500, "order_id")],
        evidence_refs=[EVIDENCE_0],
        candidate_conclusion=None,
    )


def _draft(item_id: int, statement: str, *bindings: EvidenceBinding) -> DiagnosisItemDraft:
    return DiagnosisItemDraft(
        item_id=_id(item_id),
        statement=statement,
        provenance=_provenance(),
        evidence_bindings=list(bindings),
        supersedes=[],
    )


def _candidate_draft(*, existing_id: str | None = None) -> CandidateConclusionDraft:
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key="evidence-one",
    )
    return CandidateConclusionDraft(
        proposal_key="candidate",
        existing_conclusion_id=existing_id,
        resolution_status="COMPLETE",
        terminal_path_id="complete",
        statement="The inventory RPC exceeded its deadline.",
        causal_factors=[
            {
                "factor_id": "primary_cause",
                "role": "CAUSE",
                "statement": "The inventory RPC exceeded its deadline.",
                "evidence_bindings": [binding],
                "required_rule_ids": ["causal_chain"],
            }
        ],
        candidate_factors=[],
        excluded_factors=[],
        supporting_evidence_bindings=[binding],
        completion_criteria_mapping=[
            CompletionCriterionDraftMapping(
                criterion_index=0,
                criterion="The cause is evidenced.",
                status="SATISFIED",
                evidence_bindings=[binding],
                explanation="The timeout is present in the fixed evidence.",
            )
        ],
    )


def test_formalizes_published_artifact_and_logparse_evidence_at_stable_time() -> None:
    sha256 = "a" * 64
    staged = StagedResourceRef(
        staging_id=_id(60),
        owner_job_id=JOB_ID,
        proposal_key="diagnostic-export",
        resource_kind=ResourceKind.FILE,
        size=7,
        sha256=sha256,
        tree_manifest=None,
    )
    artifact_proposal = ArtifactProposal(
        proposal_key="diagnostic-export",
        artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
        name="trace.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        size=7,
        sha256=sha256,
        staged_resource_ref=staged,
        metadata=DiagnosticExportMetadata(
            schema_version=1,
            format_id="trace-v1",
            description="Validated trace export.",
        ),
    )
    target = PlannedResourceTarget(
        case_id=CASE_ID,
        resource_type=ResourceType.ARTIFACT,
        resource_id=ARTIFACT_1,
        resource_kind=ResourceKind.FILE,
        size=7,
        sha256=sha256,
        final_storage_key=(
            f"resources/cases/{CASE_ID}/artifacts/{ARTIFACT_1}/payload"
        ),
    )
    resource = ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=target.final_storage_key,
        size=7,
        sha256=sha256,
    )
    artifacts = formalize_accepted_artifacts(
        [artifact_proposal],
        [artifact_proposal.proposal_key],
        case_id=CASE_ID,
        created_by_job_id=JOB_ID,
        artifact_ids_by_proposal_key={artifact_proposal.proposal_key: ARTIFACT_1},
        planned_targets_by_proposal_key={artifact_proposal.proposal_key: target},
        published_resources_by_proposal_key={artifact_proposal.proposal_key: resource},
        occurred_at=OCCURRED_AT,
    )
    assert artifacts[artifact_proposal.proposal_key].storage_key == resource.storage_key
    assert artifacts[artifact_proposal.proposal_key].created_at == OCCURRED_AT

    wrong_key_resource = resource.model_copy(
        update={
            "storage_key": (
                f"resources/cases/{CASE_ID}/artifacts/{ARTIFACT_2}/payload"
            )
        }
    )
    with pytest.raises(ValueError, match="does not match proposal"):
        formalize_accepted_artifacts(
            [artifact_proposal],
            [artifact_proposal.proposal_key],
            case_id=CASE_ID,
            created_by_job_id=JOB_ID,
            artifact_ids_by_proposal_key={artifact_proposal.proposal_key: ARTIFACT_1},
            planned_targets_by_proposal_key={artifact_proposal.proposal_key: target},
            published_resources_by_proposal_key={
                artifact_proposal.proposal_key: wrong_key_resource
            },
            occurred_at=OCCURRED_AT,
        )

    logparse_sha256 = "b" * 64
    logparse_artifact = Artifact(
        artifact_id=ARTIFACT_2,
        case_id=CASE_ID,
        kind=ArtifactKind.LOGPARSE_RUN,
        name="logparse-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=11,
        sha256=logparse_sha256,
        storage_key=f"cases/{CASE_ID}/resources/{ARTIFACT_2}/tree",
        metadata=LogparseRunMetadata(
            tree_manifest_sha256=logparse_sha256,
            logparse_version_ref=_ref("logparse", "9"),
            parse_manifest_relative_path="parse_manifest.json",
            source_attachment_id=_id(70),
            source_attachment_sha256="c" * 64,
            parse_parameters=LogparseParseParameters(product="payments"),
        ),
        created_by_job_id=JOB_ID,
        created_at=OCCURRED_AT,
    )

    evidence_proposal = EvidenceProposal(
        proposal_key="logparse-evidence",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key="logparse-run",
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="events/timeout.json",
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary="The parsed event records the deadline.",
        content_hash=None,
        staged_resource_ref=None,
    )
    evidence = formalize_accepted_evidence(
        [evidence_proposal],
        [evidence_proposal.proposal_key],
        case_id=CASE_ID,
        evidence_ids_by_proposal_key={evidence_proposal.proposal_key: EVIDENCE_1},
        existing_source_refs=set(),
        artifacts_by_proposal_key={"logparse-run": logparse_artifact},
        planned_targets_by_proposal_key={},
        published_resources_by_proposal_key={},
        occurred_at=OCCURRED_AT,
    )[evidence_proposal.proposal_key]
    assert evidence.source_ref == ARTIFACT_2
    assert evidence.collected_at == OCCURRED_AT
    assert evidence.resource_ref is None

    with pytest.raises(ValueError, match="LOGPARSE_RUN"):
        formalize_accepted_evidence(
            [evidence_proposal],
            [evidence_proposal.proposal_key],
            case_id=CASE_ID,
            evidence_ids_by_proposal_key={evidence_proposal.proposal_key: EVIDENCE_1},
            existing_source_refs=set(),
            artifacts_by_proposal_key={
                "logparse-run": artifacts[artifact_proposal.proposal_key]
            },
            planned_targets_by_proposal_key={},
            published_resources_by_proposal_key={},
            occurred_at=OCCURRED_AT,
        )


def test_candidate_hash_preimage_and_existing_candidate_revision_are_frozen() -> None:
    candidate = formalize_accepted_candidate(
        _candidate_draft(),
        "candidate",
        current_candidate=None,
        problem_completion_criteria=["The cause is evidenced."],
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"evidence-one": EVIDENCE_1},
        candidate_ids_by_proposal_key={"candidate": CANDIDATE_ID},
        proposed_by_job_id=JOB_ID,
    )
    assert candidate is not None
    preimage = {
        "resolution_status": candidate.resolution_status,
        "terminal_path_id": candidate.terminal_path_id,
        "statement": candidate.statement,
        "causal_factors": [
            item.model_dump(mode="json") for item in candidate.causal_factors
        ],
        "candidate_factors": [],
        "excluded_factors": [],
        "supporting_evidence_refs": [EVIDENCE_1],
        "completion_criteria_mapping": [
            candidate.completion_criteria_mapping[0].model_dump(mode="json")
        ],
    }
    assert candidate.content_hash == canonical_json_sha256(preimage)
    assert candidate.revision == 1
    assert candidate.status is CandidateStatus.PROPOSED

    installed = apply_candidate_mutation(
        None,
        CandidateMutation(
            action=CandidateMutationAction.INSTALL,
            candidate_binding=ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key="candidate",
            ),
            expected_status=None,
            target_status=CandidateStatus.REVIEWING,
            reason=None,
        ),
        candidates_by_proposal_key={"candidate": candidate},
    )
    assert installed is not None
    revised = formalize_accepted_candidate(
        _candidate_draft(existing_id=CANDIDATE_ID),
        "candidate",
        current_candidate=installed,
        problem_completion_criteria=["The cause is evidenced."],
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"evidence-one": EVIDENCE_1},
        candidate_ids_by_proposal_key={},
        proposed_by_job_id=_id(12),
    )
    assert revised is not None
    assert revised.conclusion_id == CANDIDATE_ID
    assert revised.revision == 2
    assert revised.proposed_by_job_id == _id(12)


def test_candidate_resolution_status_cannot_overstate_or_hide_incomplete_work() -> None:
    complete = _candidate_draft().model_dump(mode="python")
    complete["candidate_factors"] = [
        {
            **complete["causal_factors"][0],
            "factor_id": "remaining_candidate",
        }
    ]
    with pytest.raises(ValueError, match="COMPLETE Candidate draft cannot retain"):
        CandidateConclusionDraft.model_validate(complete)

    partial = _candidate_draft().model_dump(mode="python")
    partial["resolution_status"] = "PARTIAL"
    partial["terminal_path_id"] = "partial"
    with pytest.raises(ValueError, match="explicitly incomplete criterion"):
        CandidateConclusionDraft.model_validate(partial)


def test_partial_candidate_preserves_reviewable_progress_and_content_hash() -> None:
    value = _candidate_draft().model_dump(mode="python")
    value["resolution_status"] = "PARTIAL"
    value["terminal_path_id"] = "partial"
    value["completion_criteria_mapping"][0]["status"] = "PARTIALLY_SATISFIED"
    draft = CandidateConclusionDraft.model_validate(value)
    candidate = formalize_accepted_candidate(
        draft,
        "candidate",
        current_candidate=None,
        problem_completion_criteria=["The cause is evidenced."],
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"evidence-one": EVIDENCE_1},
        candidate_ids_by_proposal_key={"candidate": CANDIDATE_ID},
        proposed_by_job_id=JOB_ID,
    )
    assert candidate is not None
    assert candidate.resolution_status.value == "PARTIAL"
    assert candidate.terminal_path_id == "partial"
    assert candidate.content_hash


def test_applies_delta_in_s01_order_and_bumps_semantic_revision_once() -> None:
    current = _state()
    new_user_fact = DiagnosisItem(
        item_id=_id(150),
        statement="order-123",
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.USER_INPUT,
            source_ref=_id(22),
            input_name="order_id",
        ),
        evidence_refs=[],
        created_revision=2,
        supersedes=[],
    )
    e1 = EvidenceBinding(existing_evidence_id=None, evidence_proposal_key="evidence-one")
    e2 = EvidenceBinding(existing_evidence_id=None, evidence_proposal_key="evidence-two")
    delta = DiagnosisStateDelta(
        problem_spec_patch=ProblemSpecPatch(actual_behavior="The request exceeds its deadline."),
        add_user_facts=[new_user_fact],
        proposed_facts=[_draft(140, "The deadline was exceeded.", e2)],
        add_active_hypotheses=[_draft(310, "Later hypothesis"), _draft(309, "Earlier hypothesis")],
        update_hypotheses=[
            DiagnosisItemChange(
                item_id=_id(300),
                statement="Updated first hypothesis",
                reason="New evidence narrows it.",
                evidence_bindings=[e1],
            )
        ],
        reject_hypotheses=[
            DiagnosisItemChange(
                item_id=_id(301),
                statement=None,
                reason="The fixed evidence excludes it.",
                evidence_bindings=[EvidenceBinding(existing_evidence_id=EVIDENCE_0, evidence_proposal_key=None)],
            )
        ],
        add_open_questions=[_draft(410, "Later question"), _draft(409, "Earlier question")],
        resolve_questions=[
            DiagnosisItemChange(
                item_id=_id(400),
                statement=None,
                reason="The request is now identified.",
                evidence_bindings=[e1],
            )
        ],
        add_pending_requirements=[
            _requirement(510, "later_input"),
            _requirement(509, "earlier_input"),
        ],
        fulfill_requirements=[
            RequirementFulfillment(
                requirement_id=_id(500),
                fulfilled_by_refs=[new_user_fact.item_id],
            )
        ],
        add_evidence_bindings=[e2, e1],
    )
    candidate = formalize_accepted_candidate(
        _candidate_draft(),
        "candidate",
        current_candidate=None,
        problem_completion_criteria=current.problem_spec.completion_criteria,
        existing_evidence_ids=set(current.evidence_refs),
        evidence_ids_by_proposal_key={"evidence-one": EVIDENCE_1},
        candidate_ids_by_proposal_key={"candidate": CANDIDATE_ID},
        proposed_by_job_id=JOB_ID,
    )
    assert candidate is not None
    install = CandidateMutation(
        action=CandidateMutationAction.INSTALL,
        candidate_binding=ReviewTargetBinding(
            existing_candidate_target=None,
            accepted_candidate_proposal_key="candidate",
        ),
        expected_status=None,
        target_status=CandidateStatus.REVIEWING,
        reason=None,
    )
    result = apply_diagnosis_state_delta(
        current,
        delta,
        evidence_ids_by_proposal_key={
            "evidence-one": EVIDENCE_1,
            "evidence-two": EVIDENCE_2,
        },
        candidate_mutation=install,
        candidates_by_proposal_key={"candidate": candidate},
        expected_target_revision=2,
    )

    assert result.revision == 2
    assert result.problem_spec.revision == 2
    assert result.problem_spec.actual_behavior == "The request exceeds its deadline."
    assert [item.item_id for item in result.confirmed_facts] == [_id(140)]
    assert [item.item_id for item in result.active_hypotheses] == [_id(300), _id(309), _id(310)]
    assert result.active_hypotheses[0].statement == "Updated first hypothesis"
    assert [item.item_id for item in result.rejected_hypotheses] == [_id(301)]
    assert [item.item_id for item in result.open_questions] == [_id(409), _id(410)]
    assert [item.requirement_id for item in result.pending_requirements] == [_id(500), _id(509), _id(510)]
    assert result.pending_requirements[0].status is RequirementStatus.FULFILLED
    assert result.evidence_refs == [EVIDENCE_0, EVIDENCE_1, EVIDENCE_2]
    assert result.candidate_conclusion is not None
    assert result.candidate_conclusion.status is CandidateStatus.REVIEWING


def test_resolved_question_still_requires_every_evidence_binding_to_resolve() -> None:
    delta = DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[
            DiagnosisItemChange(
                item_id=_id(400),
                statement=None,
                reason="The question was answered.",
                evidence_bindings=[
                    EvidenceBinding(
                        existing_evidence_id=None,
                        evidence_proposal_key="unaccepted-evidence",
                    )
                ],
            )
        ],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )

    with pytest.raises(ValueError, match="unresolved Evidence proposal binding"):
        apply_diagnosis_state_delta(
            _state(),
            delta,
            evidence_ids_by_proposal_key={},
        )


class _RecordingProjector:
    def __init__(self) -> None:
        self.seen: DiagnosisState | None = None

    def project(self, target_diagnosis_state: DiagnosisState) -> ContextSnapshot:
        self.seen = target_diagnosis_state
        return ContextSnapshot(
            diagnosis_state_revision=target_diagnosis_state.revision,
            problem_spec=target_diagnosis_state.problem_spec,
            user_facts=target_diagnosis_state.user_facts,
            confirmed_facts=target_diagnosis_state.confirmed_facts,
            active_hypotheses=target_diagnosis_state.active_hypotheses,
            rejected_hypotheses=target_diagnosis_state.rejected_hypotheses,
            open_questions=target_diagnosis_state.open_questions,
            pending_requirements=target_diagnosis_state.pending_requirements,
            evidence_refs=target_diagnosis_state.evidence_refs,
            candidate_conclusion=target_diagnosis_state.candidate_conclusion,
        )


def test_builds_pending_job_from_final_state_and_projector() -> None:
    target = _state().model_copy(
        update={"revision": 2, "evidence_refs": [EVIDENCE_0, EVIDENCE_1]}
    )
    target = DiagnosisState.model_validate(target.model_dump(mode="python"))
    spec = JobSpec(
        job_type=JobType.DIAGNOSE,
        diagnosis_mode="SPECIALIZED",
        generic_skill_name=None,
        generic_problem_text=None,
        goal="Continue the fixed diagnosis.",
        target_state_revision=2,
        evidence_bindings=[
            PlannedResourceBinding(existing_resource_id=EVIDENCE_0, accepted_proposal_key=None),
            PlannedResourceBinding(existing_resource_id=None, accepted_proposal_key="new-evidence"),
        ],
        attachment_refs=[],
        previous_outcome_refs=[OUTCOME_ID],
        artifact_bindings=[
            PlannedResourceBinding(existing_resource_id=ARTIFACT_0, accepted_proposal_key=None),
            PlannedResourceBinding(existing_resource_id=None, accepted_proposal_key="new-artifact"),
        ],
        agent_profile_ref=_ref("diagnosis-profile", "a"),
        available_skill_refs=[],
        skill_ref=_ref("rpc-timeout", "b"),
        tool_bundle_ref=_ref("diagnosis-tools", "c"),
        context_policy_ref=_ref("diagnosis-context", "d"),
        output_contract_ref=_ref("diagnosis-outcome", "e"),
        logparse_tool_ref=None,
        logparse_product=None,
        review_target_binding=None,
        replacement_for_job_id=None,
        resource_limits=default_resource_limits(JobType.DIAGNOSE),
    )
    projector = _RecordingProjector()
    job = build_job(
        spec,
        job_id=_id(12),
        case_id=CASE_ID,
        created_at=OCCURRED_AT,
        target_diagnosis_state=target,
        projector=projector,
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"new-evidence": EVIDENCE_1},
        existing_artifact_ids={ARTIFACT_0},
        artifact_ids_by_proposal_key={"new-artifact": ARTIFACT_1},
        existing_candidate=None,
        candidates_by_proposal_key={},
    )
    assert projector.seen == target
    assert job.evidence_refs == [EVIDENCE_0, EVIDENCE_1]
    assert job.artifact_refs == [ARTIFACT_0, ARTIFACT_1]
    assert job.base_state_revision == job.context_snapshot.diagnosis_state_revision == 2
    assert job.created_at == OCCURRED_AT


def test_generic_job_skips_context_projection_and_freezes_only_raw_problem_text() -> None:
    target = _state()
    spec = JobSpec(
        job_type=JobType.DIAGNOSE,
        diagnosis_mode="GENERIC",
        generic_skill_name="generic-problem-locator-smoke",
        generic_problem_text="原始问题第一行\n第二行逐字保留",
        goal="Run the configured generic problem locator.",
        target_state_revision=target.revision,
        evidence_bindings=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_bindings=[],
        agent_profile_ref=_ref("generic-profile", "a"),
        available_skill_refs=[],
        skill_ref=None,
        tool_bundle_ref=_ref("generic-tools", "b"),
        context_policy_ref=_ref("generic-context", "c"),
        output_contract_ref=_ref("generic-outcome", "d"),
        logparse_tool_ref=None,
        logparse_product=None,
        review_target_binding=None,
        replacement_for_job_id=None,
        resource_limits=default_resource_limits(JobType.DIAGNOSE),
    )
    projector = _RecordingProjector()

    job = build_job(
        spec,
        job_id=_id(12),
        case_id=CASE_ID,
        created_at=OCCURRED_AT,
        target_diagnosis_state=target,
        projector=projector,
        existing_evidence_ids=set(target.evidence_refs),
        evidence_ids_by_proposal_key={},
        existing_artifact_ids=set(),
        artifact_ids_by_proposal_key={},
        existing_candidate=target.candidate_conclusion,
        candidates_by_proposal_key={},
    )

    assert projector.seen is None
    assert job.context_snapshot is None
    assert job.generic_problem_text == "原始问题第一行\n第二行逐字保留"
    assert job.evidence_refs == []
    assert job.attachment_refs == []
    assert job.previous_outcome_refs == []
    assert job.artifact_refs == []


def test_build_job_orders_evidence_as_target_snapshot_subsequence() -> None:
    target = _state().model_copy(
        update={"revision": 2, "evidence_refs": [EVIDENCE_0, EVIDENCE_1]}
    )
    target = DiagnosisState.model_validate(target.model_dump(mode="python"))
    spec = JobSpec(
        job_type=JobType.DIAGNOSE,
        diagnosis_mode="SPECIALIZED",
        generic_skill_name=None,
        generic_problem_text=None,
        goal="Continue with both accepted evidence records.",
        target_state_revision=2,
        evidence_bindings=[
            PlannedResourceBinding(
                existing_resource_id=None,
                accepted_proposal_key="new-evidence",
            ),
            PlannedResourceBinding(
                existing_resource_id=EVIDENCE_0,
                accepted_proposal_key=None,
            ),
        ],
        attachment_refs=[],
        previous_outcome_refs=[OUTCOME_ID],
        artifact_bindings=[],
        agent_profile_ref=_ref("diagnosis-profile", "a"),
        available_skill_refs=[],
        skill_ref=_ref("rpc-timeout", "b"),
        tool_bundle_ref=_ref("diagnosis-tools", "c"),
        context_policy_ref=_ref("diagnosis-context", "d"),
        output_contract_ref=_ref("diagnosis-outcome", "e"),
        logparse_tool_ref=None,
        logparse_product=None,
        review_target_binding=None,
        replacement_for_job_id=None,
        resource_limits=default_resource_limits(JobType.DIAGNOSE),
    )

    job = build_job(
        spec,
        job_id=_id(12),
        case_id=CASE_ID,
        created_at=OCCURRED_AT,
        target_diagnosis_state=target,
        projector=_RecordingProjector(),
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"new-evidence": EVIDENCE_1},
        existing_artifact_ids=set(),
        artifact_ids_by_proposal_key={},
        existing_candidate=None,
        candidates_by_proposal_key={},
    )

    assert job.evidence_refs == [EVIDENCE_0, EVIDENCE_1]


def test_explicit_field_and_candidate_acceptance_mutations() -> None:
    skill = _ref("rpc-timeout", "b")
    assert apply_selected_skill_update(
        None, SelectedSkillUpdate(action=FieldUpdateAction.SET, value=skill)
    ) == skill
    assert apply_selected_skill_update(
        skill, SelectedSkillUpdate(action=FieldUpdateAction.CLEAR, value=None)
    ) is None

    failure = CaseFailure(
        code="OUTCOME_INVALID",
        message="Job outcome validation failed.",
        source_job_id=JOB_ID,
        source_outcome_id=OUTCOME_ID,
        occurred_at=OCCURRED_AT,
    )
    assert apply_case_failure_update(
        None, CaseFailureUpdate(action=FieldUpdateAction.SET, value=failure)
    ) == failure

    candidate = formalize_accepted_candidate(
        _candidate_draft(),
        "candidate",
        current_candidate=None,
        problem_completion_criteria=["The cause is evidenced."],
        existing_evidence_ids={EVIDENCE_0},
        evidence_ids_by_proposal_key={"evidence-one": EVIDENCE_1},
        candidate_ids_by_proposal_key={"candidate": CANDIDATE_ID},
        proposed_by_job_id=JOB_ID,
    )
    assert candidate is not None
    reviewing = candidate.model_copy(update={"status": CandidateStatus.REVIEWING})
    target = reviewing.model_dump(mode="python")
    fixed_target = {
        "candidate_conclusion_id": target["conclusion_id"],
        "candidate_revision": target["revision"],
        "candidate_content_hash": target["content_hash"],
    }
    mutation = CandidateMutation(
        action=CandidateMutationAction.SET_STATUS,
        candidate_binding=ReviewTargetBinding(
            existing_candidate_target=fixed_target,
            accepted_candidate_proposal_key=None,
        ),
        expected_status=CandidateStatus.REVIEWING,
        target_status=CandidateStatus.ACCEPTED,
        reason=None,
    )
    accepted = apply_candidate_mutation(
        reviewing, mutation, candidates_by_proposal_key={}
    )
    assert accepted is not None
    assert accepted.status is CandidateStatus.ACCEPTED
    assert resolve_final_result(accepted, mutation.candidate_binding.existing_candidate_target) == accepted
