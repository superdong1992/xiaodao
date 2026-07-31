from __future__ import annotations

import pytest

from problem_locator.contracts import (
    ApplicationError,
    AssetUnavailableTriggerPayload,
    CancelCaseTriggerPayload,
    CandidateStatus,
    CaseStatus,
    JobStatus,
    JobType,
    OldEpochTriggerPayload,
    OutcomeDisposition,
    ResumeInterruptedTriggerPayload,
    StaleActiveOutcomeTriggerPayload,
    TriggerType,
    VersionedRef,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    CURRENT_EPOCH,
    RUNTIME_EPOCH,
    continuation,
    diagnose_job,
    interrupted_snapshot,
    rebuild,
    review_job,
    route_job,
    runtime_bindings,
    snapshot_with_active,
    state_from_job,
    trigger,
    waiting_snapshot,
)


MISSING_REF = VersionedRef(
    id="missing-asset",
    version="1.0.0",
    content_hash="a" * 64,
)


def test_cancel_ends_the_active_job_and_clears_it() -> None:
    source = route_job()
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.CANCEL_CASE,
        payload=CancelCaseTriggerPayload(
            reason="USER_CANCEL",
            active_job_id=source.job_id,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "CANCELLED"
    assert plan.job_updates[0].target_status is JobStatus.CANCELLED
    assert plan.clear_active_job is True
    assert plan.next_job_spec is None


@pytest.mark.parametrize(
    "status",
    [CaseStatus.WAITING_INPUT, CaseStatus.WAITING_ATTACHMENT],
)
def test_cancel_waiting_case_has_no_job_update(status: CaseStatus) -> None:
    snapshot = waiting_snapshot(state_from_job(diagnose_job()), status)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.CANCEL_CASE,
        payload=CancelCaseTriggerPayload(
            reason="USER_CANCEL",
            active_job_id=None,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.CANCELLED
    assert plan.job_updates == []
    assert plan.clear_active_job is False


def test_cancel_interrupted_case_does_not_rewrite_the_source_job() -> None:
    snapshot = interrupted_snapshot(diagnose_job())
    request = trigger(
        snapshot,
        trigger_type=TriggerType.CANCEL_CASE,
        payload=CancelCaseTriggerPayload(
            reason="USER_CANCEL",
            active_job_id=None,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.CANCELLED
    assert plan.job_updates == []
    assert plan.case_failure_update is None


@pytest.mark.parametrize("source_factory", [route_job, diagnose_job, review_job])
def test_resume_copies_the_source_job_and_preserves_its_stage(source_factory: object) -> None:
    source = source_factory()
    snapshot = interrupted_snapshot(source)
    interrupted_source = snapshot.resume_source_job
    assert interrupted_source is not None
    request = trigger(
        snapshot,
        trigger_type=TriggerType.RESUME_INTERRUPTED,
        payload=ResumeInterruptedTriggerPayload(source_job_id=source.job_id),
        bindings={source.job_type: runtime_bindings(source)},
        continuation_resources=continuation(job=source),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.next_job_spec is not None
    spec = plan.next_job_spec
    assert spec.job_type is source.job_type
    assert spec.goal == source.goal
    assert spec.replacement_for_job_id == source.job_id
    assert [item.existing_resource_id for item in spec.evidence_bindings] == source.evidence_refs
    assert spec.attachment_refs == source.attachment_refs
    assert [item.existing_resource_id for item in spec.artifact_bindings] == source.artifact_refs
    assert spec.previous_outcome_refs == source.previous_outcome_refs
    assert runtime_bindings(source) == runtime_bindings(interrupted_source)
    if source.job_type is JobType.REVIEW:
        assert plan.target_case_status.value == "REVIEWING"
        assert spec.review_target_binding is not None
        assert spec.review_target_binding.existing_candidate_target == source.review_target
    else:
        assert plan.target_case_status.value == "RUNNING"


def test_resume_rejects_selected_skill_drift() -> None:
    source = diagnose_job()
    snapshot = interrupted_snapshot(source)
    drifted_case = rebuild(
        snapshot.case,
        selected_skill_ref=VersionedRef(
            id="different-skill",
            version="1.0.0",
            content_hash="d" * 64,
        ),
    )
    snapshot = rebuild(snapshot, case=drifted_case)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.RESUME_INTERRUPTED,
        payload=ResumeInterruptedTriggerPayload(source_job_id=source.job_id),
        bindings={source.job_type: runtime_bindings(source)},
        continuation_resources=continuation(job=source),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"


def test_resume_review_rejects_candidate_target_drift() -> None:
    source = review_job()
    snapshot = interrupted_snapshot(source)
    candidate = snapshot.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    rejected = rebuild(candidate, status=CandidateStatus.REJECTED)
    drifted_state = rebuild(
        snapshot.case.diagnosis_state,
        candidate_conclusion=rejected,
    )
    snapshot = rebuild(
        snapshot,
        case=rebuild(snapshot.case, diagnosis_state=drifted_state),
    )
    request = trigger(
        snapshot,
        trigger_type=TriggerType.RESUME_INTERRUPTED,
        payload=ResumeInterruptedTriggerPayload(source_job_id=source.job_id),
        bindings={source.job_type: runtime_bindings(source)},
        continuation_resources=continuation(job=source),
    )

    result = DomainCoordinator().plan(snapshot, request)

    assert isinstance(result, ApplicationError)
    assert result.code.value == "VALIDATION_ERROR"


@pytest.mark.parametrize("source_factory", [diagnose_job, review_job])
def test_old_epoch_interrupts_without_creating_a_replacement(
    source_factory: object,
) -> None:
    source = source_factory()
    snapshot = snapshot_with_active(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.MARK_OLD_EPOCH_INTERRUPTED,
        payload=OldEpochTriggerPayload(
            source_job_id=source.job_id,
            previous_runtime_epoch=RUNTIME_EPOCH,
            current_runtime_epoch=CURRENT_EPOCH,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "INTERRUPTED"
    assert plan.job_updates[0].target_status is JobStatus.INTERRUPTED
    assert plan.next_job_spec is None
    assert plan.case_failure_update is None


@pytest.mark.parametrize("source_factory", [diagnose_job, review_job])
def test_stale_active_outcome_is_audit_only_and_interrupts(
    source_factory: object,
) -> None:
    source = source_factory()
    state = rebuild(state_from_job(source), revision=source.base_state_revision + 1)
    snapshot = snapshot_with_active(source, state=state)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.STALE_ACTIVE_OUTCOME,
        payload=StaleActiveOutcomeTriggerPayload(
            source_job_id=source.job_id,
            outcome_id="00000000-0000-0000-0000-000000000039",
            expected_base_state_revision=source.base_state_revision,
            actual_state_revision=state.revision,
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.outcome_disposition is OutcomeDisposition.STALE
    assert plan.target_case_status.value == "INTERRUPTED"
    assert plan.accepted_state_delta.model_dump(exclude_none=False)["add_evidence_bindings"] == []
    assert plan.accepted_evidence_proposal_keys == []
    assert plan.accepted_artifact_proposal_keys == []


def test_stale_is_an_outcome_disposition_not_a_job_status() -> None:
    assert "STALE" not in {status.value for status in JobStatus}


@pytest.mark.parametrize("source_factory", [route_job, diagnose_job, review_job])
def test_asset_unavailable_fails_a_pending_active_job(source_factory: object) -> None:
    source = source_factory()
    snapshot = snapshot_with_active(source, job_status=JobStatus.PENDING)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ASSET_VERSION_UNAVAILABLE,
        payload=AssetUnavailableTriggerPayload(
            source_job_id=source.job_id,
            missing_refs=[MISSING_REF],
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == "FAILED"
    assert plan.job_updates[0].target_status is JobStatus.FAILED
    assert plan.case_failure_update is not None
    assert plan.case_failure_update.value is not None
    assert plan.case_failure_update.value.code.value == "ASSET_VERSION_UNAVAILABLE"


def test_asset_unavailable_fails_interrupted_case_without_rewriting_source() -> None:
    source = diagnose_job()
    snapshot = interrupted_snapshot(source)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ASSET_VERSION_UNAVAILABLE,
        payload=AssetUnavailableTriggerPayload(
            source_job_id=source.job_id,
            missing_refs=[MISSING_REF],
        ),
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status is CaseStatus.FAILED
    assert plan.job_updates == []
    assert plan.clear_active_job is False
    assert plan.case_failure_update is not None
    assert plan.case_failure_update.value is not None
    assert plan.case_failure_update.value.source_job_id == source.job_id
