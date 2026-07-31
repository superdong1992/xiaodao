"""Outcome, trigger, and transition-plan views of the public contract models."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .enums import (
    ArtifactKind,
    AttachmentStatus,
    CandidateStatus,
    ErrorCode,
    EvidenceSourceType,
    FieldUpdateAction,
    OutcomeDisposition,
    OutcomeResultType,
    RequirementKind,
    RequirementStatus,
    ReviewVerdict,
    RouteKind,
    TriggerType,
)
from .models import (
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    AgentJobOutcome,
    ApplicationError,
    ArtifactProposal,
    AssetUnavailableTriggerPayload,
    CancelCaseTriggerPayload,
    CandidateConclusionDraft,
    CandidateConclusion,
    CandidateMutation,
    CaseAggregate,
    CaseFailureUpdate,
    CaseSnapshot,
    CompletionCriterionDraftMapping,
    CoordinatorPlanResult,
    CreateCaseTriggerPayload,
    DiagnosisItemChange,
    DiagnosisItemDraft,
    DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceProposal,
    EvidenceSourceBinding,
    ExecutionFailure,
    ExecutionFailedTriggerPayload,
    Finding,
    Job,
    JobOutcome,
    JobSpec,
    LogparseParseClaim,
    OldEpochTriggerPayload,
    OutcomePayload,
    PlannedResourceBinding,
    ProblemSpec,
    ProblemSpecPatch,
    RequirementFulfillment,
    ResumeInterruptedTriggerPayload,
    ReviewAssessment,
    ReviewOutcomeTriggerPayload,
    ReviewTargetBinding,
    RouteDecision,
    RouteOutcomeTriggerPayload,
    RuntimeBindings,
    SelectedSkillUpdate,
    StaleActiveOutcomeTriggerPayload,
    SubmitSupplementTriggerPayload,
    TransitionPlan,
    TriggerPayload,
    UserResultPayload,
    UserFactEvidenceLocator,
    ValidatedTrigger,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    validate_workspace_manifest_for_job,
)
from .serialization import parse_canonical_json_bytes


PROBLEM_SPEC_PATCH_FIELDS = (
    "statement",
    "expected_behavior",
    "actual_behavior",
    "scope",
    "goals",
    "non_goals",
    "constraints",
    "completion_criteria",
)


def apply_problem_spec_patch(
    current: ProblemSpec,
    patch: ProblemSpecPatch,
) -> tuple[ProblemSpec, bool]:
    """Apply frozen whole-field patch semantics and its independent revision.

    Deciding that a semantic target change requires a new Case remains an S03
    policy decision.  Once a patch is accepted, this helper performs the S00
    mechanical operation: absent fields are preserved, present arrays replace
    rather than append, equal values are a no-op, and one or more changes
    increment ``ProblemSpec.revision`` exactly once.
    """

    present = patch.model_fields_set
    updates = {
        field_name: getattr(patch, field_name)
        for field_name in PROBLEM_SPEC_PATCH_FIELDS
        if field_name in present and getattr(current, field_name) != getattr(patch, field_name)
    }
    if not updates:
        return current.model_copy(deep=True), False
    payload = current.model_dump(mode="python")
    payload.update(updates)
    payload["revision"] = current.revision + 1
    return ProblemSpec.model_validate(payload), True


def validate_coordinator_plan_result(
    trigger: ValidatedTrigger,
    result: CoordinatorPlanResult,
) -> CoordinatorPlanResult:
    """Validate the frozen non-exception Coordinator decision channel.

    A stable-target change is already a validated application fact when it
    reaches the Coordinator, so it has exactly one legal decision:
    ``NEW_CASE_REQUIRED`` with no TransitionPlan or mutation side channel.
    """

    from .errors import COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER

    payload = getattr(trigger, "payload", None)
    if isinstance(result, ApplicationError):
        trigger_type = getattr(trigger, "trigger_type", None)
        if trigger_type is None:
            trigger_type = {
                CreateCaseTriggerPayload: TriggerType.CREATE_CASE,
                RouteOutcomeTriggerPayload: TriggerType.ROUTE_OUTCOME,
                DiagnosisOutcomeTriggerPayload: TriggerType.DIAGNOSIS_OUTCOME,
                ReviewOutcomeTriggerPayload: TriggerType.REVIEW_OUTCOME,
                SubmitSupplementTriggerPayload: TriggerType.SUBMIT_SUPPLEMENT,
                CancelCaseTriggerPayload: TriggerType.CANCEL_CASE,
                ResumeInterruptedTriggerPayload: TriggerType.RESUME_INTERRUPTED,
                ExecutionFailedTriggerPayload: TriggerType.EXECUTION_FAILED,
                AssetUnavailableTriggerPayload: TriggerType.ASSET_VERSION_UNAVAILABLE,
                OldEpochTriggerPayload: TriggerType.MARK_OLD_EPOCH_INTERRUPTED,
                StaleActiveOutcomeTriggerPayload: TriggerType.STALE_ACTIVE_OUTCOME,
            }.get(type(payload))
        if trigger_type is None:
            raise ValueError(
                "Coordinator ApplicationError must use a frozen non-retryable "
                "code allowed for this Trigger; Trigger type is required"
            )
        allowed_codes = COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER[trigger_type]
        if result.code not in allowed_codes or result.retryable:
            raise ValueError(
                "Coordinator ApplicationError must use a frozen non-retryable "
                "code allowed for this Trigger"
            )
    elif not isinstance(result, TransitionPlan):
        raise TypeError("Coordinator result must be TransitionPlan or ApplicationError")

    if (
        isinstance(payload, SubmitSupplementTriggerPayload)
        and payload.stable_target_changed
        and (
            not isinstance(result, ApplicationError)
            or result.code is not ErrorCode.NEW_CASE_REQUIRED
        )
    ):
        raise ValueError(
            "stable_target_changed requires NEW_CASE_REQUIRED without a plan"
        )
    return result


def coordinator_outcome_error_failure(
    trigger: ValidatedTrigger,
    error: ApplicationError,
) -> ExecutionFailure:
    """Normalize a finalized Outcome semantic rejection to one terminal failure.

    State drift is deliberately excluded: ``INVALID_CASE_STATE`` belongs to
    S03's STALE path and must never be made into a second terminal rejection.
    """

    from .errors import deterministic_outcome_failure

    validate_coordinator_plan_result(trigger, error)
    if trigger.trigger_type not in {
        TriggerType.ROUTE_OUTCOME,
        TriggerType.DIAGNOSIS_OUTCOME,
        TriggerType.REVIEW_OUTCOME,
    }:
        raise ValueError(
            "Coordinator error normalization requires a finalized Outcome Trigger"
        )
    if error.code is ErrorCode.INVALID_CASE_STATE:
        raise ValueError(
            "INVALID_CASE_STATE belongs to the STALE Outcome path"
        )
    semantic_codes = {
        TriggerType.ROUTE_OUTCOME: frozenset({ErrorCode.VALIDATION_ERROR}),
        TriggerType.DIAGNOSIS_OUTCOME: frozenset(
            {ErrorCode.VALIDATION_ERROR, ErrorCode.NEW_CASE_REQUIRED}
        ),
        TriggerType.REVIEW_OUTCOME: frozenset({ErrorCode.VALIDATION_ERROR}),
    }
    if error.code not in semantic_codes[trigger.trigger_type]:
        raise ValueError(
            "Coordinator error is not a finalized Outcome semantic rejection"
        )
    return deterministic_outcome_failure(ErrorCode.OUTCOME_INVALID, error.details)


def _payload_evidence_bindings(payload: OutcomePayload | None) -> list[EvidenceBinding]:
    if not isinstance(payload, DiagnosisOutcome):
        return []
    bindings: list[EvidenceBinding] = []
    for finding in payload.findings:
        bindings.extend(finding.evidence_bindings)
    delta = payload.state_delta
    for draft in delta.proposed_facts + delta.add_active_hypotheses + delta.add_open_questions:
        bindings.extend(draft.evidence_bindings)
    for change in delta.update_hypotheses + delta.reject_hypotheses + delta.resolve_questions:
        bindings.extend(change.evidence_bindings)
    bindings.extend(delta.add_evidence_bindings)
    candidate = payload.candidate_conclusion_draft
    if candidate is not None:
        bindings.extend(candidate.supporting_evidence_bindings)
        for mapping in candidate.completion_criteria_mapping:
            bindings.extend(mapping.evidence_bindings)
    return bindings


def _candidate_evidence_bindings(
    candidate: CandidateConclusionDraft | None,
) -> list[EvidenceBinding]:
    if candidate is None:
        return []
    bindings = list(candidate.supporting_evidence_bindings)
    for mapping in candidate.completion_criteria_mapping:
        bindings.extend(mapping.evidence_bindings)
    return bindings


def _delta_evidence_bindings(delta: DiagnosisStateDelta) -> list[EvidenceBinding]:
    bindings: list[EvidenceBinding] = []
    for draft in delta.proposed_facts + delta.add_active_hypotheses + delta.add_open_questions:
        bindings.extend(draft.evidence_bindings)
    for change in delta.update_hypotheses + delta.reject_hypotheses + delta.resolve_questions:
        bindings.extend(change.evidence_bindings)
    bindings.extend(delta.add_evidence_bindings)
    return bindings


def validate_outcome_for_job(
    job: Job,
    outcome: AgentJobOutcome | JobOutcome,
    resource_context: WorkspaceInputManifest | CaseAggregate | None = None,
) -> AgentJobOutcome | JobOutcome:
    """Freeze technical Job↔Outcome invariants shared by S03 and S04."""

    if (
        outcome.job_id != job.job_id
        or outcome.case_id != job.case_id
        or outcome.job_type is not job.job_type
        or outcome.base_state_revision != job.base_state_revision
    ):
        raise ValueError("Outcome identity and base revision must exactly match its Job")
    job_evidence = set(job.evidence_refs)
    if any(ref not in job_evidence for ref in outcome.consumed_evidence_refs):
        raise ValueError("consumed_evidence_refs must be fixed by the Job")

    payload = outcome.payload
    if isinstance(payload, RouteDecision):
        if payload.kind is RouteKind.MATCHED and payload.skill_ref not in job.available_skill_refs:
            raise ValueError("MATCHED route skill_ref must belong to Job.available_skill_refs")

    if isinstance(payload, ReviewAssessment):
        target = job.review_target
        if target is None or (
            payload.candidate_conclusion_id != target.candidate_conclusion_id
            or payload.candidate_revision != target.candidate_revision
            or payload.candidate_content_hash != target.candidate_content_hash
        ):
            raise ValueError("ReviewAssessment must echo the complete Job.review_target")
        if payload.reviewed_state_revision != job.base_state_revision:
            raise ValueError("reviewed_state_revision must equal the Job base revision")
        if any(ref not in job_evidence for ref in payload.reviewed_evidence_refs):
            raise ValueError("reviewed_evidence_refs must be fixed by the REVIEW Job")
        candidate = job.context_snapshot.candidate_conclusion
        if (
            payload.verdict is ReviewVerdict.PASS
            and candidate is not None
            and any(ref not in payload.reviewed_evidence_refs for ref in candidate.supporting_evidence_refs)
        ):
            raise ValueError("PASS must review every candidate supporting Evidence")

    if isinstance(payload, DiagnosisOutcome):
        requirements = {
            requirement.requirement_id: requirement
            for requirement in job.context_snapshot.pending_requirements
        }
        for requirement in payload.state_delta.add_pending_requirements:
            if requirement.requirement_id in requirements:
                raise ValueError("Outcome cannot add an existing requirement ID")
            requirements[requirement.requirement_id] = requirement
        fulfilled = {
            item.requirement_id for item in payload.state_delta.fulfill_requirements
        }
        open_requirements = {
            requirement_id: requirement
            for requirement_id, requirement in requirements.items()
            if requirement.status is RequirementStatus.OPEN
            and requirement_id not in fulfilled
        }
        if any(
            requirement_id not in open_requirements
            or open_requirements[requirement_id].kind is not RequirementKind.INPUT
            for requirement_id in payload.requested_input
        ):
            raise ValueError("requested_input must resolve OPEN INPUT requirements")
        if any(
            requirement_id not in open_requirements
            or open_requirements[requirement_id].kind is not RequirementKind.ATTACHMENT
            for requirement_id in payload.requested_attachments
        ):
            raise ValueError(
                "requested_attachments must resolve OPEN ATTACHMENT requirements"
            )
        candidate = payload.candidate_conclusion_draft
        if candidate is not None:
            criteria = job.context_snapshot.problem_spec.completion_criteria
            mappings = candidate.completion_criteria_mapping
            if len(mappings) != len(criteria) or any(
                mapping.criterion_index != index or mapping.criterion != criterion
                for index, (mapping, criterion) in enumerate(zip(mappings, criteria, strict=True))
            ):
                raise ValueError("candidate draft must exactly cover the Job ProblemSpec criteria")
            current = job.context_snapshot.candidate_conclusion
            if candidate.existing_conclusion_id is not None and (
                current is None or current.conclusion_id != candidate.existing_conclusion_id
            ):
                raise ValueError("existing candidate ID must match the Job snapshot candidate")
        if any(
            binding.existing_evidence_id is not None
            and binding.existing_evidence_id not in job_evidence
            for binding in _payload_evidence_bindings(payload)
        ):
            raise ValueError("Outcome Evidence bindings must be fixed by the Job")

    evidence_proposals = (
        outcome.proposed_evidence_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_evidence
    )
    source_sets = {
        EvidenceSourceType.USER_FACT: {
            item.item_id for item in job.context_snapshot.user_facts
        },
        EvidenceSourceType.ATTACHMENT: set(job.attachment_refs),
        EvidenceSourceType.LOGPARSE: set(job.artifact_refs),
        EvidenceSourceType.TOOL_OUTPUT: set(job.artifact_refs),
        EvidenceSourceType.PREVIOUS_OUTCOME: set(job.previous_outcome_refs),
    }
    for proposal in evidence_proposals:
        existing = proposal.source_binding.existing_source_ref
        if existing is not None and existing not in source_sets[proposal.source_type]:
            raise ValueError("Evidence source binding must be fixed by the Job")
        if proposal.source_type is EvidenceSourceType.USER_FACT and existing is not None:
            source = next(
                item
                for item in job.context_snapshot.user_facts
                if item.item_id == existing
            )
            if (
                not isinstance(proposal.locator, UserFactEvidenceLocator)
                or proposal.locator.input_name != source.provenance.input_name
            ):
                raise ValueError("USER_FACT locator input_name must match its source")

    artifact_proposals = (
        outcome.proposed_artifact_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_artifacts
    )
    for artifact in artifact_proposals:
        if artifact.artifact_kind is ArtifactKind.LOGPARSE_RUN:
            metadata = artifact.metadata
            if (
                job.logparse_tool_ref is None
                or metadata.logparse_version_ref != job.logparse_tool_ref
                or metadata.parse_parameters.product != job.logparse_product
                or metadata.source_attachment_id not in job.attachment_refs
            ):
                raise ValueError("LOGPARSE_RUN metadata must be fixed by the producing Job")
    requires_resource_context = any(
        proposal.source_binding.existing_source_ref is not None
        and proposal.source_type
        in {
            EvidenceSourceType.ATTACHMENT,
            EvidenceSourceType.LOGPARSE,
            EvidenceSourceType.TOOL_OUTPUT,
        }
        for proposal in evidence_proposals
    ) or any(
        artifact.artifact_kind is ArtifactKind.LOGPARSE_RUN
        for artifact in artifact_proposals
    )
    if requires_resource_context and resource_context is None:
        raise ValueError("resource-backed Outcome validation requires a fixed resource context")
    if resource_context is not None:
        validate_outcome_resources_for_job(job, outcome, resource_context)
    return outcome


def validate_outcome_resources_for_job(
    job: Job,
    outcome: AgentJobOutcome | JobOutcome,
    resource_context: WorkspaceInputManifest | CaseAggregate,
) -> AgentJobOutcome | JobOutcome:
    """Validate typed existing sources and hashes from the fixed Workspace view."""

    if isinstance(resource_context, WorkspaceInputManifest):
        validate_workspace_manifest_for_job(resource_context, job)
        attachments = {
            entry.resource_id: entry
            for entry in resource_context.entries
            if isinstance(entry, WorkspaceAttachmentInput)
        }
        artifacts = {
            entry.resource_id: entry
            for entry in resource_context.entries
            if isinstance(entry, WorkspaceArtifactInput)
        }
    else:
        stored_job = resource_context.jobs.get(job.job_id)
        if stored_job != job or resource_context.case.case_id != job.case_id:
            raise ValueError("CaseAggregate resource context must contain the exact Job")
        attachments = {
            ref: resource_context.attachments[ref]
            for ref in job.attachment_refs
            if ref in resource_context.attachments
        }
        artifacts = {
            ref: resource_context.artifacts[ref]
            for ref in job.artifact_refs
            if ref in resource_context.artifacts
        }
    evidence_proposals = (
        outcome.proposed_evidence_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_evidence
    )
    for proposal in evidence_proposals:
        source_ref = proposal.source_binding.existing_source_ref
        if source_ref is None:
            continue
        if proposal.source_type is EvidenceSourceType.ATTACHMENT:
            source = attachments.get(source_ref)
            if source is None or (
                hasattr(source, "status")
                and source.status is not AttachmentStatus.READY
            ):
                raise ValueError("ATTACHMENT source must resolve the fixed manifest entry")
        elif proposal.source_type is EvidenceSourceType.LOGPARSE:
            source = artifacts.get(source_ref)
            source_kind = getattr(source, "artifact_kind", getattr(source, "kind", None))
            if source is None or source_kind is not ArtifactKind.LOGPARSE_RUN:
                raise ValueError("LOGPARSE source must resolve a LOGPARSE_RUN Artifact")
        elif proposal.source_type is EvidenceSourceType.TOOL_OUTPUT:
            source = artifacts.get(source_ref)
            source_kind = getattr(source, "artifact_kind", getattr(source, "kind", None))
            if source is None or source_kind is not ArtifactKind.DIAGNOSTIC_EXPORT:
                raise ValueError(
                    "TOOL_OUTPUT source must resolve a DIAGNOSTIC_EXPORT Artifact"
                )

    artifact_proposals = (
        outcome.proposed_artifact_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_artifacts
    )
    for proposal in artifact_proposals:
        if proposal.artifact_kind is not ArtifactKind.LOGPARSE_RUN:
            continue
        metadata = proposal.metadata
        attachment = attachments.get(metadata.source_attachment_id)
        if (
            attachment is None
            or (
                hasattr(attachment, "status")
                and attachment.status is not AttachmentStatus.READY
            )
            or attachment.sha256 != metadata.source_attachment_sha256
        ):
            raise ValueError("LOGPARSE_RUN source attachment hash must match the manifest")
    return outcome


def validate_user_result_for_outcome(
    job: Job,
    outcome: AgentJobOutcome | JobOutcome,
    result_bytes: bytes,
) -> UserResultPayload:
    """Validate the canonical USER_RESULT as the exact candidate representation."""

    result = parse_canonical_json_bytes(result_bytes, model_type=UserResultPayload)
    payload = outcome.payload
    candidate = (
        payload.candidate_conclusion_draft
        if isinstance(payload, DiagnosisOutcome)
        else None
    )
    if candidate is None:
        raise ValueError("USER_RESULT requires a CandidateConclusionDraft")
    expected = {
        "problem_statement": job.context_snapshot.problem_spec.statement,
        "candidate_statement": candidate.statement,
        "supporting_evidence_bindings": candidate.supporting_evidence_bindings,
        "completion_criteria_mapping": candidate.completion_criteria_mapping,
    }
    for field_name, value in expected.items():
        if getattr(result, field_name) != value:
            raise ValueError(f"USER_RESULT {field_name} must exactly match the candidate seam")
    artifacts = (
        outcome.proposed_artifact_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_artifacts
    )
    user_results = [
        artifact
        for artifact in artifacts
        if artifact.artifact_kind is ArtifactKind.USER_RESULT
    ]
    if len(user_results) != 1:
        raise ValueError("candidate Outcome requires exactly one USER_RESULT Artifact")
    artifact = user_results[0]
    actual_size = len(result_bytes)
    actual_sha256 = hashlib.sha256(result_bytes).hexdigest()
    if isinstance(outcome, AgentJobOutcome):
        if artifact.declared_size is not None and artifact.declared_size != actual_size:
            raise ValueError("USER_RESULT declared_size does not match canonical bytes")
        if (
            artifact.declared_sha256 is not None
            and artifact.declared_sha256 != actual_sha256
        ):
            raise ValueError("USER_RESULT declared_sha256 does not match canonical bytes")
    elif artifact.size != actual_size or artifact.sha256 != actual_sha256:
        raise ValueError("USER_RESULT ArtifactProposal must match canonical bytes")
    return result


def validate_user_result_resolution(
    result: UserResultPayload,
    final_candidate: CandidateConclusion,
    evidence_ids_by_proposal: Mapping[str, str],
) -> CandidateConclusion:
    """Validate draft Evidence bindings after S03 resolves proposal keys to IDs."""

    def resolve(binding: EvidenceBinding) -> str:
        if binding.existing_evidence_id is not None:
            return binding.existing_evidence_id
        assert binding.evidence_proposal_key is not None
        try:
            return evidence_ids_by_proposal[binding.evidence_proposal_key]
        except KeyError as exc:
            raise ValueError("USER_RESULT references an unresolved Evidence proposal") from exc

    resolved_supporting = [
        resolve(binding) for binding in result.supporting_evidence_bindings
    ]
    if (
        result.candidate_statement != final_candidate.statement
        or resolved_supporting != final_candidate.supporting_evidence_refs
        or len(result.completion_criteria_mapping)
        != len(final_candidate.completion_criteria_mapping)
    ):
        raise ValueError("resolved USER_RESULT does not match the final Candidate")
    for draft_mapping, final_mapping in zip(
        result.completion_criteria_mapping,
        final_candidate.completion_criteria_mapping,
        strict=True,
    ):
        if (
            draft_mapping.criterion_index != final_mapping.criterion_index
            or draft_mapping.criterion != final_mapping.criterion
            or draft_mapping.satisfied != final_mapping.satisfied
            or draft_mapping.explanation != final_mapping.explanation
            or [resolve(binding) for binding in draft_mapping.evidence_bindings]
            != final_mapping.evidence_refs
        ):
            raise ValueError("resolved USER_RESULT mapping does not match the final Candidate")
    return final_candidate


def validate_logparse_claim_for_job(
    claim: LogparseParseClaim | None,
    job: Job,
    workspace_manifest: WorkspaceInputManifest,
    parse_request_bytes: bytes | None,
    outcome: AgentJobOutcome | JobOutcome | None = None,
) -> LogparseParseClaim | None:
    """Validate the one-shot logparse claim, manifest, request, and Outcome seam."""

    validate_workspace_manifest_for_job(workspace_manifest, job)
    existing_runs = [
        entry
        for entry in workspace_manifest.entries
        if isinstance(entry, WorkspaceArtifactInput)
        and entry.artifact_kind is ArtifactKind.LOGPARSE_RUN
    ]
    proposals: list[object] = []
    if outcome is not None:
        proposals = [
            proposal
            for proposal in (
                outcome.proposed_artifact_drafts
                if isinstance(outcome, AgentJobOutcome)
                else outcome.proposed_artifacts
            )
            if proposal.artifact_kind is ArtifactKind.LOGPARSE_RUN
        ]
    if claim is None:
        if parse_request_bytes is not None:
            raise ValueError("request bytes require a parse claim")
        if proposals:
            raise ValueError("LOGPARSE_RUN proposal requires a parse claim")
        return None
    if existing_runs:
        raise ValueError("a fixed LOGPARSE_RUN manifest forbids a new parse claim")
    if parse_request_bytes is None:
        raise ValueError("a parse claim requires the exact request bytes")
    parse_canonical_json_bytes(parse_request_bytes)
    attachments = {
        entry.resource_id: entry
        for entry in workspace_manifest.entries
        if isinstance(entry, WorkspaceAttachmentInput)
    }
    attachment = attachments.get(claim.attachment_id)
    if (
        claim.job_id != job.job_id
        or job.logparse_tool_ref is None
        or claim.logparse_tool_ref != job.logparse_tool_ref
        or attachment is None
        or attachment.sha256 != claim.attachment_sha256
        or claim.request_sha256 != hashlib.sha256(parse_request_bytes).hexdigest()
    ):
        raise ValueError("logparse claim must match its Job, Attachment, and request")
    if outcome is not None:
        if outcome.result_type is OutcomeResultType.FAILED:
            if proposals:
                raise ValueError("failed claimed execution forbids LOGPARSE_RUN proposals")
        elif len(proposals) != 1:
            raise ValueError("successful claimed execution requires one LOGPARSE_RUN proposal")
        else:
            proposal = proposals[0]
            metadata = proposal.metadata
            if (
                proposal.proposal_key != claim.artifact_proposal_key
                or metadata.logparse_version_ref != claim.logparse_tool_ref
                or metadata.source_attachment_id != claim.attachment_id
                or metadata.source_attachment_sha256 != claim.attachment_sha256
            ):
                raise ValueError("LOGPARSE_RUN proposal must exactly match the parse claim")
    return claim


def validate_transition_plan_for_outcome(
    plan: TransitionPlan,
    outcome: AgentJobOutcome | JobOutcome,
) -> TransitionPlan:
    """Validate proposal/candidate acceptance across the Coordinator seam."""

    evidence = (
        outcome.proposed_evidence_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_evidence
    )
    artifacts = (
        outcome.proposed_artifact_drafts
        if isinstance(outcome, AgentJobOutcome)
        else outcome.proposed_artifacts
    )
    evidence_by_key = {proposal.proposal_key: proposal for proposal in evidence}
    artifact_by_key = {proposal.proposal_key: proposal for proposal in artifacts}
    accepted_evidence = set(plan.accepted_evidence_proposal_keys)
    accepted_artifacts = set(plan.accepted_artifact_proposal_keys)
    if not accepted_evidence <= evidence_by_key.keys():
        raise ValueError("TransitionPlan accepts an unknown Evidence proposal")
    if not accepted_artifacts <= artifact_by_key.keys():
        raise ValueError("TransitionPlan accepts an unknown Artifact proposal")
    if plan.outcome_disposition is not OutcomeDisposition.APPLIED and (
        accepted_evidence
        or accepted_artifacts
        or plan.accepted_candidate_proposal_key is not None
    ):
        raise ValueError("only an APPLIED Outcome may accept proposals")

    for key in accepted_evidence:
        artifact_key = evidence_by_key[key].source_binding.artifact_proposal_key
        if artifact_key is not None and artifact_key not in accepted_artifacts:
            raise ValueError("bound Evidence and LOGPARSE_RUN Artifact must be accepted together")
    for proposal in evidence:
        artifact_key = proposal.source_binding.artifact_proposal_key
        if artifact_key is not None and (
            (proposal.proposal_key in accepted_evidence)
            != (artifact_key in accepted_artifacts)
        ):
            raise ValueError("bound Evidence and LOGPARSE_RUN Artifact must be accepted together")

    payload = outcome.payload
    candidate = payload.candidate_conclusion_draft if isinstance(payload, DiagnosisOutcome) else None
    user_result_keys = {
        proposal.proposal_key
        for proposal in artifacts
        if proposal.artifact_kind is ArtifactKind.USER_RESULT
    }
    if plan.accepted_candidate_proposal_key is not None:
        if candidate is None or plan.accepted_candidate_proposal_key != candidate.proposal_key:
            raise ValueError("TransitionPlan accepts an unknown candidate proposal")
        if len(user_result_keys) != 1 or not user_result_keys <= accepted_artifacts:
            raise ValueError("an accepted candidate requires its unique USER_RESULT Artifact")
        for binding in _candidate_evidence_bindings(candidate):
            if (
                binding.evidence_proposal_key is not None
                and binding.evidence_proposal_key not in accepted_evidence
            ):
                raise ValueError("accepted candidate bindings require accepted Evidence proposals")
    elif user_result_keys & accepted_artifacts:
        raise ValueError("USER_RESULT cannot be accepted without its candidate")

    for binding in _delta_evidence_bindings(plan.accepted_state_delta):
        if (
            binding.evidence_proposal_key is not None
            and binding.evidence_proposal_key not in accepted_evidence
        ):
            raise ValueError("accepted state delta requires accepted Evidence proposals")

    if (
        plan.outcome_disposition is OutcomeDisposition.APPLIED
        and isinstance(payload, RouteDecision)
        and payload.kind is RouteKind.MATCHED
    ):
        update = plan.selected_skill_update
        if (
            update is None
            or update.action is not FieldUpdateAction.SET
            or update.value != payload.skill_ref
        ):
            raise ValueError("MATCHED route plan must SET the selected skill")
    if (
        plan.outcome_disposition is OutcomeDisposition.APPLIED
        and outcome.result_type is OutcomeResultType.REROUTE
    ):
        update = plan.selected_skill_update
        if update is None or update.action is not FieldUpdateAction.CLEAR:
            raise ValueError("REROUTE plan must CLEAR the selected skill")
    if (
        plan.outcome_disposition is OutcomeDisposition.APPLIED
        and isinstance(payload, ReviewAssessment)
        and payload.verdict is ReviewVerdict.PASS
    ):
        mutation = plan.candidate_mutation
        expected_target = (
            payload.candidate_conclusion_id,
            payload.candidate_revision,
            payload.candidate_content_hash,
        )
        actual_target = (
            None
            if mutation is None
            or mutation.candidate_binding.existing_candidate_target is None
            else (
                mutation.candidate_binding.existing_candidate_target.candidate_conclusion_id,
                mutation.candidate_binding.existing_candidate_target.candidate_revision,
                mutation.candidate_binding.existing_candidate_target.candidate_content_hash,
            )
        )
        final_target = (
            None
            if plan.final_result_target is None
            else (
                plan.final_result_target.candidate_conclusion_id,
                plan.final_result_target.candidate_revision,
                plan.final_result_target.candidate_content_hash,
            )
        )
        if (
            mutation is None
            or mutation.target_status is not CandidateStatus.ACCEPTED
            or actual_target != expected_target
            or final_target != expected_target
        ):
            raise ValueError("PASS plan must accept the fixed candidate")
    return plan


__all__ = [
    "AgentArtifactProposalDraft",
    "AgentEvidenceProposalDraft",
    "AgentJobOutcome",
    "ArtifactProposal",
    "AssetUnavailableTriggerPayload",
    "CancelCaseTriggerPayload",
    "CandidateConclusionDraft",
    "CandidateMutation",
    "CaseFailureUpdate",
    "CaseSnapshot",
    "CompletionCriterionDraftMapping",
    "CoordinatorPlanResult",
    "CreateCaseTriggerPayload",
    "DiagnosisItemChange",
    "DiagnosisItemDraft",
    "DiagnosisOutcome",
    "DiagnosisOutcomeTriggerPayload",
    "DiagnosisStateDelta",
    "EvidenceBinding",
    "EvidenceProposal",
    "EvidenceSourceBinding",
    "ExecutionFailedTriggerPayload",
    "Finding",
    "JobOutcome",
    "Job",
    "JobSpec",
    "OldEpochTriggerPayload",
    "OutcomePayload",
    "PlannedResourceBinding",
    "PROBLEM_SPEC_PATCH_FIELDS",
    "ProblemSpec",
    "ProblemSpecPatch",
    "RequirementFulfillment",
    "ResumeInterruptedTriggerPayload",
    "ReviewAssessment",
    "ReviewOutcomeTriggerPayload",
    "ReviewTargetBinding",
    "RouteDecision",
    "RouteOutcomeTriggerPayload",
    "RuntimeBindings",
    "SelectedSkillUpdate",
    "StaleActiveOutcomeTriggerPayload",
    "SubmitSupplementTriggerPayload",
    "TransitionPlan",
    "TriggerPayload",
    "UserResultPayload",
    "ValidatedTrigger",
    "apply_problem_spec_patch",
    "coordinator_outcome_error_failure",
    "validate_logparse_claim_for_job",
    "validate_coordinator_plan_result",
    "validate_outcome_for_job",
    "validate_outcome_resources_for_job",
    "validate_transition_plan_for_outcome",
    "validate_user_result_for_outcome",
    "validate_user_result_resolution",
]
