"""Outcome, trigger, and transition-plan views of the public contract models."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .enums import (
    ArtifactKind,
    AttachmentStatus,
    CandidateStatus,
    DiagnosisMode,
    DiagnosisResolutionStatus,
    ErrorCode,
    EvidenceSourceType,
    FieldUpdateAction,
    JobType,
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
    GenericDiagnosisOutcome,
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
    UserResultPayloadV3,
    UserFactEvidenceLocator,
    ValidatedTrigger,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    review_required_evidence_refs,
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


class UserResultValidationError(ValueError):
    """Content-free USER_RESULT invariant category for runtime DFX."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


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
        for factor in (
            candidate.causal_factors
            + candidate.candidate_factors
            + candidate.excluded_factors
        ):
            bindings.extend(factor.evidence_bindings)
    return bindings


def _candidate_evidence_bindings(
    candidate: CandidateConclusionDraft | None,
) -> list[EvidenceBinding]:
    if candidate is None:
        return []
    bindings = list(candidate.supporting_evidence_bindings)
    for mapping in candidate.completion_criteria_mapping:
        bindings.extend(mapping.evidence_bindings)
    for factor in (
        candidate.causal_factors
        + candidate.candidate_factors
        + candidate.excluded_factors
    ):
        bindings.extend(factor.evidence_bindings)
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
    if job.diagnosis_mode is DiagnosisMode.GENERIC:
        proposed_evidence = (
            outcome.proposed_evidence_drafts
            if isinstance(outcome, AgentJobOutcome)
            else outcome.proposed_evidence
        )
        proposed_artifacts = (
            outcome.proposed_artifact_drafts
            if isinstance(outcome, AgentJobOutcome)
            else outcome.proposed_artifacts
        )
        if (
            isinstance(outcome, AgentJobOutcome)
            or job.context_snapshot is not None
            or outcome.consumed_evidence_refs
            or proposed_evidence
            or proposed_artifacts
            or outcome.decision_audit is not None
        ):
            raise ValueError(
                "GENERIC Job and Outcome must remain isolated from specialized context"
            )
        if outcome.result_type is OutcomeResultType.FAILED:
            return outcome
        if (
            outcome.result_type is not OutcomeResultType.COMPLETED
            or not isinstance(payload, GenericDiagnosisOutcome)
            or payload.skill_name != job.generic_skill_name
        ):
            raise ValueError(
                "GENERIC Job requires a matching generic diagnosis Outcome"
            )
        return outcome

    audit = outcome.decision_audit
    if audit is not None:
        if audit.skill_ref != job.skill_ref:
            raise ValueError("decision audit Skill must equal the pinned Job Skill")
        proposal_keys = {
            item.proposal_key
            for item in (
                outcome.proposed_evidence_drafts
                if isinstance(outcome, AgentJobOutcome)
                else outcome.proposed_evidence
            )
        }
        for binding in audit.required_evidence_bindings:
            if (
                binding.existing_evidence_id is not None
                and binding.existing_evidence_id not in job_evidence
            ) or (
                binding.evidence_proposal_key is not None
                and binding.evidence_proposal_key not in proposal_keys
            ):
                raise ValueError(
                    "decision audit Evidence must be fixed by the Job or Outcome"
                )
        if job.job_type is JobType.REVIEW:
            required_existing = [
                item.existing_evidence_id
                for item in audit.required_evidence_bindings
            ]
            candidate = job.context_snapshot.candidate_conclusion
            if candidate is None:
                raise ValueError("REVIEW Job requires its Candidate snapshot")
            candidate_required = set(review_required_evidence_refs(candidate))
            if set(required_existing) != candidate_required:
                raise ValueError(
                    "REVIEW decision audit must cover required Candidate Evidence"
                )
            if required_existing != [
                ref for ref in job.evidence_refs if ref in candidate_required
            ]:
                raise ValueError(
                    "REVIEW decision audit Evidence must preserve the fixed Job order"
                )

    if (
        job.job_type is JobType.DIAGNOSE
        and outcome.result_type is not OutcomeResultType.FAILED
    ):
        if isinstance(payload, GenericDiagnosisOutcome):
            raise ValueError(
                "SPECIALIZED Job cannot publish a generic diagnosis Outcome"
            )
    if isinstance(payload, RouteDecision):
        if payload.kind is RouteKind.MATCHED and payload.skill_ref not in job.available_skill_refs:
            raise ValueError("MATCHED route skill_ref must belong to Job.available_skill_refs")

    if isinstance(payload, ReviewAssessment):
        assert job.context_snapshot is not None
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
        if payload.reviewed_evidence_refs != outcome.consumed_evidence_refs:
            raise ValueError(
                "REVIEW reviewed_evidence_refs must exactly equal consumed_evidence_refs"
            )
        reviewed_set = set(payload.reviewed_evidence_refs)
        if payload.reviewed_evidence_refs != [
            ref for ref in job.evidence_refs if ref in reviewed_set
        ]:
            raise ValueError(
                "REVIEW Evidence references must preserve the fixed Job order"
            )
        candidate = job.context_snapshot.candidate_conclusion
        if (
            payload.verdict is ReviewVerdict.PASS
            and candidate is not None
            and any(
                ref not in reviewed_set
                for ref in review_required_evidence_refs(candidate)
            )
        ):
            raise ValueError("PASS must review every required candidate Evidence")

    if isinstance(payload, DiagnosisOutcome):
        assert job.context_snapshot is not None
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
    assert job.context_snapshot is not None
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
) -> UserResultPayloadV3:
    """Validate the canonical server-final USER_RESULT v3 representation."""

    try:
        parsed_result = parse_canonical_json_bytes(result_bytes)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise UserResultValidationError(
            "canonical",
            "USER_RESULT bytes are not canonical",
        ) from None
    try:
        result = UserResultPayloadV3.model_validate(parsed_result)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise UserResultValidationError(
            "schema",
            "USER_RESULT does not satisfy its schema",
        ) from None
    payload = outcome.payload
    candidate = payload.candidate_conclusion_draft if isinstance(payload, DiagnosisOutcome) else None
    candidate_result = (
        outcome.job_type is JobType.DIAGNOSE
        and outcome.result_type is OutcomeResultType.COMPLETED
        and candidate is not None
    )
    unresolved_result = outcome.result_type is OutcomeResultType.INCONCLUSIVE or (
        outcome.job_type is JobType.REVIEW
        and outcome.result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, ReviewAssessment)
        and payload.verdict is not ReviewVerdict.PASS
    )
    if not candidate_result and not unresolved_result:
        raise UserResultValidationError(
            "outcome_branch",
            "this Outcome branch cannot publish USER_RESULT",
        )
    expected = {
        "source_job_type": outcome.job_type,
        "problem_statement": job.context_snapshot.problem_spec.statement,
        "status": (
            "COMPLETED"
            if candidate_result
            and candidate is not None
            and candidate.resolution_status is DiagnosisResolutionStatus.COMPLETE
            else "PARTIAL"
            if candidate_result
            else "INCONCLUSIVE"
        ),
        "recommendations": [
            payload.recommended_next_step
            if isinstance(payload, DiagnosisOutcome)
            else payload.recommendation
        ],
    }
    if candidate_result:
        assert candidate is not None
        expected.update(
            root_cause=(
                candidate.statement
                if candidate.resolution_status is DiagnosisResolutionStatus.COMPLETE
                else None
            ),
            supporting_evidence_bindings=candidate.supporting_evidence_bindings,
            completion_criteria_mapping=candidate.completion_criteria_mapping,
        )
    elif result.root_cause is not None:
        raise UserResultValidationError(
            "root_cause_mismatch",
            "an unresolved USER_RESULT must have root_cause=null",
        )
    for field_name, value in expected.items():
        if getattr(result, field_name) != value:
            raise UserResultValidationError(
                f"{field_name}_mismatch",
                f"USER_RESULT {field_name} must exactly match the server-final Outcome seam",
            )
    if candidate_result:
        assert isinstance(payload, DiagnosisOutcome)
        assert candidate is not None
        for report_values, draft_values in (
            (result.causal_factors, candidate.causal_factors),
            (result.candidate_factors, candidate.candidate_factors),
            (result.excluded_factors, candidate.excluded_factors),
        ):
            if len(report_values) != len(draft_values) or any(
                (
                    report.factor_id,
                    report.role,
                    report.statement,
                    report.evidence_bindings,
                    report.required_rule_ids,
                )
                != (
                    draft.factor_id,
                    draft.role,
                    draft.statement,
                    draft.evidence_bindings,
                    draft.required_rule_ids,
                )
                for report, draft in zip(report_values, draft_values, strict=True)
            ):
                raise UserResultValidationError(
                    "factors_mismatch",
                    "USER_RESULT factors must exactly reflect the Diagnosis Candidate",
                )
        expected_findings = [
            (item.statement, item.confidence, item.evidence_bindings)
            for item in payload.findings
        ]
        actual_findings = [
            (item.statement, item.confidence, item.evidence_bindings)
            for item in result.findings
        ]
        if actual_findings != expected_findings:
            raise UserResultValidationError(
                "findings_mismatch",
                "USER_RESULT findings must exactly reflect the Diagnosis Outcome",
            )
    audit = outcome.decision_audit
    if audit is None:
        raise UserResultValidationError(
            "decision_audit_missing",
            "USER_RESULT requires the server-final decision audit",
        )
    expected_rules = [
        (
            item.rule_id,
            item.server_evaluation.rule_kind,
            item.server_evaluation.status,
            item.server_evaluation.evidence_bindings,
            item.server_evaluation.observed_times,
            item.server_evaluation.event_observations,
            item.server_evaluation.derived_values,
            item.server_evaluation.issues,
        )
        for item in audit.rules
    ]
    actual_rules = [
        (
            item.rule_id,
            item.rule_kind,
            item.status,
            item.evidence_bindings,
            item.observed_times,
            item.event_observations,
            item.derived_values,
            item.issues,
        )
        for item in result.verification_rules
    ]
    if actual_rules != expected_rules:
        raise UserResultValidationError(
            "verification_rules_mismatch",
            "USER_RESULT verification_rules must exactly reflect the server decision audit",
        )
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
        raise UserResultValidationError(
            "artifact_count",
            "candidate Outcome requires exactly one USER_RESULT Artifact",
        )
    artifact = user_results[0]
    actual_size = len(result_bytes)
    actual_sha256 = hashlib.sha256(result_bytes).hexdigest()
    if isinstance(outcome, AgentJobOutcome):
        if artifact.declared_size is not None and artifact.declared_size != actual_size:
            raise UserResultValidationError(
                "declared_size_mismatch",
                "USER_RESULT declared_size does not match canonical bytes",
            )
        if (
            artifact.declared_sha256 is not None
            and artifact.declared_sha256 != actual_sha256
        ):
            raise UserResultValidationError(
                "declared_sha256_mismatch",
                "USER_RESULT declared_sha256 does not match canonical bytes",
            )
    elif artifact.size != actual_size or artifact.sha256 != actual_sha256:
        raise UserResultValidationError(
            "published_artifact_mismatch",
            "USER_RESULT ArtifactProposal must match canonical bytes",
        )
    return result


def validate_user_result_resolution(
    result: UserResultPayloadV3,
    final_candidate: CandidateConclusion,
    evidence_ids_by_proposal: Mapping[str, str],
) -> CandidateConclusion:
    """Validate complete or partial v3 bindings after proposal formalization."""

    expected_status = (
        "COMPLETED"
        if final_candidate.resolution_status
        is DiagnosisResolutionStatus.COMPLETE
        else "PARTIAL"
    )
    if result.status != expected_status:
        raise ValueError("USER_RESULT status must match the final Candidate")
    if expected_status == "COMPLETED" and result.root_cause is None:
        raise ValueError("a completed USER_RESULT requires root_cause")
    if expected_status == "PARTIAL" and result.root_cause is not None:
        raise ValueError("a partial USER_RESULT must not claim a complete root cause")

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
        (
            result.root_cause != final_candidate.statement
            if expected_status == "COMPLETED"
            else result.root_cause is not None
        )
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
            or draft_mapping.status is not final_mapping.status
            or draft_mapping.explanation != final_mapping.explanation
            or [resolve(binding) for binding in draft_mapping.evidence_bindings]
            != final_mapping.evidence_refs
        ):
            raise ValueError("resolved USER_RESULT mapping does not match the final Candidate")

    def validate_factors(result_factors, candidate_factors) -> None:
        if len(result_factors) != len(candidate_factors):
            raise ValueError("resolved USER_RESULT factor count does not match Candidate")
        for report_factor, candidate_factor in zip(
            result_factors, candidate_factors, strict=True
        ):
            if (
                report_factor.factor_id != candidate_factor.factor_id
                or report_factor.role is not candidate_factor.role
                or report_factor.statement != candidate_factor.statement
                or report_factor.required_rule_ids
                != candidate_factor.required_rule_ids
                or [resolve(binding) for binding in report_factor.evidence_bindings]
                != candidate_factor.evidence_refs
            ):
                raise ValueError(
                    "resolved USER_RESULT factor does not match the final Candidate"
                )

    validate_factors(result.causal_factors, final_candidate.causal_factors)
    validate_factors(result.candidate_factors, final_candidate.candidate_factors)
    validate_factors(result.excluded_factors, final_candidate.excluded_factors)
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
    archive_keys = {
        proposal.proposal_key
        for proposal in artifacts
        if proposal.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE
    }
    if plan.accepted_candidate_proposal_key is not None:
        if candidate is None or plan.accepted_candidate_proposal_key != candidate.proposal_key:
            raise ValueError("TransitionPlan accepts an unknown candidate proposal")
        if len(user_result_keys) != 1 or not user_result_keys <= accepted_artifacts:
            raise ValueError("an accepted candidate requires its unique USER_RESULT Artifact")
        if len(archive_keys) != 1 or not archive_keys <= accepted_artifacts:
            raise ValueError(
                "an accepted candidate requires its USER_RESULT_ARCHIVE Artifact"
            )
        for binding in _candidate_evidence_bindings(candidate):
            if (
                binding.evidence_proposal_key is not None
                and binding.evidence_proposal_key not in accepted_evidence
            ):
                raise ValueError("accepted candidate bindings require accepted Evidence proposals")
    elif plan.unresolved_result_draft is not None:
        user_result_key = plan.unresolved_result_draft.user_result_proposal_key
        if (
            user_result_keys != {user_result_key}
            or user_result_key not in accepted_artifacts
            or archive_keys
        ):
            raise ValueError(
                "an unresolved result must accept its unique USER_RESULT and forbid an archive"
            )
    elif (user_result_keys | archive_keys) & accepted_artifacts:
        raise ValueError(
            "user result Artifacts cannot be accepted without a Candidate or unresolved result"
        )

    for binding in _delta_evidence_bindings(plan.accepted_state_delta):
        if (
            binding.evidence_proposal_key is not None
            and binding.evidence_proposal_key not in accepted_evidence
        ):
            raise ValueError("accepted state delta requires accepted Evidence proposals")
    if plan.unresolved_result_draft is not None:
        for binding in plan.unresolved_result_draft.evidence_bindings:
            if (
                binding.evidence_proposal_key is not None
                and binding.evidence_proposal_key not in accepted_evidence
            ):
                raise ValueError(
                    "unresolved audit bindings require accepted Evidence proposals"
                )

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
        and isinstance(payload, RouteDecision)
        and payload.kind is RouteKind.NO_CAPABILITY
    ):
        update = plan.selected_skill_update
        if update is None or update.action is not FieldUpdateAction.CLEAR:
            raise ValueError("NO_CAPABILITY route plan must CLEAR the selected skill")
    if isinstance(payload, GenericDiagnosisOutcome):
        result = plan.generic_result
        if (
            plan.outcome_disposition is not OutcomeDisposition.APPLIED
            or result is None
            or result.status is not payload.status
            or result.conclusion != payload.conclusion
            or result.root_cause_analysis != payload.root_cause_analysis
            or result.skill_name != payload.skill_name
            or result.source_job_id != outcome.job_id
            or result.source_outcome_id != outcome.outcome_id
            or result.occurred_at != outcome.produced_at
        ):
            raise ValueError(
                "generic diagnosis plan must directly bind the complete Outcome result"
            )
    elif plan.generic_result is not None:
        raise ValueError("only a generic diagnosis Outcome may install generic_result")
    if (
        plan.outcome_disposition is OutcomeDisposition.APPLIED
        and outcome.result_type is OutcomeResultType.REROUTE
    ):
        update = plan.selected_skill_update
        if update is None or update.action is not FieldUpdateAction.CLEAR:
            raise ValueError("REROUTE plan must CLEAR the selected skill")
    if (
        plan.outcome_disposition is OutcomeDisposition.APPLIED
        and outcome.result_type is OutcomeResultType.COMPLETED
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
    "GenericDiagnosisOutcome",
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
    "UserResultPayloadV3",
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
