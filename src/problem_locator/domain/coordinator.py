"""Deterministic, side-effect-free Problem Locator domain coordination."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ArtifactKind,
    ArtifactProposal,
    AttachmentRequirementConstraints,
    AssetUnavailableTriggerPayload,
    CancelCaseTriggerPayload,
    CandidateConclusionDraft,
    CandidateMutation,
    CandidateMutationAction,
    CandidateStatus,
    CandidateTarget,
    CaseFailure,
    CaseFailureUpdate,
    CaseSnapshot,
    CaseStatus,
    CoordinatorPlanResult,
    CreateCaseTriggerPayload,
    DiagnosisItem,
    DiagnosisItemChange,
    DiagnosisItemDraft,
    DiagnosisMode,
    DiagnosisResolutionStatus,
    DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisState,
    DiagnosisStateDelta,
    ErrorCode,
    EvidenceBinding,
    EvidenceProposal,
    ExecutionFailedTriggerPayload,
    ExecutionFailure,
    FieldUpdateAction,
    GenericDiagnosisOutcome,
    GenericDiagnosisOutcomeV2,
    GenericResult,
    GenericResultV2Draft,
    GenericResultStatus,
    InputRequirementConstraints,
    Job,
    JobLifecycleUpdate,
    JobOutcome,
    JobSpec,
    JobStatus,
    JobType,
    MethodsReviewTargetV2,
    OldEpochTriggerPayload,
    OutcomeDisposition,
    OutcomeResultType,
    PendingRequirement,
    PlannedResourceBinding,
    ProblemSpecPatch,
    RequirementFulfillment,
    RequirementKind,
    RequirementStatus,
    ResumeInterruptedTriggerPayload,
    ReviewAssessment,
    ReviewPolicy,
    ReviewOutcomeTriggerPayload,
    ReviewTargetBinding,
    ReviewVerdict,
    RuleClaimResult,
    RouteDecision,
    RouteKind,
    RouteOutcomeTriggerPayload,
    RuntimeBindings,
    ServerRuleStatus,
    SelectedSkillUpdate,
    StaleActiveOutcomeTriggerPayload,
    SubmitSupplementTriggerPayload,
    SupplementPolicy,
    TransitionPlan,
    TriggerType,
    UnresolvedReasonCode,
    UnresolvedResultDraft,
    ValidatedTrigger,
    VersionedRef,
    apply_problem_spec_patch,
    validate_coordinator_plan_result,
)


_FATAL_FAILURE_CODES = frozenset(
    {
        ErrorCode.CONTEXT_LIMIT,
        ErrorCode.ASSET_VERSION_UNAVAILABLE,
        ErrorCode.BACKEND_OUTPUT_LIMIT,
        ErrorCode.OUTCOME_MISSING,
        ErrorCode.OUTCOME_INVALID,
        ErrorCode.WORKSPACE_LIMIT,
        ErrorCode.LOGPARSE_OUTPUT_INVALID,
        ErrorCode.CONFIG_INVALID,
        ErrorCode.RESOURCE_NOT_FOUND,
        ErrorCode.RESOURCE_HASH_MISMATCH,
        ErrorCode.RESOURCE_SIZE_MISMATCH,
        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
        ErrorCode.PATH_VIOLATION,
    }
)

_CONDITIONAL_FAILURE_CODES = frozenset(
    {
        ErrorCode.BACKEND_START_FAILED,
        ErrorCode.BACKEND_CANCELLED,
        ErrorCode.BACKEND_TIMEOUT,
        ErrorCode.BACKEND_EXIT_FAILED,
        ErrorCode.WORKSPACE_PREPARE_FAILED,
        ErrorCode.RESOURCE_STAGE_FAILED,
        ErrorCode.EXECUTION_RECORD_FAILED,
        ErrorCode.LOGPARSE_FAILED,
    }
)

_STABLE_TARGET_FIELDS = frozenset(
    {
        "statement",
        "expected_behavior",
        "scope",
        "goals",
        "completion_criteria",
    }
)

_ROUTE_GOAL = "Select a diagnosis skill for the fixed problem."
_DIAGNOSE_GOAL = "Diagnose the fixed problem using the selected skill."
_GENERIC_DIAGNOSE_GOAL = "Diagnose the raw problem with the configured generic Skill."
_SUPPLEMENT_GOAL = "Continue diagnosis with the accepted supplement."
_REVIEW_GOAL = "Review the fixed candidate against all supporting evidence."


def _empty_delta() -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def _detail(
    *,
    field: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    expected: str | int | bool | None = None,
    actual: str | int | bool | None = None,
) -> ApplicationErrorDetail:
    return ApplicationErrorDetail(
        field=field,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_ref=None,
        expected=expected,
        actual=actual,
        limit=None,
        observed=None,
    )


def _error(
    code: ErrorCode,
    message: str,
    details: Iterable[ApplicationErrorDetail] = (),
) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=message,
        details=list(details),
        retryable=False,
    )


def _invalid_state(
    status: CaseStatus,
    trigger_type: TriggerType,
) -> ApplicationError:
    return _error(
        ErrorCode.INVALID_CASE_STATE,
        "The trigger is not valid for the current Case state.",
        [
            _detail(
                field="trigger_type",
                expected="a trigger allowed by the S01 state table",
                actual=f"{status.value}:{trigger_type.value}",
            )
        ],
    )


def _validation(
    message: str,
    *,
    field: str | None = None,
    expected: str | int | bool | None = None,
    actual: str | int | bool | None = None,
) -> ApplicationError:
    details = (
        []
        if field is None
        else [_detail(field=field, expected=expected, actual=actual)]
    )
    return _error(ErrorCode.VALIDATION_ERROR, message, details)


def _existing_bindings(ids: Iterable[str]) -> list[PlannedResourceBinding]:
    return [
        PlannedResourceBinding(
            existing_resource_id=resource_id,
            accepted_proposal_key=None,
        )
        for resource_id in ids
    ]


def _proposal_binding(key: str) -> PlannedResourceBinding:
    return PlannedResourceBinding(
        existing_resource_id=None,
        accepted_proposal_key=key,
    )


def _evidence_binding_key(binding: EvidenceBinding) -> str:
    if binding.existing_evidence_id is not None:
        return f"existing:{binding.existing_evidence_id}"
    assert binding.evidence_proposal_key is not None
    return f"proposal:{binding.evidence_proposal_key}"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _user_result_proposal_key(outcome: JobOutcome) -> str | None:
    keys = [
        proposal.proposal_key
        for proposal in outcome.proposed_artifacts
        if proposal.artifact_kind is ArtifactKind.USER_RESULT
    ]
    return keys[0] if len(keys) == 1 else None


def _dedupe_evidence_bindings(
    values: Iterable[EvidenceBinding],
) -> list[EvidenceBinding]:
    seen: set[str] = set()
    result: list[EvidenceBinding] = []
    for value in values:
        key = _evidence_binding_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _runtime_from_job(job: Job) -> RuntimeBindings:
    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
        review_policy=job.review_policy,
        generic_skill_name=job.generic_skill_name,
        agent_profile_ref=job.agent_profile_ref,
        available_skill_refs=job.available_skill_refs,
        skill_ref=job.skill_ref,
        tool_bundle_ref=job.tool_bundle_ref,
        context_policy_ref=job.context_policy_ref,
        output_contract_ref=job.output_contract_ref,
        logparse_tool_ref=job.logparse_tool_ref,
        logparse_product=job.logparse_product,
        resource_limits=job.resource_limits,
    )


def _job_update(
    job: Job,
    target_status: JobStatus,
    occurred_at: str,
) -> JobLifecycleUpdate:
    return JobLifecycleUpdate(
        job_id=job.job_id,
        expected_status=job.status,
        target_status=target_status,
        started_at=None,
        finished_at=occurred_at,
        runtime_epoch=None,
    )


def _candidate_target(job: Job) -> CandidateTarget | None:
    return job.review_target


def _blocking_rule_ids(outcome: JobOutcome) -> list[str]:
    audit = outcome.decision_audit
    if audit is None:
        return []
    blocked = [
        item.rule_id
        for item in audit.rules
        if item.server_evaluation.status
        in {ServerRuleStatus.VERIFIED_FAIL, ServerRuleStatus.UNVERIFIABLE}
        or (
            item.agent_claim is not None
            and item.agent_claim.claimed_result
            in {RuleClaimResult.FAIL, RuleClaimResult.UNKNOWN}
        )
    ]
    if blocked:
        return _dedupe(blocked)
    # An INCONCLUSIVE semantic-only rule is still a named blocker even though
    # the service intentionally does not pretend to machine-prove causality.
    semantic = [
        item.rule_id
        for item in audit.rules
        if item.server_evaluation.status is ServerRuleStatus.SEMANTIC_ONLY
    ]
    return semantic or list(audit.required_rule_ids)


def _audit_evidence_bindings(outcome: JobOutcome) -> list[EvidenceBinding]:
    audit = outcome.decision_audit
    if audit is None:
        return []
    bindings = list(audit.required_evidence_bindings)
    for item in audit.rules:
        bindings.extend(item.server_evaluation.evidence_bindings)
    return _dedupe_evidence_bindings(bindings)


def _inconclusive_reason(outcome: JobOutcome) -> UnresolvedReasonCode:
    audit = outcome.decision_audit
    if audit is not None and any(
        item.server_evaluation.status is ServerRuleStatus.VERIFIED_FAIL
        for item in audit.rules
    ):
        return UnresolvedReasonCode.MECHANICAL_VERIFICATION_FAILED
    return UnresolvedReasonCode.INSUFFICIENT_EVIDENCE


class DomainCoordinator:
    """Compute complete transition plans solely from frozen DTO values."""

    def plan(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        payload = trigger.payload
        if (
            isinstance(payload, SubmitSupplementTriggerPayload)
            and payload.stable_target_changed
        ):
            return validate_coordinator_plan_result(
                trigger,
                _error(
                    ErrorCode.NEW_CASE_REQUIRED,
                    "The validated supplement changes the stable diagnosis target.",
                    [
                        _detail(
                            field="stable_target_changed",
                            expected=False,
                            actual=True,
                        )
                    ],
                ),
            )
        if snapshot.case.case_id != trigger.case_id:
            return validate_coordinator_plan_result(
                trigger,
                _validation(
                    "The Trigger belongs to a different Case.",
                    field="case_id",
                    expected=snapshot.case.case_id,
                    actual=trigger.case_id,
                ),
            )
        if (
            trigger.trigger_type is not TriggerType.CREATE_CASE
            and snapshot.case.case_revision != trigger.expected_case_revision
        ):
            return validate_coordinator_plan_result(
                trigger,
                _validation(
                    "The Trigger Case revision does not match the snapshot.",
                    field="expected_case_revision",
                    expected=snapshot.case.case_revision,
                    actual=trigger.expected_case_revision,
                ),
            )

        handler = {
            TriggerType.CREATE_CASE: self._create_case,
            TriggerType.ROUTE_OUTCOME: self._route_outcome,
            TriggerType.DIAGNOSIS_OUTCOME: self._diagnosis_outcome,
            TriggerType.REVIEW_OUTCOME: self._review_outcome,
            TriggerType.SUBMIT_SUPPLEMENT: self._submit_supplement,
            TriggerType.CANCEL_CASE: self._cancel_case,
            TriggerType.RESUME_INTERRUPTED: self._resume_interrupted,
            TriggerType.EXECUTION_FAILED: self._execution_failed,
            TriggerType.ASSET_VERSION_UNAVAILABLE: self._asset_unavailable,
            TriggerType.MARK_OLD_EPOCH_INTERRUPTED: self._old_epoch,
            TriggerType.STALE_ACTIVE_OUTCOME: self._stale_active_outcome,
        }[trigger.trigger_type]
        return validate_coordinator_plan_result(
            trigger,
            handler(snapshot, trigger),
        )

    def _create_case(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        case = snapshot.case
        if case.status is not CaseStatus.NEW:
            return _invalid_state(case.status, trigger.trigger_type)
        if snapshot.active_job is not None:
            return _error(
                ErrorCode.ACTIVE_JOB_EXISTS,
                "A new Case cannot already have an active Job.",
            )
        payload = trigger.payload
        assert isinstance(payload, CreateCaseTriggerPayload)
        state = case.diagnosis_state
        if (
            case.case_id != trigger.case_id
            or case.case_revision != 1
            or state.revision != 1
            or state.problem_spec != payload.problem_spec
            or state.user_facts != payload.initial_user_facts
            or state.confirmed_facts
            or state.active_hypotheses
            or state.rejected_hypotheses
            or state.open_questions
            or state.pending_requirements
            or state.evidence_refs
            or state.candidate_conclusion is not None
            or case.selected_skill_ref is not None
            or case.final_result is not None
            or case.unresolved_result is not None
            or case.generic_result is not None
            or case.generic_result_v2 is not None
            or case.failure is not None
            or case.raw_problem_text != payload.raw_problem_text
        ):
            return _validation(
                "The transient NEW snapshot does not match the normalized CreateCase payload."
            )
        next_job = self._job_spec(
            trigger,
            JobType.ROUTE,
            target_state_revision=1,
            goal=_ROUTE_GOAL,
            evidence_bindings=[],
            attachment_refs=[],
            previous_outcome_refs=[],
            artifact_bindings=[],
            selected_skill_ref=None,
        )
        if isinstance(next_job, ApplicationError):
            return next_job
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.RUNNING,
            job_updates=[],
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=next_job,
            final_result_target=None,
            clear_active_job=False,
            reason="Create the Case and its initial ROUTE Job.",
        )

    def _route_outcome(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        active = self._active_job(snapshot, trigger, JobType.ROUTE, JobStatus.RUNNING)
        if isinstance(active, ApplicationError):
            return active
        payload = trigger.payload
        assert isinstance(payload, RouteOutcomeTriggerPayload)
        outcome_error = self._validate_active_outcome(active, payload.job_outcome)
        if outcome_error is not None:
            return outcome_error
        outcome = payload.job_outcome
        if outcome.proposed_evidence or outcome.proposed_artifacts:
            return _validation("ROUTE Outcomes cannot propose Evidence or Artifacts.")
        if outcome.result_type is OutcomeResultType.FAILED:
            assert outcome.error is not None
            return self._failure_plan(
                active,
                outcome.error,
                trigger,
                source_outcome_id=outcome.outcome_id,
                disposition=OutcomeDisposition.APPLIED,
            )
        decision = outcome.payload
        assert isinstance(decision, RouteDecision)
        if decision.kind is RouteKind.NO_CAPABILITY:
            next_job = self._job_spec(
                trigger,
                JobType.DIAGNOSE,
                target_state_revision=snapshot.case.diagnosis_state.revision,
                goal=_GENERIC_DIAGNOSE_GOAL,
                evidence_bindings=[],
                attachment_refs=[],
                previous_outcome_refs=[],
                artifact_bindings=[],
                selected_skill_ref=None,
                generic_problem_text=snapshot.case.raw_problem_text,
            )
            if isinstance(next_job, ApplicationError):
                return next_job
            return TransitionPlan(
                accepted_state_delta=_empty_delta(),
                target_case_status=CaseStatus.RUNNING,
                job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=[],
                accepted_artifact_proposal_keys=[],
                accepted_candidate_proposal_key=None,
                selected_skill_update=SelectedSkillUpdate(
                    action=FieldUpdateAction.CLEAR,
                    value=None,
                ),
                case_failure_update=None,
                candidate_mutation=None,
                next_job_spec=next_job,
                final_result_target=None,
                clear_active_job=True,
                reason="No specialized capability matched; start generic diagnosis.",
            )

        assert decision.skill_ref is not None
        continuation = trigger.continuation_resources
        next_job = self._job_spec(
            trigger,
            JobType.DIAGNOSE,
            target_state_revision=snapshot.case.diagnosis_state.revision,
            goal=_DIAGNOSE_GOAL,
            evidence_bindings=_existing_bindings(continuation.evidence_refs),
            attachment_refs=continuation.attachment_refs,
            previous_outcome_refs=continuation.previous_outcome_refs,
            artifact_bindings=_existing_bindings(continuation.artifact_refs),
            selected_skill_ref=decision.skill_ref,
        )
        if isinstance(next_job, ApplicationError):
            return next_job
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.RUNNING,
            job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
            outcome_disposition=OutcomeDisposition.APPLIED,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=SelectedSkillUpdate(
                action=FieldUpdateAction.SET,
                value=decision.skill_ref,
            ),
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=next_job,
            final_result_target=None,
            clear_active_job=True,
            reason="Apply the fixed route decision and start diagnosis.",
        )

    def _diagnosis_outcome(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        active = self._active_job(snapshot, trigger, JobType.DIAGNOSE, JobStatus.RUNNING)
        if isinstance(active, ApplicationError):
            return active
        payload = trigger.payload
        assert isinstance(payload, DiagnosisOutcomeTriggerPayload)
        outcome_error = self._validate_active_outcome(active, payload.job_outcome)
        if outcome_error is not None:
            return outcome_error
        outcome = payload.job_outcome
        if outcome.methods_terminal_projection is not None:
            return self._methods_terminal_plan(snapshot, active, outcome, trigger)
        if outcome.result_type is OutcomeResultType.FAILED:
            assert outcome.error is not None
            return self._failure_plan(
                active,
                outcome.error,
                trigger,
                source_outcome_id=outcome.outcome_id,
                disposition=OutcomeDisposition.APPLIED,
            )
        if active.diagnosis_mode is DiagnosisMode.GENERIC:
            generic = outcome.payload
            if (
                not isinstance(
                    generic, (GenericDiagnosisOutcome, GenericDiagnosisOutcomeV2)
                )
                or generic.skill_name != active.generic_skill_name
                or outcome.result_type is not OutcomeResultType.COMPLETED
                or outcome.consumed_evidence_refs
                or outcome.proposed_evidence
                or outcome.proposed_artifacts
                or outcome.decision_audit is not None
            ):
                return _validation(
                    "A GENERIC DIAGNOSE Outcome must be the isolated generic result."
                )
            terminal_status = CaseStatus(generic.status.value)
            common = dict(
                accepted_state_delta=_empty_delta(),
                target_case_status=terminal_status,
                job_updates=[
                    _job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)
                ],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=[],
                accepted_artifact_proposal_keys=[],
                accepted_candidate_proposal_key=None,
                selected_skill_update=SelectedSkillUpdate(
                    action=FieldUpdateAction.CLEAR,
                    value=None,
                ),
                case_failure_update=None,
                candidate_mutation=None,
                next_job_spec=None,
                final_result_target=None,
                clear_active_job=True,
                reason="Apply the generic diagnosis result directly without review.",
            )
            if isinstance(generic, GenericDiagnosisOutcomeV2):
                return TransitionPlan(
                    **common,
                    generic_result_v2_draft=GenericResultV2Draft(
                        format_version=generic.format_version,
                        status=GenericResultStatus(generic.status.value),
                        report_markdown=generic.report_markdown,
                        report_utf8_size=generic.report_utf8_size,
                        report_sha256=generic.report_sha256,
                        skill_name=generic.skill_name,
                        source_job_id=active.job_id,
                        source_outcome_id=outcome.outcome_id,
                        occurred_at=outcome.produced_at,
                    ),
                )
            return TransitionPlan(
                **common,
                generic_result=GenericResult(
                    status=GenericResultStatus(generic.status.value),
                    conclusion=generic.conclusion,
                    root_cause_analysis=generic.root_cause_analysis,
                    skill_name=generic.skill_name,
                    source_job_id=active.job_id,
                    source_outcome_id=outcome.outcome_id,
                    occurred_at=outcome.produced_at,
                ),
            )
        if active.diagnosis_mode is not DiagnosisMode.SPECIALIZED:
            return _validation("A DIAGNOSE Job must have a frozen diagnosis mode.")
        diagnosis = outcome.payload
        assert isinstance(diagnosis, DiagnosisOutcome)
        candidate = diagnosis.candidate_conclusion_draft
        if candidate is not None and outcome.result_type is not OutcomeResultType.COMPLETED:
            return _validation("Only a COMPLETED diagnosis can propose a Candidate.")
        if outcome.result_type is OutcomeResultType.REROUTE and candidate is not None:
            return _validation("REROUTE cannot carry a Candidate.")

        candidate_bindings = self._candidate_bindings(candidate)
        if outcome.result_type is OutcomeResultType.INCONCLUSIVE:
            candidate_bindings = _audit_evidence_bindings(outcome)
        normalized = self._normalize_delta(
            snapshot.case.diagnosis_state,
            active,
            outcome,
            diagnosis.state_delta,
            candidate_bindings,
        )
        if isinstance(normalized, ApplicationError):
            return normalized
        accepted_delta, semantic_change, evidence_keys, dependency_artifact_keys = normalized

        target_revision = snapshot.case.diagnosis_state.revision + int(
            semantic_change or candidate is not None
        )
        if outcome.result_type is OutcomeResultType.INCONCLUSIVE:
            user_result_key = _user_result_proposal_key(outcome)
            if user_result_key is None:
                return _validation(
                    "An unresolved diagnosis requires its server-generated USER_RESULT."
                )
            return TransitionPlan(
                accepted_state_delta=accepted_delta,
                target_case_status=CaseStatus.UNRESOLVED,
                job_updates=[
                    _job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)
                ],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=evidence_keys,
                accepted_artifact_proposal_keys=_dedupe(
                    [*dependency_artifact_keys, user_result_key]
                ),
                accepted_candidate_proposal_key=None,
                selected_skill_update=None,
                case_failure_update=None,
                candidate_mutation=None,
                next_job_spec=None,
                final_result_target=None,
                unresolved_result_draft=UnresolvedResultDraft(
                    source_job_id=active.job_id,
                    source_outcome_id=outcome.outcome_id,
                    reason_code=_inconclusive_reason(outcome),
                    summary=(
                        "The available facts and logs do not establish a "
                        "complete, verifiable diagnosis."
                    ),
                    blocking_rule_ids=_blocking_rule_ids(outcome),
                    evidence_bindings=_audit_evidence_bindings(outcome),
                    recommended_next_step=diagnosis.recommended_next_step,
                    user_result_proposal_key=user_result_key,
                    occurred_at=trigger.occurred_at,
                ),
                clear_active_job=True,
                reason=(
                    "Close the Case without a final result because the "
                    "server verification gate did not establish a diagnosis."
                ),
            )
        if outcome.result_type is OutcomeResultType.NEED_INPUT:
            request_error = self._validate_requested_requirements(
                snapshot.case.diagnosis_state,
                active,
                accepted_delta,
                diagnosis.requested_input,
                RequirementKind.INPUT,
            )
            if request_error is not None:
                return request_error
            return self._diagnosis_wait_plan(
                active,
                trigger,
                accepted_delta,
                evidence_keys,
                dependency_artifact_keys,
                CaseStatus.WAITING_INPUT,
                "The Specialist requires structured user input.",
            )
        if outcome.result_type is OutcomeResultType.NEED_ATTACHMENT:
            request_error = self._validate_requested_requirements(
                snapshot.case.diagnosis_state,
                active,
                accepted_delta,
                diagnosis.requested_attachments,
                RequirementKind.ATTACHMENT,
            )
            if request_error is not None:
                return request_error
            return self._diagnosis_wait_plan(
                active,
                trigger,
                accepted_delta,
                evidence_keys,
                dependency_artifact_keys,
                CaseStatus.WAITING_ATTACHMENT,
                "The Specialist requires a READY Attachment.",
            )

        if outcome.result_type is OutcomeResultType.REROUTE:
            if not semantic_change:
                return _validation(
                    "A reroute diagnosis must make semantic progress before routing again."
                )
            next_job = self._job_spec(
                trigger,
                JobType.ROUTE,
                target_state_revision=target_revision,
                goal=diagnosis.recommended_next_step,
                evidence_bindings=[],
                attachment_refs=[],
                previous_outcome_refs=[outcome.outcome_id],
                artifact_bindings=[],
                selected_skill_ref=None,
            )
            if isinstance(next_job, ApplicationError):
                return next_job
            return TransitionPlan(
                accepted_state_delta=accepted_delta,
                target_case_status=CaseStatus.RUNNING,
                job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=evidence_keys,
                accepted_artifact_proposal_keys=dependency_artifact_keys,
                accepted_candidate_proposal_key=None,
                selected_skill_update=SelectedSkillUpdate(
                    action=FieldUpdateAction.CLEAR,
                    value=None,
                ),
                case_failure_update=None,
                candidate_mutation=None,
                next_job_spec=next_job,
                final_result_target=None,
                clear_active_job=True,
                reason="Apply diagnosis progress and reroute from the fixed Outcome.",
            )

        if candidate is not None:
            candidate_error = self._validate_candidate(
                snapshot.case.diagnosis_state,
                accepted_delta,
                candidate,
                outcome,
            )
            if candidate_error is not None:
                return candidate_error
            user_result_keys = [
                proposal.proposal_key
                for proposal in outcome.proposed_artifacts
                if proposal.artifact_kind is ArtifactKind.USER_RESULT
            ]
            if len(user_result_keys) != 1:
                return _validation(
                    "An accepted Candidate requires exactly one USER_RESULT Artifact."
                )
            archive_keys = [
                proposal.proposal_key
                for proposal in outcome.proposed_artifacts
                if proposal.artifact_kind is ArtifactKind.USER_RESULT_ARCHIVE
            ]
            if len(archive_keys) != 1:
                return _validation(
                    "An accepted Candidate requires exactly one USER_RESULT_ARCHIVE Artifact."
                )
            artifact_keys = _dedupe(
                [*dependency_artifact_keys, user_result_keys[0], *archive_keys]
            )
            candidate_binding = ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key=candidate.proposal_key,
            )
            if active.review_policy is ReviewPolicy.NONE:
                target_case_status = (
                    CaseStatus.RESOLVED
                    if candidate.resolution_status
                    is DiagnosisResolutionStatus.COMPLETE
                    else CaseStatus.PARTIALLY_RESOLVED
                )
                return TransitionPlan(
                    accepted_state_delta=accepted_delta,
                    target_case_status=target_case_status,
                    job_updates=[
                        _job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)
                    ],
                    outcome_disposition=OutcomeDisposition.APPLIED,
                    accepted_evidence_proposal_keys=evidence_keys,
                    accepted_artifact_proposal_keys=artifact_keys,
                    accepted_candidate_proposal_key=candidate.proposal_key,
                    selected_skill_update=None,
                    case_failure_update=None,
                    candidate_mutation=CandidateMutation(
                        action=CandidateMutationAction.INSTALL,
                        candidate_binding=candidate_binding,
                        expected_status=None,
                        target_status=CandidateStatus.ACCEPTED,
                        reason=None,
                    ),
                    next_job_spec=None,
                    final_result_target=candidate_binding,
                    clear_active_job=True,
                    reason=(
                        "Accept and publish the server-verified Candidate because the "
                        "frozen review policy is NONE."
                    ),
                )
            if active.review_policy is not ReviewPolicy.INDEPENDENT:
                return _validation(
                    "A specialized DIAGNOSE Job has no supported frozen review policy."
                )
            review_evidence = [
                *_existing_bindings(snapshot.case.diagnosis_state.evidence_refs),
                *[_proposal_binding(key) for key in evidence_keys],
            ]
            review_resources = self._continuation_with_proposals(
                trigger,
                evidence_keys,
                dependency_artifact_keys,
            )
            next_job = self._job_spec(
                trigger,
                JobType.REVIEW,
                target_state_revision=target_revision,
                goal=_REVIEW_GOAL,
                evidence_bindings=review_evidence,
                attachment_refs=review_resources[1],
                previous_outcome_refs=[outcome.outcome_id],
                artifact_bindings=review_resources[2],
                selected_skill_ref=snapshot.case.selected_skill_ref,
                review_target_binding=candidate_binding,
            )
            if isinstance(next_job, ApplicationError):
                return next_job
            return TransitionPlan(
                accepted_state_delta=accepted_delta,
                target_case_status=CaseStatus.REVIEWING,
                job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=evidence_keys,
                accepted_artifact_proposal_keys=artifact_keys,
                accepted_candidate_proposal_key=candidate.proposal_key,
                selected_skill_update=None,
                case_failure_update=None,
                candidate_mutation=CandidateMutation(
                    action=CandidateMutationAction.INSTALL,
                    candidate_binding=candidate_binding,
                    expected_status=None,
                    target_status=CandidateStatus.REVIEWING,
                    reason=None,
                ),
                next_job_spec=next_job,
                final_result_target=None,
                clear_active_job=True,
                reason="Install the evidence-backed Candidate and start independent review.",
            )

        if not semantic_change:
            return _validation(
                "A completed diagnosis without a Candidate must make semantic progress."
            )
        resources = self._continuation_with_proposals(
            trigger,
            evidence_keys,
            dependency_artifact_keys,
        )
        next_job = self._job_spec(
            trigger,
            JobType.DIAGNOSE,
            target_state_revision=target_revision,
            goal=diagnosis.recommended_next_step,
            evidence_bindings=resources[0],
            attachment_refs=resources[1],
            previous_outcome_refs=trigger.continuation_resources.previous_outcome_refs,
            artifact_bindings=resources[2],
            selected_skill_ref=snapshot.case.selected_skill_ref,
        )
        if isinstance(next_job, ApplicationError):
            return next_job
        return TransitionPlan(
            accepted_state_delta=accepted_delta,
            target_case_status=CaseStatus.RUNNING,
            job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
            outcome_disposition=OutcomeDisposition.APPLIED,
            accepted_evidence_proposal_keys=evidence_keys,
            accepted_artifact_proposal_keys=dependency_artifact_keys,
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=next_job,
            final_result_target=None,
            clear_active_job=True,
            reason="Apply semantic progress and continue diagnosis in a new Job.",
        )

    def _methods_terminal_plan(
        self,
        snapshot: CaseSnapshot,
        active: Job,
        outcome: JobOutcome,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        """Apply only the already-validated public Methods terminal projection."""

        terminal = outcome.methods_terminal_projection
        if terminal is None:
            return _validation(
                "Methods V2 terminal processing requires its server projection."
            )
        if (
            snapshot.case.diagnosis_state.candidate_conclusion is not None
            or active.context_snapshot is None
            or active.context_snapshot.candidate_conclusion is not None
            or (
                active.job_type is JobType.DIAGNOSE
                and active.diagnosis_mode is not DiagnosisMode.SPECIALIZED
            )
            or (
                active.job_type is JobType.REVIEW
                and active.methods_review_target is None
            )
        ):
            return _validation(
                "Methods V2 terminal transition must remain Candidate-free."
            )
        if terminal.status == "FAILED":
            if outcome.error is None:
                return _validation(
                    "A failed Methods V2 terminal Outcome requires its mapped failure."
                )
            failure_plan = self._failure_plan(
                active,
                outcome.error,
                trigger,
                source_outcome_id=outcome.outcome_id,
                disposition=OutcomeDisposition.APPLIED,
            )
            if isinstance(failure_plan, ApplicationError):
                return failure_plan
            values = failure_plan.model_dump(mode="python")
            values["methods_terminal_projection"] = terminal
            return TransitionPlan.model_validate(values)

        target_status = (
            CaseStatus.RESOLVED
            if terminal.status == "RESOLVED"
            else CaseStatus.UNRESOLVED
        )
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=target_status,
            job_updates=[
                _job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)
            ],
            outcome_disposition=OutcomeDisposition.APPLIED,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            methods_terminal_projection=terminal,
            clear_active_job=True,
            reason="Apply the Evidence V2 terminal projection without Candidate state.",
        )

    def _review_outcome(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        active = self._active_job(snapshot, trigger, JobType.REVIEW, JobStatus.RUNNING)
        if isinstance(active, ApplicationError):
            return active
        payload = trigger.payload
        assert isinstance(payload, ReviewOutcomeTriggerPayload)
        outcome_error = self._validate_active_outcome(active, payload.job_outcome)
        if outcome_error is not None:
            return outcome_error
        outcome = payload.job_outcome
        if outcome.methods_terminal_projection is not None:
            return self._methods_terminal_plan(snapshot, active, outcome, trigger)
        if outcome.result_type is OutcomeResultType.FAILED:
            assert outcome.error is not None
            return self._failure_plan(
                active,
                outcome.error,
                trigger,
                source_outcome_id=outcome.outcome_id,
                disposition=OutcomeDisposition.APPLIED,
            )
        if active.methods_review_target is not None:
            return _validation(
                "Methods V2 REVIEW Outcome requires its server terminal projection."
            )
        if outcome.proposed_evidence or any(
            proposal.artifact_kind is not ArtifactKind.USER_RESULT
            for proposal in outcome.proposed_artifacts
        ):
            return _validation(
                "REVIEW Outcomes may contain only the server-generated USER_RESULT."
            )
        assessment = outcome.payload
        assert isinstance(assessment, ReviewAssessment)
        user_result_key = _user_result_proposal_key(outcome)
        unresolved_review = (
            outcome.result_type is OutcomeResultType.INCONCLUSIVE
            or assessment.verdict is not ReviewVerdict.PASS
        )
        if unresolved_review != (user_result_key is not None):
            return _validation(
                "Only an unresolved REVIEW may carry its server-generated USER_RESULT."
            )
        target = _candidate_target(active)
        if target is None:
            return _validation("A REVIEW Job must have a fixed CandidateTarget.")
        candidate = snapshot.case.diagnosis_state.candidate_conclusion
        if (
            candidate is None
            or candidate.status is not CandidateStatus.REVIEWING
            or candidate.conclusion_id != target.candidate_conclusion_id
            or candidate.revision != target.candidate_revision
            or candidate.content_hash != target.candidate_content_hash
        ):
            return _validation("The current REVIEWING Candidate does not match the Job target.")
        update = _job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)
        if outcome.result_type is OutcomeResultType.INCONCLUSIVE:
            return self._review_unresolved_plan(
                active,
                outcome,
                assessment,
                target,
                update,
                trigger,
                reason_code=_inconclusive_reason(outcome),
                summary=(
                    "The available evidence did not pass the server verification "
                    "gate for this reviewed candidate."
                ),
            )
        if assessment.verdict is ReviewVerdict.PASS:
            target_case_status = (
                CaseStatus.RESOLVED
                if candidate.resolution_status
                is DiagnosisResolutionStatus.COMPLETE
                else CaseStatus.PARTIALLY_RESOLVED
            )
            mutation = CandidateMutation(
                action=CandidateMutationAction.SET_STATUS,
                candidate_binding=ReviewTargetBinding(
                    existing_candidate_target=target,
                    accepted_candidate_proposal_key=None,
                ),
                expected_status=CandidateStatus.REVIEWING,
                target_status=CandidateStatus.ACCEPTED,
                reason=None,
            )
            return TransitionPlan(
                accepted_state_delta=_empty_delta(),
                target_case_status=target_case_status,
                job_updates=[update],
                outcome_disposition=OutcomeDisposition.APPLIED,
                accepted_evidence_proposal_keys=[],
                accepted_artifact_proposal_keys=[],
                accepted_candidate_proposal_key=None,
                selected_skill_update=None,
                case_failure_update=None,
                candidate_mutation=mutation,
                next_job_spec=None,
                final_result_target=target,
                clear_active_job=True,
                reason=(
                    "Accept the fixed Candidate at its declared resolution status "
                    "after an independent PASS review."
                ),
            )
        if assessment.verdict is ReviewVerdict.NEED_MORE_EVIDENCE:
            requested = assessment.requested_requirement_ids
            requirements = snapshot.case.diagnosis_state.pending_requirements
            by_id = {item.requirement_id: item for item in requirements}
            requirement = by_id.get(requested[0]) if len(requested) == 1 else None
            if (
                requirement is not None
                and requirement.status is RequirementStatus.OPEN
                and requirement.supplement_policy is SupplementPolicy.MISSING_ONLY
            ):
                target_status = (
                    CaseStatus.WAITING_INPUT
                    if requirement.kind is RequirementKind.INPUT
                    else CaseStatus.WAITING_ATTACHMENT
                )
                return TransitionPlan(
                    accepted_state_delta=_empty_delta(),
                    target_case_status=target_status,
                    job_updates=[update],
                    outcome_disposition=OutcomeDisposition.APPLIED,
                    accepted_evidence_proposal_keys=[],
                    accepted_artifact_proposal_keys=[],
                    accepted_candidate_proposal_key=None,
                    selected_skill_update=None,
                    case_failure_update=None,
                    candidate_mutation=CandidateMutation(
                        action=CandidateMutationAction.SET_STATUS,
                        candidate_binding=ReviewTargetBinding(
                            existing_candidate_target=target,
                            accepted_candidate_proposal_key=None,
                        ),
                        expected_status=CandidateStatus.REVIEWING,
                        target_status=CandidateStatus.REJECTED,
                        reason=assessment.recommendation,
                    ),
                    next_job_spec=None,
                    final_result_target=None,
                    clear_active_job=True,
                    reason=(
                        "Reject the Candidate and wait only for the explicitly "
                        "declared missing supplement."
                    ),
                )
            return self._review_unresolved_plan(
                active,
                outcome,
                assessment,
                target,
                update,
                trigger,
                reason_code=UnresolvedReasonCode.INVALID_NEED_MORE_REQUEST,
                summary=(
                    "The review requested more information without a single "
                    "eligible Skill-declared missing requirement."
                ),
            )

        return self._review_unresolved_plan(
            active,
            outcome,
            assessment,
            target,
            update,
            trigger,
            reason_code=UnresolvedReasonCode.SEMANTIC_REVIEW_REJECTED,
            summary=(
                "Independent review did not confirm the proposed diagnosis "
                "under the current facts and evidence."
            ),
        )

    def _review_unresolved_plan(
        self,
        active: Job,
        outcome: JobOutcome,
        assessment: ReviewAssessment,
        target: CandidateTarget,
        update: JobLifecycleUpdate,
        trigger: ValidatedTrigger,
        *,
        reason_code: UnresolvedReasonCode,
        summary: str,
    ) -> TransitionPlan:
        user_result_key = _user_result_proposal_key(outcome)
        if user_result_key is None:
            raise ValueError(
                "unresolved REVIEW has no server-generated USER_RESULT"
            )
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.UNRESOLVED,
            job_updates=[update],
            outcome_disposition=OutcomeDisposition.APPLIED,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[user_result_key],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=CandidateMutation(
                action=CandidateMutationAction.SET_STATUS,
                candidate_binding=ReviewTargetBinding(
                    existing_candidate_target=target,
                    accepted_candidate_proposal_key=None,
                ),
                expected_status=CandidateStatus.REVIEWING,
                target_status=CandidateStatus.REJECTED,
                reason=assessment.recommendation,
            ),
            next_job_spec=None,
            final_result_target=None,
            unresolved_result_draft=UnresolvedResultDraft(
                source_job_id=active.job_id,
                source_outcome_id=outcome.outcome_id,
                reason_code=reason_code,
                summary=summary,
                blocking_rule_ids=_blocking_rule_ids(outcome),
                evidence_bindings=_audit_evidence_bindings(outcome),
                recommended_next_step=assessment.recommendation,
                user_result_proposal_key=user_result_key,
                occurred_at=trigger.occurred_at,
            ),
            clear_active_job=True,
            reason=(
                "Reject the reviewed Candidate and close the Case without "
                "automatically starting another diagnosis."
            ),
        )

    def _submit_supplement(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        case = snapshot.case
        if case.status not in {
            CaseStatus.WAITING_INPUT,
            CaseStatus.WAITING_ATTACHMENT,
        }:
            return _invalid_state(case.status, trigger.trigger_type)
        if snapshot.active_job is not None:
            return _error(
                ErrorCode.ACTIVE_JOB_EXISTS,
                "A waiting Case cannot accept supplements while a Job is active.",
            )
        payload = trigger.payload
        assert isinstance(payload, SubmitSupplementTriggerPayload)
        state = case.diagnosis_state
        open_requirements = {
            requirement.requirement_id: requirement
            for requirement in state.pending_requirements
            if requirement.status is RequirementStatus.OPEN
        }
        open_inputs_by_name = {
            requirement.name: requirement
            for requirement in open_requirements.values()
            if requirement.kind is RequirementKind.INPUT
        }
        existing_item_ids = {
            item.item_id
            for item in (
                state.user_facts
                + state.confirmed_facts
                + state.active_hypotheses
                + state.rejected_hypotheses
                + state.open_questions
            )
        }
        fixed_input_names = {
            item.provenance.input_name for item in state.user_facts
        }
        for fact in payload.user_facts:
            name = fact.provenance.input_name
            if name in fixed_input_names:
                return _error(
                    ErrorCode.NEW_CASE_REQUIRED,
                    "A user fact already fixed in this Case cannot be corrected.",
                    [
                        _detail(
                            field="user_facts.provenance.input_name",
                            expected="a previously missing input name",
                            actual=name,
                        )
                    ],
                )
        if any(
            requirement.supplement_policy is not SupplementPolicy.MISSING_ONLY
            for requirement in open_requirements.values()
        ):
            return _validation(
                "A waiting Case may accept only Skill-declared MISSING_ONLY requirements."
            )
        fulfillments: list[RequirementFulfillment] = []
        accepted_facts: list[DiagnosisItem] = []
        fulfilled_ids: set[str] = set()
        for fact in payload.user_facts:
            name = fact.provenance.input_name
            requirement = None if name is None else open_inputs_by_name.get(name)
            if requirement is None:
                return _validation(
                    "Supplement input does not match an OPEN INPUT requirement.",
                    field="user_facts.provenance.input_name",
                    expected="an OPEN INPUT requirement name",
                    actual=name,
                )
            if fact.item_id in existing_item_ids:
                return _validation(
                    "Supplement fact IDs must be new within the DiagnosisState.",
                    field="user_facts.item_id",
                    expected="a new Diagnosis item ID",
                    actual=fact.item_id,
                )
            if fact.created_revision != state.revision + 1:
                return _validation(
                    "Supplement facts must be created at the target DiagnosisState revision.",
                    field="user_facts.created_revision",
                    expected=state.revision + 1,
                    actual=fact.created_revision,
                )
            constraints = requirement.constraints
            assert isinstance(constraints, InputRequirementConstraints)
            encoded_length = len(fact.statement.encode("utf-8"))
            if not (
                constraints.min_utf8_bytes
                <= encoded_length
                <= constraints.max_utf8_bytes
            ):
                return _validation(
                    "Supplement input violates its UTF-8 byte constraints.",
                    field="user_facts.statement",
                    expected=(
                        f"{constraints.min_utf8_bytes}.."
                        f"{constraints.max_utf8_bytes} UTF-8 bytes"
                    ),
                    actual=encoded_length,
                )
            if (
                constraints.pattern is not None
                and re.fullmatch(constraints.pattern, fact.statement) is None
            ):
                return _validation(
                    "Supplement input violates its required pattern.",
                    field="user_facts.statement",
                    expected=constraints.pattern,
                    actual=fact.statement,
                )
            if (
                constraints.allowed_values
                and fact.statement not in constraints.allowed_values
            ):
                return _validation(
                    "Supplement input is not one of the allowed values.",
                    field="user_facts.statement",
                    expected="one of the requirement allowed_values",
                    actual=fact.statement,
                )
            accepted_facts.append(fact)
            fulfilled_ids.add(requirement.requirement_id)
            fulfillments.append(
                RequirementFulfillment(
                    requirement_id=requirement.requirement_id,
                    fulfilled_by_refs=[fact.item_id],
                )
            )

        if payload.ready_attachment_ids:
            attachment_requirements = [
                requirement
                for requirement in open_requirements.values()
                if requirement.kind is RequirementKind.ATTACHMENT
            ]
            if len(attachment_requirements) != 1:
                return _validation(
                    "Supplement Attachments require exactly one OPEN ATTACHMENT requirement."
                )
            requirement = attachment_requirements[0]
            constraints = requirement.constraints
            assert isinstance(constraints, AttachmentRequirementConstraints)
            attachment_count = len(payload.ready_attachment_ids)
            if not constraints.min_count <= attachment_count <= constraints.max_count:
                return _validation(
                    "Supplement Attachments violate the required count constraints.",
                    field="ready_attachment_ids",
                    expected=f"{constraints.min_count}..{constraints.max_count}",
                    actual=attachment_count,
                )
            fulfilled_ids.add(requirement.requirement_id)
            fulfillments.append(
                RequirementFulfillment(
                    requirement_id=requirement.requirement_id,
                    fulfilled_by_refs=payload.ready_attachment_ids,
                )
            )

        if not accepted_facts and not payload.ready_attachment_ids:
            return _validation("A supplement must satisfy at least one current requirement.")
        remaining = [
            requirement
            for requirement in state.pending_requirements
            if requirement.status is RequirementStatus.OPEN
            and requirement.required
            and requirement.requirement_id not in fulfilled_ids
        ]
        remaining_inputs = [
            requirement
            for requirement in remaining
            if requirement.kind is RequirementKind.INPUT
        ]
        remaining_attachments = [
            requirement
            for requirement in remaining
            if requirement.kind is RequirementKind.ATTACHMENT
        ]
        accepted_delta = DiagnosisStateDelta(
            problem_spec_patch=None,
            add_user_facts=sorted(accepted_facts, key=lambda item: item.item_id),
            proposed_facts=[],
            add_active_hypotheses=[],
            update_hypotheses=[],
            reject_hypotheses=[],
            add_open_questions=[],
            resolve_questions=[],
            add_pending_requirements=[],
            fulfill_requirements=sorted(
                fulfillments,
                key=lambda item: item.requirement_id,
            ),
            add_evidence_bindings=[],
        )
        if remaining_inputs:
            return self._supplement_wait_plan(
                accepted_delta,
                CaseStatus.WAITING_INPUT,
                "Retain the accepted supplement while required inputs remain open.",
            )
        if remaining_attachments:
            return self._supplement_wait_plan(
                accepted_delta,
                CaseStatus.WAITING_ATTACHMENT,
                "Retain the accepted supplement while the Attachment requirement remains open.",
            )
        continuation = trigger.continuation_resources
        next_job = self._job_spec(
            trigger,
            JobType.DIAGNOSE,
            target_state_revision=state.revision + 1,
            goal=_SUPPLEMENT_GOAL,
            evidence_bindings=_existing_bindings(continuation.evidence_refs),
            attachment_refs=continuation.attachment_refs,
            previous_outcome_refs=continuation.previous_outcome_refs,
            artifact_bindings=_existing_bindings(continuation.artifact_refs),
            selected_skill_ref=case.selected_skill_ref,
        )
        if isinstance(next_job, ApplicationError):
            return next_job
        return TransitionPlan(
            accepted_state_delta=accepted_delta,
            target_case_status=CaseStatus.RUNNING,
            job_updates=[],
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=next_job,
            final_result_target=None,
            clear_active_job=False,
            reason="Fulfill all current requirements and continue diagnosis once.",
        )

    def _cancel_case(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        case = snapshot.case
        if case.status is CaseStatus.CANCELLED:
            return _invalid_state(case.status, trigger.trigger_type)
        if case.status in {
            CaseStatus.NEW,
            CaseStatus.RESOLVED,
            CaseStatus.PARTIALLY_RESOLVED,
            CaseStatus.UNRESOLVED,
            CaseStatus.FAILED,
        }:
            return _invalid_state(case.status, trigger.trigger_type)
        payload = trigger.payload
        assert isinstance(payload, CancelCaseTriggerPayload)
        active = snapshot.active_job
        if (active is None) != (payload.active_job_id is None):
            return _validation("CancelCase active_job_id does not match the Case snapshot.")
        if active is not None:
            if payload.active_job_id != active.job_id or active.status not in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
            }:
                return _validation("CancelCase must target the current active Job.")
            updates = [_job_update(active, JobStatus.CANCELLED, trigger.occurred_at)]
        else:
            updates = []
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.CANCELLED,
            job_updates=updates,
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=active is not None,
            reason="Cancel the non-terminal Case and any current active Job.",
        )

    def _resume_interrupted(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        if snapshot.case.status is not CaseStatus.INTERRUPTED:
            return _invalid_state(snapshot.case.status, trigger.trigger_type)
        payload = trigger.payload
        assert isinstance(payload, ResumeInterruptedTriggerPayload)
        source = snapshot.resume_source_job
        if source is None or source.job_id != payload.source_job_id:
            return _validation("ResumeInterrupted must target the unresolved source Job.")
        if source.job_id in snapshot.replacement_job_ids_by_source:
            return _error(
                ErrorCode.ACTIVE_JOB_EXISTS,
                "The interrupted Job already has a replacement.",
            )
        selected_skill = snapshot.case.selected_skill_ref
        if (
            source.job_type is JobType.ROUTE
            and selected_skill is not None
        ) or (
            source.job_type is JobType.DIAGNOSE
            and source.diagnosis_mode is DiagnosisMode.GENERIC
            and selected_skill is not None
        ) or (
            source.job_type is not JobType.ROUTE
            and source.diagnosis_mode is not DiagnosisMode.GENERIC
            and selected_skill != source.skill_ref
        ):
            return _validation(
                "The interrupted source Job does not match the Case selected Skill."
            )
        if source.job_type is JobType.REVIEW:
            if source.methods_review_target is not None:
                if snapshot.case.diagnosis_state.candidate_conclusion is not None:
                    return _validation(
                        "An interrupted Methods V2 REVIEW must remain Candidate-free."
                    )
            else:
                target = source.review_target
                candidate = snapshot.case.diagnosis_state.candidate_conclusion
                if (
                    target is None
                    or candidate is None
                    or candidate.status is not CandidateStatus.REVIEWING
                    or candidate.conclusion_id != target.candidate_conclusion_id
                    or candidate.revision != target.candidate_revision
                    or candidate.content_hash != target.candidate_content_hash
                ):
                    return _validation(
                        "The interrupted REVIEW target no longer matches the current Candidate."
                    )
        binding = trigger.runtime_bindings_by_job_type.get(source.job_type)
        if binding is None or binding != _runtime_from_job(source):
            return _validation(
                "ResumeInterrupted RuntimeBindings must exactly equal the source Job bindings."
            )
        review_binding = (
            None
            if source.review_target is None
            else ReviewTargetBinding(
                existing_candidate_target=source.review_target,
                accepted_candidate_proposal_key=None,
            )
        )
        next_job = self._job_spec(
            trigger,
            source.job_type,
            target_state_revision=snapshot.case.diagnosis_state.revision,
            goal=source.goal,
            evidence_bindings=_existing_bindings(source.evidence_refs),
            attachment_refs=source.attachment_refs,
            previous_outcome_refs=source.previous_outcome_refs,
            artifact_bindings=_existing_bindings(source.artifact_refs),
            selected_skill_ref=source.skill_ref,
            review_target_binding=review_binding,
            methods_review_target=source.methods_review_target,
            replacement_for_job_id=source.job_id,
            generic_problem_text=source.generic_problem_text,
        )
        if isinstance(next_job, ApplicationError):
            return next_job
        target_status = (
            CaseStatus.REVIEWING
            if source.job_type is JobType.REVIEW
            else CaseStatus.RUNNING
        )
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=target_status,
            job_updates=[],
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=next_job,
            final_result_target=None,
            clear_active_job=False,
            reason="Create the unique same-stage replacement for the interrupted Job.",
        )

    def _execution_failed(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        if snapshot.case.status not in {CaseStatus.RUNNING, CaseStatus.REVIEWING}:
            return _invalid_state(snapshot.case.status, trigger.trigger_type)
        payload = trigger.payload
        assert isinstance(payload, ExecutionFailedTriggerPayload)
        active = snapshot.active_job
        if (
            active is None
            or active.job_id != payload.source_job_id
            or active.status is not JobStatus.RUNNING
        ):
            return _validation("Execution failure must target the current RUNNING Job.")
        return self._failure_plan(
            active,
            payload.execution_failure,
            trigger,
            source_outcome_id=payload.source_outcome_id,
            disposition=None,
        )

    def _asset_unavailable(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        payload = trigger.payload
        assert isinstance(payload, AssetUnavailableTriggerPayload)
        case = snapshot.case
        updates: list[JobLifecycleUpdate]
        clear_active: bool
        if case.status in {CaseStatus.RUNNING, CaseStatus.REVIEWING}:
            active = snapshot.active_job
            if (
                active is None
                or active.job_id != payload.source_job_id
                or active.status is not JobStatus.PENDING
            ):
                return _validation(
                    "Asset unavailability for an active Case must target its PENDING Job."
                )
            updates = [_job_update(active, JobStatus.FAILED, trigger.occurred_at)]
            clear_active = True
        elif case.status is CaseStatus.INTERRUPTED:
            source = snapshot.resume_source_job
            if source is None or source.job_id != payload.source_job_id:
                return _validation(
                    "Asset unavailability for an interrupted Case must target its resume source."
                )
            updates = []
            clear_active = False
        else:
            return _invalid_state(case.status, trigger.trigger_type)
        failure = CaseFailure(
            code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
            message="A fixed runtime asset version is unavailable.",
            source_job_id=payload.source_job_id,
            source_outcome_id=None,
            occurred_at=trigger.occurred_at,
        )
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.FAILED,
            job_updates=updates,
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=CaseFailureUpdate(
                action=FieldUpdateAction.SET,
                value=failure,
            ),
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=clear_active,
            reason="Fail the Case because a fixed runtime asset cannot be loaded.",
        )

    def _old_epoch(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        if snapshot.case.status not in {CaseStatus.RUNNING, CaseStatus.REVIEWING}:
            return _invalid_state(snapshot.case.status, trigger.trigger_type)
        payload = trigger.payload
        assert isinstance(payload, OldEpochTriggerPayload)
        active = snapshot.active_job
        if (
            active is None
            or active.job_id != payload.source_job_id
            or active.status is not JobStatus.RUNNING
            or active.runtime_epoch != payload.previous_runtime_epoch
        ):
            return _validation("Old-epoch interruption must target the current RUNNING Job.")
        return self._interrupt_plan(
            active,
            trigger,
            disposition=None,
            reason="Interrupt the RUNNING Job from the previous runtime epoch.",
        )

    def _stale_active_outcome(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> CoordinatorPlanResult:
        if snapshot.case.status not in {CaseStatus.RUNNING, CaseStatus.REVIEWING}:
            return _invalid_state(snapshot.case.status, trigger.trigger_type)
        payload = trigger.payload
        assert isinstance(payload, StaleActiveOutcomeTriggerPayload)
        active = snapshot.active_job
        if (
            active is None
            or active.job_id != payload.source_job_id
            or active.status is not JobStatus.RUNNING
            or active.base_state_revision != payload.expected_base_state_revision
            or snapshot.case.diagnosis_state.revision != payload.actual_state_revision
        ):
            return _validation("Stale-active interruption must match the active Job drift.")
        return self._interrupt_plan(
            active,
            trigger,
            disposition=OutcomeDisposition.STALE,
            reason="Interrupt the active Job and preserve the stale Outcome as audit only.",
        )

    def _active_job(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
        expected_type: JobType,
        expected_status: JobStatus,
    ) -> Job | ApplicationError:
        expected_case_status = (
            CaseStatus.REVIEWING
            if expected_type is JobType.REVIEW
            else CaseStatus.RUNNING
        )
        if snapshot.case.status is not expected_case_status:
            return _invalid_state(snapshot.case.status, trigger.trigger_type)
        active = snapshot.active_job
        if active is None:
            return _validation("The Case has no active Job for this Trigger.")
        if active.job_type is not expected_type or active.status is not expected_status:
            return _validation(
                "The active Job type or status does not match the Trigger.",
                field="active_job",
                expected=f"{expected_type.value}:{expected_status.value}",
                actual=f"{active.job_type.value}:{active.status.value}",
            )
        return active

    def _validate_active_outcome(
        self,
        active: Job,
        outcome: JobOutcome,
    ) -> ApplicationError | None:
        if (
            outcome.job_id != active.job_id
            or outcome.case_id != active.case_id
            or outcome.job_type is not active.job_type
            or outcome.base_state_revision != active.base_state_revision
        ):
            return _validation("The Outcome does not match the current active Job.")
        return None

    def _failure_plan(
        self,
        job: Job,
        failure: ExecutionFailure,
        trigger: ValidatedTrigger,
        *,
        source_outcome_id: str | None,
        disposition: OutcomeDisposition | None,
    ) -> CoordinatorPlanResult:
        if failure.code not in _FATAL_FAILURE_CODES | _CONDITIONAL_FAILURE_CODES:
            return _validation(
                "This error code is not a valid Agent ExecutionFailure.",
                field="execution_failure.code",
                expected="an S01 fatal or conditional execution code",
                actual=failure.code.value,
            )
        interrupted = (
            failure.code in _CONDITIONAL_FAILURE_CODES and failure.retryable
        )
        target_case_status = (
            CaseStatus.INTERRUPTED if interrupted else CaseStatus.FAILED
        )
        target_job_status = JobStatus.INTERRUPTED if interrupted else JobStatus.FAILED
        failure_update = None
        if not interrupted:
            failure_update = CaseFailureUpdate(
                action=FieldUpdateAction.SET,
                value=CaseFailure(
                    code=failure.code,
                    message=failure.message,
                    source_job_id=job.job_id,
                    source_outcome_id=source_outcome_id,
                    occurred_at=trigger.occurred_at,
                    reason_code=failure.reason_code,
                    diagnostic_id=failure.diagnostic_id,
                ),
            )
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=target_case_status,
            job_updates=[_job_update(job, target_job_status, trigger.occurred_at)],
            outcome_disposition=disposition,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=failure_update,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=True,
            reason=(
                "Interrupt the Case after a retryable execution failure."
                if interrupted
                else "Fail the Case after a fatal or non-retryable execution failure."
            ),
        )

    def _interrupt_plan(
        self,
        job: Job,
        trigger: ValidatedTrigger,
        *,
        disposition: OutcomeDisposition | None,
        reason: str,
    ) -> TransitionPlan:
        return TransitionPlan(
            accepted_state_delta=_empty_delta(),
            target_case_status=CaseStatus.INTERRUPTED,
            job_updates=[_job_update(job, JobStatus.INTERRUPTED, trigger.occurred_at)],
            outcome_disposition=disposition,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=True,
            reason=reason,
        )

    def _supplement_wait_plan(
        self,
        accepted_delta: DiagnosisStateDelta,
        target_status: CaseStatus,
        reason: str,
    ) -> TransitionPlan:
        return TransitionPlan(
            accepted_state_delta=accepted_delta,
            target_case_status=target_status,
            job_updates=[],
            outcome_disposition=None,
            accepted_evidence_proposal_keys=[],
            accepted_artifact_proposal_keys=[],
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=False,
            reason=reason,
        )

    def _diagnosis_wait_plan(
        self,
        active: Job,
        trigger: ValidatedTrigger,
        accepted_delta: DiagnosisStateDelta,
        evidence_keys: list[str],
        artifact_keys: list[str],
        target_status: CaseStatus,
        reason: str,
    ) -> TransitionPlan:
        return TransitionPlan(
            accepted_state_delta=accepted_delta,
            target_case_status=target_status,
            job_updates=[_job_update(active, JobStatus.SUCCEEDED, trigger.occurred_at)],
            outcome_disposition=OutcomeDisposition.APPLIED,
            accepted_evidence_proposal_keys=evidence_keys,
            accepted_artifact_proposal_keys=artifact_keys,
            accepted_candidate_proposal_key=None,
            selected_skill_update=None,
            case_failure_update=None,
            candidate_mutation=None,
            next_job_spec=None,
            final_result_target=None,
            clear_active_job=True,
            reason=reason,
        )

    def _job_spec(
        self,
        trigger: ValidatedTrigger,
        job_type: JobType,
        *,
        target_state_revision: int,
        goal: str,
        evidence_bindings: list[PlannedResourceBinding],
        attachment_refs: Sequence[str],
        previous_outcome_refs: Sequence[str],
        artifact_bindings: list[PlannedResourceBinding],
        selected_skill_ref: VersionedRef | None,
        review_target_binding: ReviewTargetBinding | None = None,
        methods_review_target: MethodsReviewTargetV2 | None = None,
        replacement_for_job_id: str | None = None,
        generic_problem_text: str | None = None,
    ) -> JobSpec | ApplicationError:
        bindings = trigger.runtime_bindings_by_job_type.get(job_type)
        if bindings is None:
            return _validation(
                "The Trigger is missing RuntimeBindings for the next Job type.",
                field="runtime_bindings_by_job_type",
                expected=job_type.value,
                actual=None,
            )
        if job_type is JobType.ROUTE:
            if selected_skill_ref is not None:
                return _validation("A ROUTE Job cannot have a selected Skill.")
        elif (
            job_type is JobType.DIAGNOSE
            and bindings.diagnosis_mode is DiagnosisMode.GENERIC
        ):
            if selected_skill_ref is not None or bindings.skill_ref is not None:
                return _validation(
                    "GENERIC RuntimeBindings must not select a specialized Skill."
                )
        elif selected_skill_ref is None or bindings.skill_ref != selected_skill_ref:
            return _validation(
                "RuntimeBindings do not match the Case selected Skill.",
                field="runtime_bindings_by_job_type.skill_ref",
                expected=(
                    None
                    if selected_skill_ref is None
                    else getattr(selected_skill_ref, "id", str(selected_skill_ref))
                ),
                actual=None if bindings.skill_ref is None else bindings.skill_ref.id,
            )
        return JobSpec(
            job_type=job_type,
            diagnosis_mode=bindings.diagnosis_mode,
            review_policy=bindings.review_policy,
            generic_skill_name=bindings.generic_skill_name,
            generic_problem_text=generic_problem_text,
            goal=goal,
            target_state_revision=target_state_revision,
            evidence_bindings=evidence_bindings,
            attachment_refs=list(attachment_refs),
            previous_outcome_refs=list(previous_outcome_refs),
            artifact_bindings=artifact_bindings,
            agent_profile_ref=bindings.agent_profile_ref,
            available_skill_refs=bindings.available_skill_refs,
            skill_ref=bindings.skill_ref,
            tool_bundle_ref=bindings.tool_bundle_ref,
            context_policy_ref=bindings.context_policy_ref,
            output_contract_ref=bindings.output_contract_ref,
            logparse_tool_ref=bindings.logparse_tool_ref,
            logparse_product=bindings.logparse_product,
            review_target_binding=review_target_binding,
            methods_review_target=methods_review_target,
            replacement_for_job_id=replacement_for_job_id,
            resource_limits=bindings.resource_limits,
        )

    def _normalize_delta(
        self,
        state: DiagnosisState,
        active: Job,
        outcome: JobOutcome,
        delta: DiagnosisStateDelta,
        extra_bindings: Sequence[EvidenceBinding],
    ) -> (
        tuple[DiagnosisStateDelta, bool, list[str], list[str]] | ApplicationError
    ):
        patch = self._normalize_patch(state, delta.problem_spec_patch)
        if isinstance(patch, ApplicationError):
            return patch
        accepted_patch, patch_changed = patch

        current_items = {
            item.item_id: item
            for item in (
                state.user_facts
                + state.confirmed_facts
                + state.active_hypotheses
                + state.rejected_hypotheses
                + state.open_questions
            )
        }
        active_hypothesis_ids = {
            item.item_id for item in state.active_hypotheses
        }
        open_question_ids = {item.item_id for item in state.open_questions}
        accepted_facts = [
            draft for draft in delta.proposed_facts if draft.evidence_bindings
        ]
        new_drafts = (
            accepted_facts
            + delta.add_active_hypotheses
            + delta.add_open_questions
        )
        operation_ids = [draft.item_id for draft in new_drafts]
        operation_ids.extend(change.item_id for change in delta.update_hypotheses)
        operation_ids.extend(change.item_id for change in delta.reject_hypotheses)
        operation_ids.extend(change.item_id for change in delta.resolve_questions)
        if len(operation_ids) != len(set(operation_ids)):
            return _validation("A Diagnosis item may be changed only once per Delta.")
        if any(draft.item_id in current_items for draft in new_drafts):
            return _validation("New Diagnosis item IDs must not already exist.")
        if any(
            change.item_id not in active_hypothesis_ids
            for change in delta.update_hypotheses + delta.reject_hypotheses
        ):
            return _validation("Hypothesis changes must target ACTIVE hypotheses.")
        if any(
            change.item_id not in open_question_ids
            for change in delta.resolve_questions
        ):
            return _validation("Question resolution must target an OPEN question.")
        effective_hypothesis_updates = [
            change
            for change in delta.update_hypotheses
            if self._hypothesis_update_changes_projection(
                current_items[change.item_id],
                change,
            )
        ]
        supersede_error = self._validate_supersedes(current_items, new_drafts)
        if supersede_error is not None:
            return supersede_error
        requirement_error = self._validate_new_requirements(
            state,
            active,
            delta.add_pending_requirements,
        )
        if requirement_error is not None:
            return requirement_error

        accepted_draft_bindings: list[EvidenceBinding] = []
        for draft in new_drafts:
            accepted_draft_bindings.extend(draft.evidence_bindings)
        for change in (
            effective_hypothesis_updates
            + delta.reject_hypotheses
            + delta.resolve_questions
        ):
            accepted_draft_bindings.extend(change.evidence_bindings)
        all_bindings = _dedupe_evidence_bindings(
            [
                *delta.add_evidence_bindings,
                *accepted_draft_bindings,
                *extra_bindings,
            ]
        )
        proposals = {proposal.proposal_key: proposal for proposal in outcome.proposed_evidence}
        artifacts = {proposal.proposal_key: proposal for proposal in outcome.proposed_artifacts}
        accepted_binding_error = self._validate_evidence_bindings(
            state,
            all_bindings,
            proposals,
            artifacts,
        )
        if accepted_binding_error is not None:
            return accepted_binding_error
        additions = [
            binding
            for binding in all_bindings
            if binding.evidence_proposal_key is not None
            or binding.existing_evidence_id not in state.evidence_refs
        ]
        evidence_keys = [
            binding.evidence_proposal_key
            for binding in additions
            if binding.evidence_proposal_key is not None
        ]
        artifact_keys = _dedupe(
            artifact_key
            for key in evidence_keys
            if (artifact_key := proposals[key].source_binding.artifact_proposal_key)
            is not None
        )
        accepted_evidence_keys = set(evidence_keys)
        accepted_artifact_keys = set(artifact_keys)
        if any(
            proposal.proposal_key not in accepted_evidence_keys
            and proposal.source_binding.artifact_proposal_key
            in accepted_artifact_keys
            for proposal in outcome.proposed_evidence
        ):
            return _validation(
                "A LOGPARSE_RUN cannot be accepted while a bound Evidence "
                "proposal remains unaccepted."
            )
        accepted_delta = DiagnosisStateDelta(
            problem_spec_patch=accepted_patch,
            add_user_facts=[],
            proposed_facts=sorted(accepted_facts, key=lambda item: item.item_id),
            add_active_hypotheses=sorted(
                delta.add_active_hypotheses,
                key=lambda item: item.item_id,
            ),
            update_hypotheses=sorted(
                effective_hypothesis_updates,
                key=lambda item: item.item_id,
            ),
            reject_hypotheses=sorted(
                delta.reject_hypotheses,
                key=lambda item: item.item_id,
            ),
            add_open_questions=sorted(
                delta.add_open_questions,
                key=lambda item: item.item_id,
            ),
            resolve_questions=sorted(
                delta.resolve_questions,
                key=lambda item: item.item_id,
            ),
            # PendingRequirement order is semantic: requested_input follows the
            # Skill/Profile declaration and the attachment follows that input
            # group. Opaque derived IDs must not reorder the validated request.
            add_pending_requirements=list(delta.add_pending_requirements),
            fulfill_requirements=[],
            add_evidence_bindings=additions,
        )
        semantic_change = bool(
            patch_changed
            or accepted_delta.proposed_facts
            or accepted_delta.add_active_hypotheses
            or accepted_delta.update_hypotheses
            or accepted_delta.reject_hypotheses
            or accepted_delta.add_open_questions
            or accepted_delta.resolve_questions
            or accepted_delta.add_pending_requirements
            or accepted_delta.add_evidence_bindings
        )
        return accepted_delta, semantic_change, evidence_keys, artifact_keys

    def _hypothesis_update_changes_projection(
        self,
        current: DiagnosisItem,
        change: DiagnosisItemChange,
    ) -> bool:
        if change.statement is not None and change.statement != current.statement:
            return True
        current_evidence = set(current.evidence_refs)
        return any(
            binding.evidence_proposal_key is not None
            or binding.existing_evidence_id not in current_evidence
            for binding in change.evidence_bindings
        )

    def _normalize_patch(
        self,
        state: DiagnosisState,
        patch: ProblemSpecPatch | None,
    ) -> tuple[ProblemSpecPatch | None, bool] | ApplicationError:
        if patch is None:
            return None, False
        _, patch_changed = apply_problem_spec_patch(state.problem_spec, patch)
        if not patch_changed:
            return None, False
        changes = {
            field_name
            for field_name in patch.model_fields_set
            if getattr(state.problem_spec, field_name) != getattr(patch, field_name)
        }
        if changes & _STABLE_TARGET_FIELDS:
            return _error(
                ErrorCode.NEW_CASE_REQUIRED,
                "The ProblemSpec patch changes the stable diagnosis target.",
                [
                    _detail(
                        field="problem_spec_patch",
                        expected="same stable diagnosis target",
                        actual=",".join(sorted(changes & _STABLE_TARGET_FIELDS)),
                    )
                ],
            )
        return patch, True

    def _validate_supersedes(
        self,
        current_items: dict[str, DiagnosisItem],
        new_drafts: Sequence[DiagnosisItemDraft],
    ) -> ApplicationError | None:
        known = set(current_items) | {draft.item_id for draft in new_drafts}
        graph = {
            item.item_id: list(item.supersedes) for item in current_items.values()
        }
        graph.update({draft.item_id: list(draft.supersedes) for draft in new_drafts})
        if any(ref not in known for refs in graph.values() for ref in refs):
            return _validation("supersedes may only reference known Diagnosis items.")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> bool:
            if item_id in visiting:
                return False
            if item_id in visited:
                return True
            visiting.add(item_id)
            if any(not visit(target) for target in graph.get(item_id, [])):
                return False
            visiting.remove(item_id)
            visited.add(item_id)
            return True

        if any(not visit(item_id) for item_id in graph):
            return _validation("supersedes relationships must be acyclic.")
        return None

    def _validate_new_requirements(
        self,
        state: DiagnosisState,
        active: Job,
        requirements: Sequence[PendingRequirement],
    ) -> ApplicationError | None:
        existing_ids = {item.requirement_id for item in state.pending_requirements}
        open_names = {
            item.name
            for item in state.pending_requirements
            if item.status is RequirementStatus.OPEN
        }
        fixed_input_names = {
            item.provenance.input_name for item in state.user_facts
        }
        if any(item.requirement_id in existing_ids for item in requirements):
            return _validation("New requirement IDs must not already exist.")
        if any(item.name in open_names for item in requirements):
            return _validation("New requirement names must not duplicate OPEN requirements.")
        if any(
            item.kind is RequirementKind.INPUT and item.name in fixed_input_names
            for item in requirements
        ):
            return _error(
                ErrorCode.NEW_CASE_REQUIRED,
                "An existing Case input cannot be requested again or corrected.",
            )
        if any(
            item.requested_by_job_id != active.job_id
            or item.status is not RequirementStatus.OPEN
            for item in requirements
        ):
            return _validation("New requirements must be OPEN and requested by the active Job.")
        open_attachments = sum(
            item.kind is RequirementKind.ATTACHMENT
            and item.status is RequirementStatus.OPEN
            for item in state.pending_requirements
        ) + sum(item.kind is RequirementKind.ATTACHMENT for item in requirements)
        if open_attachments > 1:
            return _validation("A DiagnosisState may have at most one OPEN ATTACHMENT requirement.")
        return None

    def _validate_evidence_bindings(
        self,
        state: DiagnosisState,
        bindings: Sequence[EvidenceBinding],
        proposals: dict[str, EvidenceProposal],
        artifacts: dict[str, ArtifactProposal],
    ) -> ApplicationError | None:
        for binding in bindings:
            if binding.existing_evidence_id is not None:
                if binding.existing_evidence_id not in state.evidence_refs:
                    return _validation(
                        "Existing Evidence bindings must belong to the current DiagnosisState."
                    )
                continue
            assert binding.evidence_proposal_key is not None
            proposal = proposals.get(binding.evidence_proposal_key)
            if proposal is None:
                return _validation("Evidence binding references an unknown proposal key.")
            artifact_key = proposal.source_binding.artifact_proposal_key
            if artifact_key is not None:
                artifact = artifacts.get(artifact_key)
                if artifact is None or artifact.artifact_kind is not ArtifactKind.LOGPARSE_RUN:
                    return _validation(
                        "LOGPARSE Evidence must bind a same-Outcome LOGPARSE_RUN Artifact."
                    )
        return None

    def _validate_requested_requirements(
        self,
        state: DiagnosisState,
        active: Job,
        delta: DiagnosisStateDelta,
        requested_ids: Sequence[str],
        kind: RequirementKind,
    ) -> ApplicationError | None:
        target_requirements = {
            item.requirement_id: item for item in state.pending_requirements
        }
        target_requirements.update(
            {item.requirement_id: item for item in delta.add_pending_requirements}
        )
        open_requirements = [
            requirement
            for requirement in target_requirements.values()
            if requirement.status is RequirementStatus.OPEN
        ]
        if any(
            requirement.supplement_policy is not SupplementPolicy.MISSING_ONLY
            for requirement in open_requirements
        ):
            return _validation(
                "Waiting transitions require Skill-declared MISSING_ONLY requirements."
            )
        if any(
            requirement.requested_by_job_id != active.job_id
            for requirement in open_requirements
        ):
            return _validation(
                "All OPEN requirements in a waiting transition must belong to the current Job."
            )
        if kind is RequirementKind.ATTACHMENT and any(
            requirement.kind is RequirementKind.INPUT
            for requirement in open_requirements
        ):
            return _validation(
                "NEED_ATTACHMENT cannot bypass an OPEN INPUT requirement."
            )
        for requested_id in requested_ids:
            requirement = target_requirements.get(requested_id)
            if (
                requirement is None
                or requirement.kind is not kind
                or requirement.status is not RequirementStatus.OPEN
            ):
                return _validation(
                    "Requested requirement IDs must resolve the expected OPEN kind."
                )
        return None

    def _candidate_bindings(
        self,
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

    def _validate_candidate(
        self,
        state: DiagnosisState,
        accepted_delta: DiagnosisStateDelta,
        candidate: CandidateConclusionDraft,
        outcome: JobOutcome,
    ) -> ApplicationError | None:
        criteria = state.problem_spec.completion_criteria
        mappings = candidate.completion_criteria_mapping
        if (
            not candidate.supporting_evidence_bindings
            or len(mappings) != len(criteria)
            or any(
                mapping.criterion_index != index
                or mapping.criterion != criterion
                for index, (mapping, criterion) in enumerate(
                    zip(mappings, criteria, strict=True)
                )
            )
        ):
            return _validation(
                "Candidate completion mappings must exactly cover the ProblemSpec."
            )
        target_order = [f"existing:{item}" for item in state.evidence_refs]
        target_order.extend(
            _evidence_binding_key(binding)
            for binding in accepted_delta.add_evidence_bindings
        )
        supporting_order = [
            _evidence_binding_key(binding)
            for binding in candidate.supporting_evidence_bindings
        ]
        if not set(supporting_order) <= set(target_order):
            return _validation(
                "Candidate supporting Evidence must be drawn from target Evidence."
            )
        if candidate.existing_conclusion_id is not None:
            current = state.candidate_conclusion
            if current is None or current.conclusion_id != candidate.existing_conclusion_id:
                return _validation("Candidate revision must target the current conclusion ID.")
        user_results = [
            proposal
            for proposal in outcome.proposed_artifacts
            if proposal.artifact_kind is ArtifactKind.USER_RESULT
        ]
        if len(user_results) != 1:
            return _validation("Candidate acceptance requires exactly one USER_RESULT.")
        return None

    def _continuation_with_proposals(
        self,
        trigger: ValidatedTrigger,
        evidence_keys: Sequence[str],
        dependency_artifact_keys: Sequence[str],
    ) -> tuple[
        list[PlannedResourceBinding],
        list[str],
        list[PlannedResourceBinding],
    ]:
        continuation = trigger.continuation_resources
        evidence_bindings = _existing_bindings(continuation.evidence_refs)
        evidence_bindings.extend(_proposal_binding(key) for key in evidence_keys)
        artifact_bindings = _existing_bindings(continuation.artifact_refs)
        artifact_bindings.extend(
            _proposal_binding(key) for key in dependency_artifact_keys
        )
        return evidence_bindings, list(continuation.attachment_refs), artifact_bindings


__all__ = ["DomainCoordinator"]
