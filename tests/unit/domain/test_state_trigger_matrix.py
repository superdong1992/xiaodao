from __future__ import annotations

from itertools import product
from typing import get_type_hints

from problem_locator.contracts import (
    ApplicationError,
    AssetUnavailableTriggerPayload,
    CancelCaseTriggerPayload,
    Case,
    CaseSnapshot,
    CaseStatus,
    CoordinatorPlanResult,
    CreateCaseTriggerPayload,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    ErrorCode,
    ExecutionFailedTriggerPayload,
    ExecutionFailure,
    ExecutionStage,
    JobType,
    OldEpochTriggerPayload,
    ResumeInterruptedTriggerPayload,
    ReviewOutcomeTriggerPayload,
    RouteOutcomeTriggerPayload,
    StaleActiveOutcomeTriggerPayload,
    SubmitSupplementTriggerPayload,
    TransitionPlan,
    TriggerType,
    VersionedRef,
    canonical_json_bytes,
    validate_coordinator_plan_result,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    CASE_ID,
    CURRENT_EPOCH,
    RUNTIME_EPOCH,
    TRIGGER_ID,
    continuation,
    diagnose_job,
    failed_case_snapshot,
    interrupted_snapshot,
    rebuild,
    review_job,
    review_outcome,
    route_job,
    route_outcome,
    runtime_bindings,
    snapshot_with_active,
    state_from_job,
    trigger,
    waiting_snapshot,
    diagnosis_outcome,
)


LEGAL_STATUS_TRIGGER_PAIRS = frozenset(
    {
        (CaseStatus.NEW, TriggerType.CREATE_CASE),
        (CaseStatus.RUNNING, TriggerType.ROUTE_OUTCOME),
        (CaseStatus.RUNNING, TriggerType.DIAGNOSIS_OUTCOME),
        (CaseStatus.RUNNING, TriggerType.CANCEL_CASE),
        (CaseStatus.RUNNING, TriggerType.EXECUTION_FAILED),
        (CaseStatus.RUNNING, TriggerType.ASSET_VERSION_UNAVAILABLE),
        (CaseStatus.RUNNING, TriggerType.MARK_OLD_EPOCH_INTERRUPTED),
        (CaseStatus.RUNNING, TriggerType.STALE_ACTIVE_OUTCOME),
        (CaseStatus.WAITING_INPUT, TriggerType.SUBMIT_SUPPLEMENT),
        (CaseStatus.WAITING_INPUT, TriggerType.CANCEL_CASE),
        (CaseStatus.WAITING_ATTACHMENT, TriggerType.SUBMIT_SUPPLEMENT),
        (CaseStatus.WAITING_ATTACHMENT, TriggerType.CANCEL_CASE),
        (CaseStatus.REVIEWING, TriggerType.REVIEW_OUTCOME),
        (CaseStatus.REVIEWING, TriggerType.CANCEL_CASE),
        (CaseStatus.REVIEWING, TriggerType.EXECUTION_FAILED),
        (CaseStatus.REVIEWING, TriggerType.ASSET_VERSION_UNAVAILABLE),
        (CaseStatus.REVIEWING, TriggerType.MARK_OLD_EPOCH_INTERRUPTED),
        (CaseStatus.REVIEWING, TriggerType.STALE_ACTIVE_OUTCOME),
        (CaseStatus.INTERRUPTED, TriggerType.RESUME_INTERRUPTED),
        (CaseStatus.INTERRUPTED, TriggerType.CANCEL_CASE),
        (CaseStatus.INTERRUPTED, TriggerType.ASSET_VERSION_UNAVAILABLE),
    }
)


def _case_without_active(status: CaseStatus) -> CaseSnapshot:
    state = state_from_job(route_job())
    if status is CaseStatus.FAILED:
        return failed_case_snapshot(state)
    if status is CaseStatus.INTERRUPTED:
        return interrupted_snapshot(route_job())
    if status in {CaseStatus.WAITING_INPUT, CaseStatus.WAITING_ATTACHMENT}:
        return waiting_snapshot(state, status)
    if status is CaseStatus.RESOLVED:
        review = review_job()
        review_state = state_from_job(review)
        candidate = review_state.candidate_conclusion
        assert candidate is not None
        accepted = rebuild(candidate, status="ACCEPTED")
        resolved_state = rebuild(review_state, candidate_conclusion=accepted)
        case = Case(
            case_id=CASE_ID,
            status=status,
            case_revision=7,
            diagnosis_state=resolved_state,
            active_job_id=None,
            selected_skill_ref=review.skill_ref,
            final_result=accepted,
            failure=None,
            created_at="2026-07-31T00:00:00.000Z",
            updated_at="2026-07-31T00:03:00.000Z",
        )
    else:
        case = Case(
            case_id=CASE_ID,
            status=status,
            case_revision=7 if status is not CaseStatus.NEW else 1,
            diagnosis_state=state,
            active_job_id=None,
            selected_skill_ref=None,
            final_result=None,
            failure=None,
            created_at="2026-07-31T00:00:00.000Z",
            updated_at="2026-07-31T00:03:00.000Z",
        )
    return CaseSnapshot(
        case=case,
        active_job=None,
        resume_source_job=None,
        replacement_job_ids_by_source={},
    )


def _snapshot(status: CaseStatus) -> CaseSnapshot:
    if status is CaseStatus.RUNNING:
        return snapshot_with_active(route_job())
    if status is CaseStatus.REVIEWING:
        return snapshot_with_active(review_job())
    return _case_without_active(status)


def _request(snapshot: CaseSnapshot, trigger_type: TriggerType):
    route = route_job()
    diagnosis = diagnose_job()
    review = review_job()
    route_result = route_outcome()
    diagnosis_result = diagnosis_outcome()
    review_result = review_outcome()
    bindings = {
        JobType.ROUTE: runtime_bindings(route),
        JobType.DIAGNOSE: runtime_bindings(diagnosis),
        JobType.REVIEW: runtime_bindings(review),
    }
    occurred_at = "2026-07-31T00:03:00.000Z"
    resources = continuation()
    if trigger_type is TriggerType.CREATE_CASE:
        payload = CreateCaseTriggerPayload(
            problem_spec=snapshot.case.diagnosis_state.problem_spec,
            initial_user_facts=snapshot.case.diagnosis_state.user_facts,
        )
    elif trigger_type is TriggerType.ROUTE_OUTCOME:
        payload = RouteOutcomeTriggerPayload(job_outcome=route_result)
        occurred_at = route_result.produced_at
        resources = continuation(incoming_outcome_id=route_result.outcome_id)
    elif trigger_type is TriggerType.DIAGNOSIS_OUTCOME:
        payload = DiagnosisOutcomeTriggerPayload(job_outcome=diagnosis_result)
        occurred_at = diagnosis_result.produced_at
        resources = continuation(incoming_outcome_id=diagnosis_result.outcome_id)
    elif trigger_type is TriggerType.REVIEW_OUTCOME:
        payload = ReviewOutcomeTriggerPayload(job_outcome=review_result)
        occurred_at = review_result.produced_at
        resources = continuation(incoming_outcome_id=review_result.outcome_id)
    elif trigger_type is TriggerType.SUBMIT_SUPPLEMENT:
        payload = SubmitSupplementTriggerPayload(
            user_facts=[
                DiagnosisItem(
                    item_id="00000000-0000-0000-0000-000000000093",
                    statement="value",
                    status=DiagnosisItemStatus.ACTIVE,
                    provenance=DiagnosisProvenance(
                        source_type=DiagnosisProvenanceType.USER_INPUT,
                        source_ref=TRIGGER_ID,
                        input_name="order_id",
                    ),
                    evidence_refs=[],
                    created_revision=snapshot.case.diagnosis_state.revision + 1,
                    supersedes=[],
                )
            ],
            ready_attachment_ids=[],
            stable_target_changed=False,
        )
    elif trigger_type is TriggerType.CANCEL_CASE:
        payload = CancelCaseTriggerPayload(
            reason="USER_CANCEL",
            active_job_id=(
                None if snapshot.active_job is None else snapshot.active_job.job_id
            ),
        )
    elif trigger_type is TriggerType.RESUME_INTERRUPTED:
        payload = ResumeInterruptedTriggerPayload(
            source_job_id=(
                route.job_id
                if snapshot.resume_source_job is None
                else snapshot.resume_source_job.job_id
            )
        )
    elif trigger_type is TriggerType.EXECUTION_FAILED:
        payload = ExecutionFailedTriggerPayload(
            source_job_id=(
                route.job_id if snapshot.active_job is None else snapshot.active_job.job_id
            ),
            source_outcome_id=None,
            execution_failure=ExecutionFailure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.BACKEND_TIMEOUT,
                message="The fixed execution timed out.",
                retryable=True,
                details=[],
            ),
        )
    elif trigger_type is TriggerType.ASSET_VERSION_UNAVAILABLE:
        payload = AssetUnavailableTriggerPayload(
            source_job_id=(
                route.job_id
                if snapshot.active_job is None
                else snapshot.active_job.job_id
            ),
            missing_refs=[
                VersionedRef(id="missing", version="1", content_hash="a" * 64)
            ],
        )
    elif trigger_type is TriggerType.MARK_OLD_EPOCH_INTERRUPTED:
        payload = OldEpochTriggerPayload(
            source_job_id=(
                route.job_id if snapshot.active_job is None else snapshot.active_job.job_id
            ),
            previous_runtime_epoch=RUNTIME_EPOCH,
            current_runtime_epoch=CURRENT_EPOCH,
        )
    else:
        payload = StaleActiveOutcomeTriggerPayload(
            source_job_id=(
                route.job_id if snapshot.active_job is None else snapshot.active_job.job_id
            ),
            outcome_id="00000000-0000-0000-0000-000000000094",
            expected_base_state_revision=1,
            actual_state_revision=2,
        )
    return trigger(
        snapshot,
        trigger_type=trigger_type,
        payload=payload,
        bindings=bindings,
        continuation_resources=resources,
        occurred_at=occurred_at,
    )


def test_status_trigger_partition_covers_the_complete_cartesian_product() -> None:
    all_pairs = set(product(CaseStatus, TriggerType))
    illegal_pairs = all_pairs - LEGAL_STATUS_TRIGGER_PAIRS

    assert len(all_pairs) == len(CaseStatus) * len(TriggerType) == 99
    assert LEGAL_STATUS_TRIGGER_PAIRS.isdisjoint(illegal_pairs)
    assert set(LEGAL_STATUS_TRIGGER_PAIRS) | illegal_pairs == all_pairs

    assert get_type_hints(DomainCoordinator.plan)["return"] == CoordinatorPlanResult

    for status, trigger_type in sorted(
        all_pairs,
        key=lambda item: (item[0].value, item[1].value),
    ):
        snapshot = _snapshot(status)
        request = _request(snapshot, trigger_type)
        snapshot_before = canonical_json_bytes(snapshot)
        request_before = canonical_json_bytes(request)
        first = DomainCoordinator().plan(snapshot, request)
        second = DomainCoordinator().plan(snapshot, request)
        assert isinstance(first, (TransitionPlan, ApplicationError))
        assert canonical_json_bytes(first) == canonical_json_bytes(second)
        assert validate_coordinator_plan_result(request, first) is first
        assert canonical_json_bytes(snapshot) == snapshot_before
        assert canonical_json_bytes(request) == request_before
        if (status, trigger_type) in LEGAL_STATUS_TRIGGER_PAIRS:
            assert not (
                isinstance(first, ApplicationError)
                and first.code is ErrorCode.INVALID_CASE_STATE
            ), (status, trigger_type, first)
        else:
            assert isinstance(first, ApplicationError), (
                status,
                trigger_type,
                first,
            )
            assert first.code is ErrorCode.INVALID_CASE_STATE, (
                status,
                trigger_type,
                first,
            )
            assert first.retryable is False


def test_stable_target_change_has_one_error_result_across_every_case_status() -> None:
    for status in CaseStatus:
        snapshot = _snapshot(status)
        request = _request(snapshot, TriggerType.SUBMIT_SUPPLEMENT)
        payload = request.payload
        assert isinstance(payload, SubmitSupplementTriggerPayload)
        request = rebuild(
            request,
            payload=rebuild(payload, stable_target_changed=True),
        )
        snapshot_before = canonical_json_bytes(snapshot)
        request_before = canonical_json_bytes(request)

        result = DomainCoordinator().plan(snapshot, request)

        assert isinstance(result, ApplicationError)
        assert result.code is ErrorCode.NEW_CASE_REQUIRED
        assert result.retryable is False
        assert validate_coordinator_plan_result(request, result) is result
        assert canonical_json_bytes(snapshot) == snapshot_before
        assert canonical_json_bytes(request) == request_before
