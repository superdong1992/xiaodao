"""Mechanical construction of complete S00 state mutations and Case updates."""

from __future__ import annotations

from collections.abc import Sequence

from problem_locator.contracts import (
    Artifact,
    Attachment,
    Case,
    CaseStatus,
    DiagnosisState,
    Evidence,
    ExecutionFailureRecord,
    GenericResultV2,
    IdempotencyRecord,
    Job,
    JobLifecycleUpdate,
    JobOutcome,
    OutcomeProcessingRecord,
    RecoveryProcessingRecord,
    RuntimeEpochRecord,
    StateMutation,
    TransitionPlan,
    UnresolvedResult,
    finalize_generic_result_v2,
)

from .formalization import (
    apply_case_failure_update,
    apply_selected_skill_update,
    resolve_final_result,
)


def build_state_mutation(
    *,
    upsert_case: Case | None = None,
    upsert_runtime_epoch_records: Sequence[RuntimeEpochRecord] = (),
    upsert_recovery_processing_records: Sequence[RecoveryProcessingRecord] = (),
    insert_jobs: Sequence[Job] = (),
    job_lifecycle_updates: Sequence[JobLifecycleUpdate] = (),
    insert_outcomes: Sequence[JobOutcome] = (),
    insert_outcome_processing_records: Sequence[OutcomeProcessingRecord] = (),
    insert_execution_failure_records: Sequence[ExecutionFailureRecord] = (),
    upsert_attachments: Sequence[Attachment] = (),
    insert_evidence: Sequence[Evidence] = (),
    insert_artifacts: Sequence[Artifact] = (),
    insert_idempotency_records: Sequence[IdempotencyRecord] = (),
) -> StateMutation:
    """Fill every frozen StateMutation field without shared mutable defaults."""

    return StateMutation(
        upsert_case=upsert_case,
        upsert_runtime_epoch_records=list(upsert_runtime_epoch_records),
        upsert_recovery_processing_records=list(
            upsert_recovery_processing_records
        ),
        insert_jobs=list(insert_jobs),
        job_lifecycle_updates=list(job_lifecycle_updates),
        insert_outcomes=list(insert_outcomes),
        insert_outcome_processing_records=list(
            insert_outcome_processing_records
        ),
        insert_execution_failure_records=list(insert_execution_failure_records),
        upsert_attachments=list(upsert_attachments),
        insert_evidence=list(insert_evidence),
        insert_artifacts=list(insert_artifacts),
        insert_idempotency_records=list(insert_idempotency_records),
    )


def apply_transition_plan_to_case(
    current: Case,
    plan: TransitionPlan,
    target_diagnosis_state: DiagnosisState,
    *,
    created_job: Job | None,
    processed_at: str,
    unresolved_result: UnresolvedResult | None = None,
    generic_result_v2: GenericResultV2 | None = None,
) -> Case:
    """Apply only explicit plan fields after DiagnosisState formalization."""

    if (plan.next_job_spec is None) != (created_job is None):
        raise ValueError("created_job must exist exactly when next_job_spec exists")
    if (plan.generic_result_v2_draft is None) != (generic_result_v2 is None):
        raise ValueError(
            "generic_result_v2 must exist exactly for a V2 generic result draft"
        )
    if generic_result_v2 is not None:
        assert plan.generic_result_v2_draft is not None
        expected_v2 = finalize_generic_result_v2(
            plan.generic_result_v2_draft,
            generic_result_v2.report_artifact_id,
        )
        if generic_result_v2 != expected_v2:
            raise ValueError(
                "generic_result_v2 must finalize the complete TransitionPlan draft"
            )
    if plan.generic_result is not None and generic_result_v2 is not None:
        raise ValueError("V1 and V2 generic results are mutually exclusive")
    generic_result = plan.generic_result or generic_result_v2
    if generic_result is None and (
        (plan.target_case_status is CaseStatus.UNRESOLVED)
        != (unresolved_result is not None)
    ):
        raise ValueError(
            "unresolved_result must exist exactly for an UNRESOLVED plan"
        )
    if generic_result is not None and unresolved_result is not None:
        raise ValueError("generic terminal plans forbid unresolved_result")
    if created_job is not None:
        if (
            created_job.case_id != current.case_id
            or created_job.base_state_revision != target_diagnosis_state.revision
        ):
            raise ValueError("created Job does not match the target Case state")
        active_job_id = created_job.job_id
    elif plan.clear_active_job:
        active_job_id = None
    else:
        active_job_id = current.active_job_id

    candidate = target_diagnosis_state.candidate_conclusion
    payload = current.model_dump(mode="python")
    payload.update(
        status=plan.target_case_status,
        case_revision=current.case_revision + 1,
        diagnosis_state=target_diagnosis_state,
        active_job_id=active_job_id,
        selected_skill_ref=apply_selected_skill_update(
            current.selected_skill_ref,
            plan.selected_skill_update,
        ),
        final_result=resolve_final_result(candidate, plan.final_result_target),
        unresolved_result=unresolved_result,
        generic_result=plan.generic_result,
        generic_result_v2=generic_result_v2,
        failure=apply_case_failure_update(
            current.failure,
            plan.case_failure_update,
        ),
        updated_at=processed_at,
    )
    return Case.model_validate(payload)


__all__ = ["apply_transition_plan_to_case", "build_state_mutation"]
