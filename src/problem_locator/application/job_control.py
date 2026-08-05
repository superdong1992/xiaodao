"""Application orchestration for Job lifecycle control commands.

The public ``JobControlPort`` is split at implementation level: this module
owns ClaimJob, ReportExecutionInfrastructureFailure, and
InterruptPreviousEpoch.  Outcome submission has a larger resource-publication
pipeline and is implemented separately.

Every dependency is an S00 Port.  Coordinator decisions are consumed through
the frozen ``TransitionPlan | ApplicationError`` union, repository failures
retain their public ``ApplicationPortError`` identity, and each state commit is
covered by a short injected publication lease.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    AssetCatalogPort,
    AssetUnavailableTriggerPayload,
    Case,
    CaseAggregate,
    CaseStatus,
    ClaimJob,
    ClaimReceipt,
    Clock,
    CommitReceipt,
    Coordinator,
    ErrorCode,
    ERROR_SPECS,
    ExecutionFailedTriggerPayload,
    ExecutionFailure,
    ExecutionFailureRecord,
    FailureReceipt,
    FailureReportDisposition,
    IdGenerator,
    InterruptPreviousEpoch,
    Job,
    JobLifecycleUpdate,
    JobStatus,
    JobType,
    OldEpochTriggerPayload,
    PublicationCommitGuard,
    RecoveryProcessingRecord,
    RecoveryReceipt,
    ReportExecutionInfrastructureFailure,
    RuntimeEpochRecord,
    StateChangeNotifier,
    StateFile,
    StateMutation,
    StateRepository,
    TransitionPlan,
    TriggerType,
    ValidatedTrigger,
    canonical_json_sha256,
    validate_coordinator_plan_result,
)
from problem_locator.journey import record_journey_event

from .formalization import apply_diagnosis_state_delta
from .mutations import apply_transition_plan_to_case, build_state_mutation
from .preparation import claim_lifecycle_update, fixed_asset_refs
from .projection import (
    build_case_snapshot,
    empty_continuation_resources,
    project_case_components,
    project_case_view,
)


_MAX_COMMIT_ATTEMPTS = 3


def _application_error(
    code: ErrorCode,
    message: str,
    *,
    details: Iterable[ApplicationErrorDetail] = (),
) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=list(details),
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _find_job(
    state: StateFile,
    job_id: str,
) -> tuple[str, CaseAggregate, Job] | None:
    matches = [
        (case_id, aggregate, aggregate.jobs[job_id])
        for case_id, aggregate in state.cases.items()
        if job_id in aggregate.jobs
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Job ID is not globally unique")
    return matches[0]


def _find_failure_record(
    state: StateFile,
    failure_id: str,
) -> tuple[str, CaseAggregate, ExecutionFailureRecord] | None:
    matches = [
        (case_id, aggregate, aggregate.execution_failure_records[failure_id])
        for case_id, aggregate in state.cases.items()
        if failure_id in aggregate.execution_failure_records
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ExecutionFailureRecord ID is not globally unique")
    return matches[0]


def _is_revision_conflict(error: ApplicationPortError) -> bool:
    return error.error.code is ErrorCode.REVISION_CONFLICT


def _expected_case_status(job_type: JobType) -> CaseStatus:
    if job_type is JobType.REVIEW:
        return CaseStatus.REVIEWING
    return CaseStatus.RUNNING


def _claimable(aggregate: CaseAggregate, job: Job) -> bool:
    return (
        job.status is JobStatus.PENDING
        and aggregate.case.active_job_id == job.job_id
        and aggregate.case.status is _expected_case_status(job.job_type)
    )


def _running_job(job: Job, update: JobLifecycleUpdate) -> Job:
    payload = job.model_dump(mode="python")
    payload.update(
        status=update.target_status,
        started_at=update.started_at,
        finished_at=update.finished_at,
        runtime_epoch=update.runtime_epoch,
    )
    return Job.model_validate(payload)


def _case_after_claim(current: Case, *, occurred_at: str) -> Case:
    payload = current.model_dump(mode="python")
    payload.update(
        case_revision=current.case_revision + 1,
        updated_at=occurred_at,
    )
    return Case.model_validate(payload)


def _validate_control_plan(
    aggregate: CaseAggregate,
    plan: TransitionPlan,
    *,
    source_job: Job,
    target_job_statuses: Iterable[JobStatus],
    occurred_at: str,
) -> Case:
    """Validate and apply a control-only plan without inventing mutations."""

    allowed_statuses = set(target_job_statuses)
    if (
        plan.outcome_disposition is not None
        or plan.accepted_evidence_proposal_keys
        or plan.accepted_artifact_proposal_keys
        or plan.accepted_candidate_proposal_key is not None
        or plan.selected_skill_update is not None
        or plan.candidate_mutation is not None
        or plan.next_job_spec is not None
        or plan.final_result_target is not None
        or not plan.clear_active_job
    ):
        raise ValueError("control Trigger returned a non-control TransitionPlan")

    target_state = apply_diagnosis_state_delta(
        aggregate.case.diagnosis_state,
        plan.accepted_state_delta,
        evidence_ids_by_proposal_key={},
        expected_target_revision=aggregate.case.diagnosis_state.revision,
    )
    if target_state != aggregate.case.diagnosis_state:
        raise ValueError("control Trigger must not change DiagnosisState")
    if len(plan.job_updates) != 1:
        raise ValueError("control Trigger must update exactly its source Job")
    update = plan.job_updates[0]
    if (
        update.job_id != source_job.job_id
        or update.expected_status is not source_job.status
        or update.target_status not in allowed_statuses
        or update.started_at is not None
        or update.finished_at != occurred_at
        or update.runtime_epoch is not None
    ):
        raise ValueError("control Trigger returned an invalid Job lifecycle update")
    expected_target_case_status = {
        JobStatus.FAILED: CaseStatus.FAILED,
        JobStatus.INTERRUPTED: CaseStatus.INTERRUPTED,
    }[update.target_status]
    if plan.target_case_status is not expected_target_case_status:
        raise ValueError("control Trigger Job and Case target statuses disagree")

    return apply_transition_plan_to_case(
        aggregate.case,
        plan,
        target_state,
        created_job=None,
        processed_at=occurred_at,
    )


class JobControlService:
    """S03 implementation of the three non-Outcome JobControl operations."""

    def __init__(
        self,
        *,
        repository: StateRepository,
        publication_guard: PublicationCommitGuard,
        coordinator: Coordinator,
        asset_catalog: AssetCatalogPort,
        notifier: StateChangeNotifier,
        clock: Clock,
        ids: IdGenerator,
        max_commit_attempts: int = _MAX_COMMIT_ATTEMPTS,
    ) -> None:
        if max_commit_attempts < 1:
            raise ValueError("max_commit_attempts must be positive")
        self._repository = repository
        self._publication_guard = publication_guard
        self._coordinator = coordinator
        self._asset_catalog = asset_catalog
        self._notifier = notifier
        self._clock = clock
        self._ids = ids
        self._max_commit_attempts = max_commit_attempts

    def _commit(
        self,
        state: StateFile,
        *,
        expected_case_revision: int | None,
        mutation: StateMutation,
    ) -> CommitReceipt:
        lease = self._publication_guard.acquire()
        try:
            return self._repository.commit(
                state.generation,
                expected_case_revision,
                mutation,
            )
        finally:
            lease.release()

    def _notify(self, case_id: str, generation: int) -> None:
        # A notification is an optimization.  The committed generation is the
        # authority and notifier failure must never roll it back or mask it.
        try:
            self._notifier.notify(case_id, generation)
        except Exception:
            return

    def claim_job(self, job_id: str, runtime_epoch: str) -> ClaimReceipt:
        try:
            command = ClaimJob.model_validate(
                {"job_id": job_id, "runtime_epoch": runtime_epoch},
                strict=True,
            )
        except (TypeError, ValueError):
            raise _application_error(
                ErrorCode.VALIDATION_ERROR,
                "JobControlPort.claim_job received invalid raw input.",
            ) from None
        job_id = command.job_id
        runtime_epoch = command.runtime_epoch

        occurred_at: str | None = None
        trigger_id: str | None = None

        for attempt in range(self._max_commit_attempts):
            state = self._repository.read_snapshot()
            found = _find_job(state, job_id)
            if found is None:
                raise _application_error(
                    ErrorCode.JOB_NOT_FOUND,
                    "The requested Job does not exist.",
                )
            case_id, aggregate, job = found
            if not _claimable(aggregate, job):
                return ClaimReceipt(
                    claimed=False,
                    job=None,
                    failure_applied=False,
                    failure_code=None,
                )

            availability = self._asset_catalog.check(fixed_asset_refs(job))
            if occurred_at is None:
                occurred_at = self._clock.now()

            if availability.available:
                lifecycle = claim_lifecycle_update(
                    job,
                    runtime_epoch=runtime_epoch,
                    started_at=occurred_at,
                )
                updated_case = _case_after_claim(
                    aggregate.case,
                    occurred_at=occurred_at,
                )
                mutation = build_state_mutation(
                    upsert_case=updated_case,
                    job_lifecycle_updates=[lifecycle],
                )
                try:
                    receipt = self._commit(
                        state,
                        expected_case_revision=aggregate.case.case_revision,
                        mutation=mutation,
                    )
                except ApplicationPortError as error:
                    if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                        continue
                    raise
                running_job = _running_job(job, lifecycle)
                record_journey_event(
                    "job.claimed",
                    timestamp=occurred_at,
                    case_id=case_id,
                    job_id=job.job_id,
                    job_type=job.job_type,
                    data={
                        "runtime_epoch": runtime_epoch,
                        "generation": receipt.generation,
                        "job": running_job,
                        "case_revision": updated_case.case_revision,
                    },
                )
                self._notify(case_id, receipt.generation)
                return ClaimReceipt(
                    claimed=True,
                    job=running_job,
                    failure_applied=False,
                    failure_code=None,
                )

            if trigger_id is None:
                trigger_id = self._ids.new("trigger")
            trigger = ValidatedTrigger(
                trigger_id=trigger_id,
                trigger_type=TriggerType.ASSET_VERSION_UNAVAILABLE,
                case_id=case_id,
                expected_case_revision=aggregate.case.case_revision,
                idempotency_key=f"claim:{job_id}:{runtime_epoch}",
                payload=AssetUnavailableTriggerPayload(
                    source_job_id=job_id,
                    missing_refs=list(availability.missing_refs),
                ),
                continuation_resources=empty_continuation_resources(),
                runtime_bindings_by_job_type={},
                occurred_at=occurred_at,
            )
            try:
                decision = validate_coordinator_plan_result(
                    trigger,
                    self._coordinator.plan(
                        build_case_snapshot(state, case_id),
                        trigger,
                    ),
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The asset-unavailable Claim decision is invalid.",
                ) from None
            if isinstance(decision, ApplicationError):
                return ClaimReceipt(
                    claimed=False,
                    job=None,
                    failure_applied=False,
                    failure_code=None,
                )
            plan = decision
            try:
                updated_case = _validate_control_plan(
                    aggregate,
                    plan,
                    source_job=job,
                    target_job_statuses=[JobStatus.FAILED],
                    occurred_at=occurred_at,
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The asset-unavailable Claim plan is invalid.",
                ) from None
            mutation = build_state_mutation(
                upsert_case=updated_case,
                job_lifecycle_updates=plan.job_updates,
            )
            try:
                receipt = self._commit(
                    state,
                    expected_case_revision=aggregate.case.case_revision,
                    mutation=mutation,
                )
            except ApplicationPortError as error:
                if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                    continue
                raise
            record_journey_event(
                "job.claim.failed",
                level=logging.ERROR,
                timestamp=occurred_at,
                case_id=case_id,
                job_id=job.job_id,
                job_type=job.job_type,
                data={
                    "code": ErrorCode.ASSET_VERSION_UNAVAILABLE.value,
                    "missing_refs": availability.missing_refs,
                    "generation": receipt.generation,
                    "from_case": aggregate.case,
                    "to_case": updated_case,
                },
            )
            if aggregate.case.status is not updated_case.status:
                record_journey_event(
                    "case.status.changed",
                    timestamp=occurred_at,
                    case_id=case_id,
                    job_id=job.job_id,
                    job_type=job.job_type,
                    data={
                        "source_event": "job.claim.failed",
                        "from_status": aggregate.case.status.value,
                        "to_status": updated_case.status.value,
                        "from_case_revision": aggregate.case.case_revision,
                        "to_case_revision": updated_case.case_revision,
                        "diagnosis_state_revision": updated_case.diagnosis_state.revision,
                        "active_job_id": updated_case.active_job_id,
                        "failure": updated_case.failure,
                        "generation": receipt.generation,
                    },
                )
            self._notify(case_id, receipt.generation)
            return ClaimReceipt(
                claimed=False,
                job=None,
                failure_applied=True,
                failure_code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
            )

        raise AssertionError("claim retry loop exhausted without a terminal result")

    def report_execution_infrastructure_failure(
        self,
        job_id: str,
        runtime_epoch: str,
        failure_id: str,
        execution_failure: ExecutionFailure,
    ) -> FailureReceipt:
        try:
            command = ReportExecutionInfrastructureFailure(
                job_id=job_id,
                runtime_epoch=runtime_epoch,
                failure_id=failure_id,
                execution_failure=execution_failure,
            )
        except (TypeError, ValueError):
            raise _application_error(
                ErrorCode.VALIDATION_ERROR,
                "The execution infrastructure failure report is invalid.",
            ) from None

        occurred_at: str | None = None
        trigger_id: str | None = None
        for attempt in range(self._max_commit_attempts):
            state = self._repository.read_snapshot()
            previous = _find_failure_record(state, command.failure_id)
            if previous is not None:
                previous_case_id, _, record = previous
                same_report = (
                    record.job_id == command.job_id
                    and record.runtime_epoch == command.runtime_epoch
                    and canonical_json_sha256(record.failure)
                    == canonical_json_sha256(command.execution_failure)
                )
                if not same_report:
                    raise _application_error(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "The failure ID is already bound to different content.",
                    )
                return FailureReceipt(
                    failure_id=command.failure_id,
                    disposition=FailureReportDisposition.DUPLICATE,
                    case_view=project_case_view(state, previous_case_id),
                )

            found = _find_job(state, command.job_id)
            if found is None:
                raise _application_error(
                    ErrorCode.JOB_NOT_FOUND,
                    "The requested Job does not exist.",
                )
            case_id, aggregate, job = found
            if (
                job.status is not JobStatus.RUNNING
                or aggregate.case.active_job_id != job.job_id
                or job.runtime_epoch != command.runtime_epoch
            ):
                return FailureReceipt(
                    failure_id=command.failure_id,
                    disposition=FailureReportDisposition.STALE,
                    case_view=project_case_view(state, case_id),
                )

            if occurred_at is None:
                occurred_at = self._clock.now()
            if trigger_id is None:
                trigger_id = self._ids.new("trigger")
            trigger = ValidatedTrigger(
                trigger_id=trigger_id,
                trigger_type=TriggerType.EXECUTION_FAILED,
                case_id=case_id,
                expected_case_revision=aggregate.case.case_revision,
                idempotency_key=command.failure_id,
                payload=ExecutionFailedTriggerPayload(
                    source_job_id=job.job_id,
                    source_outcome_id=None,
                    execution_failure=command.execution_failure,
                ),
                continuation_resources=empty_continuation_resources(),
                runtime_bindings_by_job_type={},
                occurred_at=occurred_at,
            )
            try:
                decision = validate_coordinator_plan_result(
                    trigger,
                    self._coordinator.plan(
                        build_case_snapshot(state, case_id),
                        trigger,
                    ),
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The execution-failure decision is invalid.",
                ) from None
            if isinstance(decision, ApplicationError):
                if decision.code is not ErrorCode.INVALID_CASE_STATE:
                    raise ApplicationPortError(decision)
                fresh_state = self._repository.read_snapshot()
                fresh_previous = _find_failure_record(
                    fresh_state,
                    command.failure_id,
                )
                if fresh_previous is not None:
                    fresh_case_id, _, record = fresh_previous
                    same_report = (
                        record.job_id == command.job_id
                        and record.runtime_epoch == command.runtime_epoch
                        and canonical_json_sha256(record.failure)
                        == canonical_json_sha256(command.execution_failure)
                    )
                    if not same_report:
                        raise _application_error(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "The failure ID is already bound to different content.",
                        )
                    return FailureReceipt(
                        failure_id=command.failure_id,
                        disposition=FailureReportDisposition.DUPLICATE,
                        case_view=project_case_view(fresh_state, fresh_case_id),
                    )
                fresh_found = _find_job(fresh_state, command.job_id)
                if fresh_found is None:
                    raise _application_error(
                        ErrorCode.JOB_NOT_FOUND,
                        "The requested Job does not exist.",
                    )
                fresh_case_id = fresh_found[0]
                return FailureReceipt(
                    failure_id=command.failure_id,
                    disposition=FailureReportDisposition.STALE,
                    case_view=project_case_view(fresh_state, fresh_case_id),
                )
            plan = decision
            try:
                updated_case = _validate_control_plan(
                    aggregate,
                    plan,
                    source_job=job,
                    target_job_statuses=[JobStatus.FAILED, JobStatus.INTERRUPTED],
                    occurred_at=occurred_at,
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The execution-failure plan is invalid.",
                ) from None
            failure_record = ExecutionFailureRecord(
                failure_id=command.failure_id,
                job_id=job.job_id,
                runtime_epoch=command.runtime_epoch,
                failure=command.execution_failure,
                recorded_at=occurred_at,
            )
            mutation = build_state_mutation(
                upsert_case=updated_case,
                job_lifecycle_updates=plan.job_updates,
                insert_execution_failure_records=[failure_record],
            )
            target_case_view = project_case_components(
                updated_case,
                None,
                aggregate.artifacts.values(),
            )
            try:
                receipt = self._commit(
                    state,
                    expected_case_revision=aggregate.case.case_revision,
                    mutation=mutation,
                )
            except ApplicationPortError as error:
                if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                    continue
                raise
            record_journey_event(
                "job.execution.failure_applied",
                level=logging.ERROR,
                timestamp=occurred_at,
                case_id=case_id,
                job_id=job.job_id,
                job_type=job.job_type,
                data={
                    "failure_id": command.failure_id,
                    "runtime_epoch": command.runtime_epoch,
                    "failure": command.execution_failure,
                    "disposition": FailureReportDisposition.APPLIED.value,
                    "plan_reason": plan.reason,
                    "generation": receipt.generation,
                    "case_view": target_case_view,
                },
            )
            if aggregate.case.status is not updated_case.status:
                record_journey_event(
                    "case.status.changed",
                    timestamp=occurred_at,
                    case_id=case_id,
                    job_id=job.job_id,
                    job_type=job.job_type,
                    data={
                        "source_event": "job.execution.failure_applied",
                        "from_status": aggregate.case.status.value,
                        "to_status": updated_case.status.value,
                        "from_case_revision": aggregate.case.case_revision,
                        "to_case_revision": updated_case.case_revision,
                        "diagnosis_state_revision": updated_case.diagnosis_state.revision,
                        "active_job_id": updated_case.active_job_id,
                        "failure": updated_case.failure,
                        "generation": receipt.generation,
                    },
                )
            self._notify(case_id, receipt.generation)
            return FailureReceipt(
                failure_id=command.failure_id,
                disposition=FailureReportDisposition.APPLIED,
                case_view=target_case_view,
            )

        raise AssertionError("failure-report retry loop exhausted without a terminal result")

    def interrupt_previous_epoch(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryReceipt:
        try:
            command = InterruptPreviousEpoch.model_validate(
                {
                    "current_runtime_epoch": current_runtime_epoch,
                    "recovery_id": recovery_id,
                },
                strict=True,
            )
        except (TypeError, ValueError):
            raise _application_error(
                ErrorCode.VALIDATION_ERROR,
                "JobControlPort.interrupt_previous_epoch received invalid raw input.",
            ) from None
        current_runtime_epoch = command.current_runtime_epoch
        recovery_id = command.recovery_id

        record = self._ensure_recovery_started(current_runtime_epoch, recovery_id)
        if record.completed_at is not None:
            return self._recovery_receipt(record)

        for job_id in record.interrupted_job_ids:
            self._interrupt_recovery_job(
                job_id=job_id,
                current_runtime_epoch=current_runtime_epoch,
                recovery_id=recovery_id,
            )
        completed = self._complete_recovery(current_runtime_epoch, recovery_id)
        return self._recovery_receipt(completed)

    @staticmethod
    def _recovery_receipt(record: RecoveryProcessingRecord) -> RecoveryReceipt:
        return RecoveryReceipt(
            recovery_id=record.recovery_id,
            interrupted_job_ids=list(record.interrupted_job_ids),
            pending_job_ids=list(record.pending_job_ids),
        )

    def _ensure_recovery_started(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryProcessingRecord:
        started_at: str | None = None
        for attempt in range(self._max_commit_attempts):
            state = self._repository.read_snapshot()
            existing = state.recovery_processing_records.get(recovery_id)
            if existing is not None:
                if existing.current_runtime_epoch != current_runtime_epoch:
                    raise _application_error(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "The recovery ID is already bound to another runtime epoch.",
                    )
                return existing

            if any(
                epoch.runtime_epoch == current_runtime_epoch
                and epoch.recovery_id != recovery_id
                for epoch in state.runtime_epochs
            ):
                raise _application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The runtime epoch is already bound to another recovery ID.",
                )
            if any(
                item.completed_at is None and item.recovery_id != recovery_id
                for item in state.recovery_processing_records.values()
            ):
                raise _application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "Another startup recovery is still incomplete.",
                )

            interrupted_job_ids = sorted(
                job.job_id
                for aggregate in state.cases.values()
                for job in aggregate.jobs.values()
                if job.status is JobStatus.RUNNING
                and job.runtime_epoch != current_runtime_epoch
            )
            pending_job_ids = sorted(
                job.job_id
                for aggregate in state.cases.values()
                for job in aggregate.jobs.values()
                if job.status is JobStatus.PENDING
            )
            if started_at is None:
                started_at = self._clock.now()
            runtime_record = RuntimeEpochRecord(
                runtime_epoch=current_runtime_epoch,
                started_at=started_at,
                recovery_id=recovery_id,
                recovery_completed_at=None,
            )
            processing_record = RecoveryProcessingRecord(
                recovery_id=recovery_id,
                current_runtime_epoch=current_runtime_epoch,
                interrupted_job_ids=interrupted_job_ids,
                pending_job_ids=pending_job_ids,
                completed_at=None,
            )
            mutation = build_state_mutation(
                upsert_runtime_epoch_records=[runtime_record],
                upsert_recovery_processing_records=[processing_record],
            )
            try:
                self._commit(
                    state,
                    expected_case_revision=None,
                    mutation=mutation,
                )
            except ApplicationPortError as error:
                if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                    continue
                raise
            return processing_record

        raise AssertionError("recovery-start retry loop exhausted without a terminal result")

    def _interrupt_recovery_job(
        self,
        *,
        job_id: str,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> None:
        occurred_at: str | None = None
        trigger_id: str | None = None
        for attempt in range(self._max_commit_attempts):
            state = self._repository.read_snapshot()
            found = _find_job(state, job_id)
            if found is None:
                raise _application_error(
                    ErrorCode.STATE_CORRUPT,
                    "A persisted recovery Job no longer exists.",
                )
            case_id, aggregate, job = found
            if not (
                job.status is JobStatus.RUNNING
                and job.runtime_epoch != current_runtime_epoch
            ):
                return
            if (
                aggregate.case.active_job_id != job.job_id
                or aggregate.case.status is not _expected_case_status(job.job_type)
            ):
                raise _application_error(
                    ErrorCode.STATE_CORRUPT,
                    "An old-epoch RUNNING Job is not its Case active Job.",
                )

            if occurred_at is None:
                occurred_at = self._clock.now()
            if trigger_id is None:
                trigger_id = self._ids.new("trigger")
            trigger = ValidatedTrigger(
                trigger_id=trigger_id,
                trigger_type=TriggerType.MARK_OLD_EPOCH_INTERRUPTED,
                case_id=case_id,
                expected_case_revision=aggregate.case.case_revision,
                idempotency_key=recovery_id,
                payload=OldEpochTriggerPayload(
                    source_job_id=job.job_id,
                    previous_runtime_epoch=job.runtime_epoch,
                    current_runtime_epoch=current_runtime_epoch,
                ),
                continuation_resources=empty_continuation_resources(),
                runtime_bindings_by_job_type={},
                occurred_at=occurred_at,
            )
            try:
                decision = validate_coordinator_plan_result(
                    trigger,
                    self._coordinator.plan(
                        build_case_snapshot(state, case_id),
                        trigger,
                    ),
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The old-epoch interruption decision is invalid.",
                ) from None
            if isinstance(decision, ApplicationError):
                if decision.code is not ErrorCode.INVALID_CASE_STATE:
                    raise ApplicationPortError(decision)
                fresh_state = self._repository.read_snapshot()
                fresh_found = _find_job(fresh_state, job_id)
                if fresh_found is None or not (
                    fresh_found[2].status is JobStatus.RUNNING
                    and fresh_found[2].runtime_epoch != current_runtime_epoch
                ):
                    return
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    decision.message,
                    details=decision.details,
                )
            plan = decision
            try:
                updated_case = _validate_control_plan(
                    aggregate,
                    plan,
                    source_job=job,
                    target_job_statuses=[JobStatus.INTERRUPTED],
                    occurred_at=occurred_at,
                )
            except (TypeError, ValueError):
                raise _application_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The old-epoch interruption plan is invalid.",
                ) from None
            mutation = build_state_mutation(
                upsert_case=updated_case,
                job_lifecycle_updates=plan.job_updates,
            )
            try:
                receipt = self._commit(
                    state,
                    expected_case_revision=aggregate.case.case_revision,
                    mutation=mutation,
                )
            except ApplicationPortError as error:
                if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                    continue
                raise
            self._notify(case_id, receipt.generation)
            return

        raise AssertionError("recovery Job retry loop exhausted without a terminal result")

    def _complete_recovery(
        self,
        current_runtime_epoch: str,
        recovery_id: str,
    ) -> RecoveryProcessingRecord:
        completed_at: str | None = None
        for attempt in range(self._max_commit_attempts):
            state = self._repository.read_snapshot()
            processing = state.recovery_processing_records.get(recovery_id)
            if processing is None or processing.current_runtime_epoch != current_runtime_epoch:
                raise _application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The recovery audit record changed before completion.",
                )
            if processing.completed_at is not None:
                return processing
            for job_id in processing.interrupted_job_ids:
                found = _find_job(state, job_id)
                if found is None:
                    raise _application_error(
                        ErrorCode.STATE_CORRUPT,
                        "A persisted recovery Job no longer exists.",
                    )
                job = found[2]
                if (
                    job.status is JobStatus.RUNNING
                    and job.runtime_epoch != current_runtime_epoch
                ):
                    raise _application_error(
                        ErrorCode.STATE_CORRUPT,
                        "Recovery cannot complete while an old Job remains RUNNING.",
                    )

            runtime_matches = [
                item
                for item in state.runtime_epochs
                if item.recovery_id == recovery_id
            ]
            if len(runtime_matches) != 1:
                raise _application_error(
                    ErrorCode.STATE_CORRUPT,
                    "Recovery has no unique RuntimeEpochRecord.",
                )
            runtime = runtime_matches[0]
            if runtime.runtime_epoch != current_runtime_epoch:
                raise _application_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The recovery runtime epoch changed before completion.",
                )
            if completed_at is None:
                completed_at = self._clock.now()
            completed_runtime = runtime.model_copy(
                update={"recovery_completed_at": completed_at}
            )
            completed_processing = processing.model_copy(
                update={"completed_at": completed_at}
            )
            mutation = build_state_mutation(
                upsert_runtime_epoch_records=[completed_runtime],
                upsert_recovery_processing_records=[completed_processing],
            )
            try:
                self._commit(
                    state,
                    expected_case_revision=None,
                    mutation=mutation,
                )
            except ApplicationPortError as error:
                if _is_revision_conflict(error) and attempt + 1 < self._max_commit_attempts:
                    continue
                raise
            return completed_processing

        raise AssertionError("recovery-completion retry loop exhausted without a terminal result")


__all__ = ["JobControlService"]
