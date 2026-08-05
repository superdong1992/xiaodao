"""External non-streaming command orchestration for the application service.

The handler owns the five S03 write commands whose request bodies are already
materialized DTOs.  ``UploadAttachmentContent`` is intentionally handled by
the upload-specific service because its forward-only stream has a different
locking and retry boundary.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    ApplicationResponse,
    AssetCatalogPort,
    AssetUnavailableTriggerPayload,
    AttachmentRequirementConstraints,
    AttachmentStatus,
    BusinessReceipt,
    CancelCase,
    CancelCaseTriggerPayload,
    Case,
    CaseAggregate,
    CaseSnapshot,
    CaseStatus,
    Clock,
    CommitReceipt,
    ContextSnapshotProjector,
    Coordinator,
    CreateCase,
    DiagnosisItem,
    DiagnosisState,
    DiagnosisStateDelta,
    Dispatcher,
    ErrorCode,
    ExecutionRecordStore,
    IdGenerator,
    InputRequirementConstraints,
    Job,
    JobStatus,
    JobType,
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
    PrepareAttachment,
    PublicationCommitGuard,
    RequirementKind,
    RequirementStatus,
    ResourceStore,
    ResumeCase,
    ResumeInterruptedTriggerPayload,
    RuntimeBindings,
    StateChangeNotifier,
    StateFile,
    StateMutation,
    StateRepository,
    SubmitSupplement,
    SubmitSupplementTriggerPayload,
    TransitionPlan,
    TriggerType,
    UserFactInput,
    ValidatedTrigger,
    VersionedRef,
    validate_coordinator_plan_result,
)
from problem_locator.journey import record_journey_event

from .formalization import apply_diagnosis_state_delta, build_job
from .errors import raise_port_error as _raise_shared_port_error
from .idempotency import (
    IdempotencyDisposition,
    decide_idempotency,
    make_idempotency_record,
)
from .mutations import apply_transition_plan_to_case, build_state_mutation
from .preparation import (
    build_create_case_trigger,
    build_uploading_attachment,
    fixed_asset_refs,
    make_user_fact,
    runtime_bindings_from_job,
)
from .projection import (
    build_case_snapshot,
    continuation_for_resume,
    continuation_for_supplement,
    empty_continuation_resources,
    project_case_view,
)
from .runtime_bindings import (
    rebuild_runtime_bindings_for_role,
    runtime_bindings_from_job_spec,
)


ExternalNonUploadCommand = (
    CreateCase | PrepareAttachment | SubmitSupplement | ResumeCase | CancelCase
)

_TERMINAL_CASE_STATUSES = {
    CaseStatus.RESOLVED,
    CaseStatus.FAILED,
    CaseStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class _CommittedCommand:
    receipt: BusinessReceipt
    generation: int
    occurred_at: str
    event: str
    request_id: str
    committed_case: Case
    data: dict[str, Any]
    previous_case: Case | None = None
    created_job: Job | None = None
    submit_job: Job | None = None
    cancel_job: Job | None = None


def _detail(
    *,
    field: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    expected: str | int | bool | None = None,
    actual: str | int | bool | None = None,
    limit: int | None = None,
    observed: int | None = None,
) -> ApplicationErrorDetail:
    return ApplicationErrorDetail(
        field=field,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_ref=None,
        expected=expected,
        actual=actual,
        limit=limit,
        observed=observed,
    )


def _raise_port_error(
    code: ErrorCode,
    message: str,
    details: Sequence[ApplicationErrorDetail] = (),
) -> None:
    _raise_shared_port_error(code, message, details=details)


def _validated_catalog_bindings(
    job_type: JobType,
    bindings: RuntimeBindings,
    *,
    expected_skill_ref: VersionedRef | None = None,
) -> RuntimeBindings:
    try:
        return rebuild_runtime_bindings_for_role(
            job_type,
            bindings,
            expected_skill_ref=expected_skill_ref,
        )
    except (TypeError, ValueError):
        _raise_port_error(
            ErrorCode.CONFIG_INVALID,
            "The Asset Catalog returned invalid runtime bindings.",
        )


def _default_stable_target_detector(
    case: Case,
    inputs: Mapping[str, str],
) -> bool:
    """Conservatively identify explicit replacements of stable target fields.

    Requirement names are the only frozen structured names available to S03.
    A deployment may inject a stricter detector, but exact names of ProblemSpec
    target fields have deterministic default semantics.
    """

    spec = case.diagnosis_state.problem_spec
    scalar_fields = {
        "statement",
        "expected_behavior",
        "actual_behavior",
        "scope",
    }
    collection_fields = {
        "goals",
        "non_goals",
        "constraints",
        "completion_criteria",
    }
    return any(
        (name in scalar_fields and value != getattr(spec, name))
        or name in collection_fields
        for name, value in inputs.items()
    )


class ExternalCommandHandler:
    """Synchronous S03 handler for the five non-streaming external commands."""

    def __init__(
        self,
        *,
        repository: StateRepository,
        coordinator: Coordinator,
        projector: ContextSnapshotProjector,
        publication_guard: PublicationCommitGuard,
        resource_store: ResourceStore,
        execution_records: ExecutionRecordStore,
        asset_catalog: AssetCatalogPort,
        dispatcher: Dispatcher,
        notifier: StateChangeNotifier,
        clock: Clock,
        ids: IdGenerator,
        stable_target_detector: Callable[[Case, Mapping[str, str]], bool]
        | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._projector = projector
        self._publication_guard = publication_guard
        self._resource_store = resource_store
        self._execution_records = execution_records
        self._asset_catalog = asset_catalog
        self._dispatcher = dispatcher
        self._notifier = notifier
        self._clock = clock
        self._ids = ids
        self._stable_target_detector = (
            stable_target_detector or _default_stable_target_detector
        )
        self._monotonic = monotonic

    def execute(self, command: ExternalNonUploadCommand) -> ApplicationResponse:
        try:
            if isinstance(command, CreateCase):
                return self._create_case(command)
            if isinstance(command, PrepareAttachment):
                return self._prepare_attachment(command)
            if isinstance(command, SubmitSupplement):
                return self._submit_supplement(command)
            if isinstance(command, ResumeCase):
                return self._resume_case(command)
            if isinstance(command, CancelCase):
                return self._cancel_case(command)
        except ApplicationPortError:
            raise
        except (TypeError, ValueError):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The command could not be applied to the validated state.",
            )
        raise TypeError("ExternalCommandHandler received an unsupported command")

    def _create_case(self, command: CreateCase) -> ApplicationResponse:
        occurred_at: str | None = None
        case_id: str | None = None
        trigger_id: str | None = None
        job_id: str | None = None
        user_fact_ids: list[str] | None = None
        route_bindings = None

        for attempt in range(3):
            snapshot = self._repository.read_snapshot()
            replay = self._idempotency_result(snapshot, command)
            if replay is not None:
                return self._respond(snapshot, replay, command.wait_seconds)

            if occurred_at is None:
                occurred_at = self._clock.now()
                case_id = self._ids.new("case")
                trigger_id = self._ids.new("trigger")
                job_id = self._ids.new("job")
                user_fact_ids = [
                    self._ids.new("diagnosis_item")
                    for _ in command.initial_user_facts
                ]
                route_bindings = _validated_catalog_bindings(
                    JobType.ROUTE,
                    self._asset_catalog.route_bindings(),
                )

            assert case_id is not None
            assert trigger_id is not None
            assert job_id is not None
            assert user_fact_ids is not None
            assert occurred_at is not None
            assert route_bindings is not None
            if case_id in snapshot.cases:
                _raise_port_error(
                    ErrorCode.REVISION_CONFLICT,
                    "The allocated Case ID is no longer available.",
                    [_detail(resource_type="case", resource_id=case_id)],
                )

            trigger = build_create_case_trigger(
                command,
                case_id=case_id,
                trigger_id=trigger_id,
                user_fact_ids=user_fact_ids,
                route_bindings=route_bindings,
                occurred_at=occurred_at,
            )
            provisional_case = self._provisional_case(trigger, occurred_at)
            coordinator_snapshot = CaseSnapshot(
                case=provisional_case,
                active_job=None,
                resume_source_job=None,
                replacement_job_ids_by_source={},
            )
            plan = self._plan(coordinator_snapshot, trigger)
            self._validate_external_plan(
                plan,
                trigger.runtime_bindings_by_job_type,
            )
            if (
                plan.next_job_spec is None
                or plan.next_job_spec.job_type is not JobType.ROUTE
                or plan.job_updates
                or plan.target_case_status is not CaseStatus.RUNNING
                or plan.clear_active_job
                or plan.selected_skill_update is not None
                or plan.case_failure_update is not None
                or plan.candidate_mutation is not None
                or plan.final_result_target is not None
                or not self._delta_is_empty(plan.accepted_state_delta)
            ):
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The CreateCase transition plan is invalid.",
                )

            target_state = apply_diagnosis_state_delta(
                provisional_case.diagnosis_state,
                plan.accepted_state_delta,
                evidence_ids_by_proposal_key={},
                candidate_mutation=plan.candidate_mutation,
                candidates_by_proposal_key={},
                expected_target_revision=1,
            )
            created_job = build_job(
                plan.next_job_spec,
                job_id=job_id,
                case_id=case_id,
                created_at=occurred_at,
                target_diagnosis_state=target_state,
                projector=self._projector,
                existing_evidence_ids=(),
                evidence_ids_by_proposal_key={},
                existing_artifact_ids=(),
                artifact_ids_by_proposal_key={},
                existing_candidate=None,
                candidates_by_proposal_key={},
            )
            created_case = apply_transition_plan_to_case(
                provisional_case,
                plan,
                target_state,
                created_job=created_job,
                processed_at=occurred_at,
            )
            created_case_payload = created_case.model_dump(mode="python")
            created_case_payload["case_revision"] = 1
            created_case = Case.model_validate(created_case_payload)
            receipt = BusinessReceipt(
                operation="CreateCase",
                primary_resource_id=case_id,
                case_id=case_id,
                case_revision=1,
                job_id=job_id,
                status=created_case.status.value,
            )
            record = make_idempotency_record(
                command,
                decide_idempotency(snapshot, command).request_hash,
                receipt,
                case_id=case_id,
                created_at=occurred_at,
            )
            mutation = build_state_mutation(
                upsert_case=created_case,
                insert_jobs=[created_job],
                insert_idempotency_records=[record],
            )
            try:
                commit = self._commit(
                    snapshot.generation,
                    None,
                    mutation,
                    publish_job=created_job,
                )
            except ApplicationPortError as error:
                if self._retry_revision_conflict(error, attempt):
                    continue
                raise
            return self._after_commit(
                _CommittedCommand(
                    receipt=receipt,
                    generation=commit.generation,
                    occurred_at=occurred_at,
                    event="case.created",
                    request_id=command.idempotency_key,
                    committed_case=created_case,
                    data={
                        "operation": receipt.operation,
                        "problem_spec": command.problem_spec,
                        "initial_user_facts": command.initial_user_facts,
                        "case": created_case,
                        "created_job": created_job,
                    },
                    created_job=created_job,
                    submit_job=created_job,
                ),
                wait_seconds=command.wait_seconds,
            )
        raise AssertionError("three-attempt retry loop exhausted unexpectedly")

    def _prepare_attachment(
        self,
        command: PrepareAttachment,
    ) -> ApplicationResponse:
        occurred_at: str | None = None
        attachment_id: str | None = None

        for attempt in range(3):
            snapshot = self._repository.read_snapshot()
            replay = self._idempotency_result(snapshot, command)
            if replay is not None:
                return self._respond(snapshot, replay, 0)
            aggregate = self._require_case(snapshot, command.case_id)
            self._require_expected_revision(
                aggregate.case,
                command.expected_case_revision,
            )
            self._require_nonterminal(aggregate.case)
            if command.declared_size is not None and command.declared_size > MAX_ATTACHMENT_BYTES:
                _raise_port_error(
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    "The declared Attachment size exceeds the V1 limit.",
                    [
                        _detail(
                            field="declared_size",
                            limit=MAX_ATTACHMENT_BYTES,
                            observed=command.declared_size,
                        )
                    ],
                )

            if occurred_at is None:
                occurred_at = self._clock.now()
                attachment_id = self._ids.new("attachment")
            assert occurred_at is not None
            assert attachment_id is not None

            attachment = build_uploading_attachment(
                command,
                attachment_id=attachment_id,
                occurred_at=occurred_at,
            )
            case_payload = aggregate.case.model_dump(mode="python")
            case_payload.update(
                case_revision=aggregate.case.case_revision + 1,
                updated_at=occurred_at,
            )
            target_case = Case.model_validate(case_payload)
            receipt = BusinessReceipt(
                operation="PrepareAttachment",
                primary_resource_id=attachment_id,
                case_id=command.case_id,
                case_revision=target_case.case_revision,
                job_id=None,
                status=AttachmentStatus.UPLOADING.value,
            )
            record = make_idempotency_record(
                command,
                decide_idempotency(snapshot, command).request_hash,
                receipt,
                case_id=command.case_id,
                created_at=occurred_at,
            )
            mutation = build_state_mutation(
                upsert_case=target_case,
                upsert_attachments=[attachment],
                insert_idempotency_records=[record],
            )
            lease = self._publication_guard.acquire()
            try:
                usage = self._resource_store.validate_case_capacity(
                    command.case_id,
                    [],
                )
                # r3 makes ``CaseResourceUsage.total_bytes`` the authoritative
                # observation, including formal outbox and orphan bytes.  An
                # empty target batch has ``new_bytes == 0`` today, but consume
                # the frozen total directly rather than reconstructing it.
                observed = usage.total_bytes + (command.declared_size or 0)
                if observed > MAX_CASE_RESOURCE_BYTES:
                    _raise_port_error(
                        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                        "The Case resource capacity would be exceeded.",
                        [
                            _detail(
                                field="declared_size",
                                resource_type="case",
                                resource_id=command.case_id,
                                limit=MAX_CASE_RESOURCE_BYTES,
                                observed=observed,
                            )
                        ],
                    )
                commit = self._repository.commit(
                    snapshot.generation,
                    aggregate.case.case_revision,
                    mutation,
                )
            except ApplicationPortError as error:
                if error.error.code is ErrorCode.PATH_VIOLATION:
                    _raise_port_error(
                        ErrorCode.RESOURCE_PUBLISH_FAILED,
                        "The Attachment capacity preflight could not be completed.",
                    )
                if self._retry_revision_conflict(error, attempt):
                    continue
                raise
            finally:
                lease.release()
            return self._after_commit(
                _CommittedCommand(
                    receipt=receipt,
                    generation=commit.generation,
                    occurred_at=occurred_at,
                    event="attachment.prepared",
                    request_id=command.idempotency_key,
                    committed_case=target_case,
                    previous_case=aggregate.case,
                    data={
                        "operation": receipt.operation,
                        "attachment": attachment,
                        "declared_size": command.declared_size,
                        "declared_sha256": command.declared_sha256,
                        "case_revision": target_case.case_revision,
                    },
                ),
                wait_seconds=0,
            )
        raise AssertionError("three-attempt retry loop exhausted unexpectedly")

    def _submit_supplement(
        self,
        command: SubmitSupplement,
    ) -> ApplicationResponse:
        occurred_at: str | None = None
        trigger_id: str | None = None
        job_id: str | None = None
        fact_ids_by_name: dict[str, str] | None = None
        fixed_diagnose_bindings: RuntimeBindings | None = None

        for attempt in range(3):
            snapshot = self._repository.read_snapshot()
            replay = self._idempotency_result(snapshot, command)
            if replay is not None:
                return self._respond(snapshot, replay, command.wait_seconds)
            aggregate = self._require_case(snapshot, command.case_id)
            self._require_expected_revision(
                aggregate.case,
                command.expected_case_revision,
            )
            if aggregate.case.status not in {
                CaseStatus.WAITING_INPUT,
                CaseStatus.WAITING_ATTACHMENT,
            }:
                _raise_port_error(
                    ErrorCode.INVALID_CASE_STATE,
                    "SubmitSupplement requires a Case waiting for input or an Attachment.",
                    [
                        _detail(
                            resource_type="case",
                            resource_id=command.case_id,
                            expected="WAITING_INPUT|WAITING_ATTACHMENT",
                            actual=aggregate.case.status.value,
                        )
                    ],
                )
            self._validate_supplement_inputs(aggregate.case, command.inputs)
            self._validate_supplement_attachments(
                snapshot,
                aggregate.case,
                command.attachment_ids,
            )

            if occurred_at is None:
                occurred_at = self._clock.now()
                trigger_id = self._ids.new("trigger")
                job_id = self._ids.new("job")
                fact_ids_by_name = {
                    name: self._ids.new("diagnosis_item")
                    for name in sorted(command.inputs)
                }
            assert occurred_at is not None
            assert trigger_id is not None
            assert job_id is not None
            assert fact_ids_by_name is not None

            user_facts = [
                make_user_fact(
                    UserFactInput(name=name, value=command.inputs[name]),
                    item_id=fact_ids_by_name[name],
                    trigger_id=trigger_id,
                    created_revision=aggregate.case.diagnosis_state.revision + 1,
                )
                for name in sorted(command.inputs)
            ]
            selected_skill = aggregate.case.selected_skill_ref
            if selected_skill is None:
                _raise_port_error(
                    ErrorCode.INVALID_CASE_STATE,
                    "The waiting Case has no selected diagnosis skill.",
                )
            if fixed_diagnose_bindings is None:
                fixed_diagnose_bindings = _validated_catalog_bindings(
                    JobType.DIAGNOSE,
                    self._asset_catalog.diagnose_bindings(selected_skill),
                    expected_skill_ref=selected_skill,
                )
            trigger = ValidatedTrigger(
                trigger_id=trigger_id,
                trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
                case_id=command.case_id,
                expected_case_revision=command.expected_case_revision,
                idempotency_key=command.idempotency_key,
                payload=SubmitSupplementTriggerPayload(
                    user_facts=user_facts,
                    ready_attachment_ids=list(command.attachment_ids),
                    stable_target_changed=self._stable_target_detector(
                        aggregate.case,
                        command.inputs,
                    ),
                ),
                continuation_resources=continuation_for_supplement(
                    snapshot,
                    command.case_id,
                    ready_attachment_ids=command.attachment_ids,
                ),
                runtime_bindings_by_job_type={
                    JobType.DIAGNOSE: fixed_diagnose_bindings
                },
                occurred_at=occurred_at,
            )
            plan = self._plan(build_case_snapshot(snapshot, command.case_id), trigger)
            self._validate_external_plan(
                plan,
                trigger.runtime_bindings_by_job_type,
            )
            self._validate_supplement_plan(plan, user_facts)
            try:
                committed = self._commit_case_plan(
                    snapshot,
                    command,
                    plan,
                    occurred_at=occurred_at,
                    next_job_id=job_id,
                )
            except ApplicationPortError as error:
                if self._retry_revision_conflict(error, attempt):
                    continue
                raise
            return self._after_commit(
                committed,
                wait_seconds=command.wait_seconds,
            )
        raise AssertionError("three-attempt retry loop exhausted unexpectedly")

    def _resume_case(self, command: ResumeCase) -> ApplicationResponse:
        occurred_at: str | None = None
        trigger_id: str | None = None
        replacement_job_id: str | None = None

        for attempt in range(3):
            snapshot = self._repository.read_snapshot()
            replay = self._idempotency_result(snapshot, command)
            if replay is not None:
                return self._respond(snapshot, replay, command.wait_seconds)
            aggregate = self._require_case(snapshot, command.case_id)
            self._require_expected_revision(
                aggregate.case,
                command.expected_case_revision,
            )

            if occurred_at is None:
                occurred_at = self._clock.now()
            assert occurred_at is not None

            active = (
                None
                if aggregate.case.active_job_id is None
                else aggregate.jobs[aggregate.case.active_job_id]
            )
            if active is not None:
                if active.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
                    _raise_port_error(
                        ErrorCode.INVALID_CASE_STATE,
                        "The Case active Job is not resumable.",
                    )
                receipt = BusinessReceipt(
                    operation="ResumeCase",
                    primary_resource_id=command.case_id,
                    case_id=command.case_id,
                    case_revision=aggregate.case.case_revision,
                    job_id=active.job_id,
                    status=aggregate.case.status.value,
                )
                record = make_idempotency_record(
                    command,
                    decide_idempotency(snapshot, command).request_hash,
                    receipt,
                    case_id=command.case_id,
                    created_at=occurred_at,
                )
                mutation = build_state_mutation(
                    upsert_case=aggregate.case,
                    insert_idempotency_records=[record],
                )
                try:
                    commit = self._commit(
                        snapshot.generation,
                        aggregate.case.case_revision,
                        mutation,
                    )
                except ApplicationPortError as error:
                    if self._retry_revision_conflict(error, attempt):
                        continue
                    raise
                return self._after_commit(
                    _CommittedCommand(
                        receipt=receipt,
                        generation=commit.generation,
                        occurred_at=occurred_at,
                        event="case.resumed",
                        request_id=command.idempotency_key,
                        committed_case=aggregate.case,
                        previous_case=aggregate.case,
                        data={
                            "operation": receipt.operation,
                            "active_job": active,
                            "resignalled": active.status is JobStatus.PENDING,
                        },
                        submit_job=(
                            active if active.status is JobStatus.PENDING else None
                        ),
                    ),
                    wait_seconds=command.wait_seconds,
                )

            if aggregate.case.status is not CaseStatus.INTERRUPTED:
                _raise_port_error(
                    ErrorCode.INVALID_CASE_STATE,
                    "ResumeCase requires an interrupted Case or an active Job.",
                    [
                        _detail(
                            resource_type="case",
                            resource_id=command.case_id,
                            expected=CaseStatus.INTERRUPTED.value,
                            actual=aggregate.case.status.value,
                        )
                    ],
                )
            if trigger_id is None:
                trigger_id = self._ids.new("trigger")
                replacement_job_id = self._ids.new("job")
            assert trigger_id is not None
            assert replacement_job_id is not None
            case_snapshot = build_case_snapshot(snapshot, command.case_id)
            source_job = case_snapshot.resume_source_job
            if source_job is None:
                _raise_port_error(
                    ErrorCode.INVALID_CASE_STATE,
                    "The interrupted Case has no unreplaced Job.",
                )
            availability = self._asset_catalog.check(fixed_asset_refs(source_job))
            if availability.available:
                trigger = ValidatedTrigger(
                    trigger_id=trigger_id,
                    trigger_type=TriggerType.RESUME_INTERRUPTED,
                    case_id=command.case_id,
                    expected_case_revision=command.expected_case_revision,
                    idempotency_key=command.idempotency_key,
                    payload=ResumeInterruptedTriggerPayload(
                        source_job_id=source_job.job_id,
                    ),
                    continuation_resources=continuation_for_resume(
                        snapshot,
                        command.case_id,
                    ),
                    runtime_bindings_by_job_type={
                        source_job.job_type: runtime_bindings_from_job(source_job)
                    },
                    occurred_at=occurred_at,
                )
            else:
                trigger = ValidatedTrigger(
                    trigger_id=trigger_id,
                    trigger_type=TriggerType.ASSET_VERSION_UNAVAILABLE,
                    case_id=command.case_id,
                    expected_case_revision=command.expected_case_revision,
                    idempotency_key=command.idempotency_key,
                    payload=AssetUnavailableTriggerPayload(
                        source_job_id=source_job.job_id,
                        missing_refs=availability.missing_refs,
                    ),
                    continuation_resources=empty_continuation_resources(),
                    runtime_bindings_by_job_type={},
                    occurred_at=occurred_at,
                )
            plan = self._plan(case_snapshot, trigger)
            self._validate_external_plan(
                plan,
                trigger.runtime_bindings_by_job_type,
            )
            self._validate_resume_plan(
                plan,
                source_job,
                assets_available=availability.available,
            )
            try:
                committed = self._commit_case_plan(
                    snapshot,
                    command,
                    plan,
                    occurred_at=occurred_at,
                    next_job_id=replacement_job_id,
                )
            except ApplicationPortError as error:
                if self._retry_revision_conflict(error, attempt):
                    continue
                raise
            return self._after_commit(
                committed,
                wait_seconds=command.wait_seconds,
            )
        raise AssertionError("three-attempt retry loop exhausted unexpectedly")

    def _cancel_case(self, command: CancelCase) -> ApplicationResponse:
        occurred_at: str | None = None
        trigger_id: str | None = None

        for attempt in range(3):
            snapshot = self._repository.read_snapshot()
            replay = self._idempotency_result(snapshot, command)
            if replay is not None:
                return self._respond(snapshot, replay, 0)
            aggregate = self._require_case(snapshot, command.case_id)
            self._require_expected_revision(
                aggregate.case,
                command.expected_case_revision,
            )
            self._require_nonterminal(aggregate.case)
            if occurred_at is None:
                occurred_at = self._clock.now()
                trigger_id = self._ids.new("trigger")
            assert occurred_at is not None
            assert trigger_id is not None
            active_job_id = aggregate.case.active_job_id
            trigger = ValidatedTrigger(
                trigger_id=trigger_id,
                trigger_type=TriggerType.CANCEL_CASE,
                case_id=command.case_id,
                expected_case_revision=command.expected_case_revision,
                idempotency_key=command.idempotency_key,
                payload=CancelCaseTriggerPayload(
                    reason="USER_CANCEL",
                    active_job_id=active_job_id,
                ),
                continuation_resources=empty_continuation_resources(),
                runtime_bindings_by_job_type={},
                occurred_at=occurred_at,
            )
            plan = self._plan(build_case_snapshot(snapshot, command.case_id), trigger)
            self._validate_external_plan(
                plan,
                trigger.runtime_bindings_by_job_type,
            )
            self._validate_cancel_plan(
                plan,
                aggregate,
                occurred_at=occurred_at,
            )
            try:
                committed = self._commit_case_plan(
                    snapshot,
                    command,
                    plan,
                    occurred_at=occurred_at,
                    next_job_id=None,
                    cancel_job_id=active_job_id,
                )
            except ApplicationPortError as error:
                if self._retry_revision_conflict(error, attempt):
                    continue
                raise
            return self._after_commit(committed, wait_seconds=0)
        raise AssertionError("three-attempt retry loop exhausted unexpectedly")

    def _commit_case_plan(
        self,
        snapshot: StateFile,
        command: SubmitSupplement | ResumeCase | CancelCase,
        plan: TransitionPlan,
        *,
        occurred_at: str,
        next_job_id: str | None,
        cancel_job_id: str | None = None,
    ) -> _CommittedCommand:
        aggregate = snapshot.cases[command.case_id]
        target_state = apply_diagnosis_state_delta(
            aggregate.case.diagnosis_state,
            plan.accepted_state_delta,
            evidence_ids_by_proposal_key={},
            candidate_mutation=plan.candidate_mutation,
            candidates_by_proposal_key={},
            expected_target_revision=(
                None
                if plan.next_job_spec is None
                else plan.next_job_spec.target_state_revision
            ),
        )
        created_job: Job | None = None
        if plan.next_job_spec is not None:
            if next_job_id is None:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The transition unexpectedly creates a Job.",
                )
            created_job = build_job(
                plan.next_job_spec,
                job_id=next_job_id,
                case_id=command.case_id,
                created_at=occurred_at,
                target_diagnosis_state=target_state,
                projector=self._projector,
                existing_evidence_ids=aggregate.evidence,
                evidence_ids_by_proposal_key={},
                existing_artifact_ids=aggregate.artifacts,
                artifact_ids_by_proposal_key={},
                existing_candidate=aggregate.case.diagnosis_state.candidate_conclusion,
                candidates_by_proposal_key={},
            )
        target_case = apply_transition_plan_to_case(
            aggregate.case,
            plan,
            target_state,
            created_job=created_job,
            processed_at=occurred_at,
        )
        if isinstance(command, SubmitSupplement):
            self._validate_supplement_target(
                aggregate.case,
                target_case,
                plan.accepted_state_delta.add_user_facts,
                command.attachment_ids,
            )
        receipt = BusinessReceipt(
            operation=type(command).__name__,
            primary_resource_id=command.case_id,
            case_id=command.case_id,
            case_revision=target_case.case_revision,
            job_id=(
                created_job.job_id
                if created_job is not None
                else cancel_job_id
            ),
            status=target_case.status.value,
        )
        record = make_idempotency_record(
            command,
            decide_idempotency(snapshot, command).request_hash,
            receipt,
            case_id=command.case_id,
            created_at=occurred_at,
        )
        mutation = build_state_mutation(
            upsert_case=target_case,
            insert_jobs=[] if created_job is None else [created_job],
            job_lifecycle_updates=plan.job_updates,
            insert_idempotency_records=[record],
        )
        commit = self._commit(
            snapshot.generation,
            aggregate.case.case_revision,
            mutation,
            publish_job=created_job,
        )
        event = {
            SubmitSupplement: "case.supplement.applied",
            ResumeCase: "case.resumed",
            CancelCase: "case.cancelled",
        }[type(command)]
        return _CommittedCommand(
            receipt=receipt,
            generation=commit.generation,
            occurred_at=occurred_at,
            event=event,
            request_id=command.idempotency_key,
            committed_case=target_case,
            previous_case=aggregate.case,
            created_job=created_job,
            submit_job=created_job,
            cancel_job=(
                None if cancel_job_id is None else aggregate.jobs[cancel_job_id]
            ),
            data={
                "operation": receipt.operation,
                "command": command,
                "plan_reason": plan.reason,
                "from_case": aggregate.case,
                "to_case": target_case,
                "created_job": created_job,
                "cancelled_job_id": cancel_job_id,
            },
        )

    def _commit(
        self,
        generation: int,
        case_revision: int | None,
        mutation: StateMutation,
        *,
        publish_job: Job | None = None,
    ) -> CommitReceipt:
        lease = self._publication_guard.acquire()
        try:
            if publish_job is not None:
                self._execution_records.publish_job(publish_job)
            return self._repository.commit(generation, case_revision, mutation)
        finally:
            lease.release()

    def _idempotency_result(
        self,
        snapshot: StateFile,
        command: ExternalNonUploadCommand,
    ) -> BusinessReceipt | None:
        decision = decide_idempotency(snapshot, command)
        if decision.disposition is IdempotencyDisposition.CONFLICT:
            _raise_port_error(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The idempotency key is already bound to another request.",
                [_detail(field="idempotency_key", actual=command.idempotency_key)],
            )
        if decision.disposition is IdempotencyDisposition.REPLAY:
            assert decision.record is not None
            return decision.record.business_receipt
        return None

    def _plan(
        self,
        snapshot: CaseSnapshot,
        trigger: ValidatedTrigger,
    ) -> TransitionPlan:
        result = validate_coordinator_plan_result(
            trigger,
            self._coordinator.plan(snapshot, trigger),
        )
        if isinstance(result, ApplicationError):
            raise ApplicationPortError(result)
        if not isinstance(result, TransitionPlan):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The Coordinator returned an invalid result.",
            )
        return result

    @staticmethod
    def _validate_external_plan(
        plan: TransitionPlan,
        offered_bindings: Mapping[JobType, RuntimeBindings],
    ) -> None:
        if (
            plan.outcome_disposition is not None
            or plan.accepted_evidence_proposal_keys
            or plan.accepted_artifact_proposal_keys
            or plan.accepted_candidate_proposal_key is not None
        ):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "An external command plan cannot accept Outcome proposals.",
            )
        spec = plan.next_job_spec
        if spec is None:
            return
        expected = offered_bindings.get(spec.job_type)
        if expected is None or runtime_bindings_from_job_spec(spec) != expected:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The Coordinator replaced the trigger's pinned runtime bindings.",
            )

    @staticmethod
    def _delta_is_empty(delta: DiagnosisStateDelta) -> bool:
        return (
            getattr(delta, "problem_spec_patch") is None
            and not getattr(delta, "add_user_facts")
            and not getattr(delta, "proposed_facts")
            and not getattr(delta, "add_active_hypotheses")
            and not getattr(delta, "update_hypotheses")
            and not getattr(delta, "reject_hypotheses")
            and not getattr(delta, "add_open_questions")
            and not getattr(delta, "resolve_questions")
            and not getattr(delta, "add_pending_requirements")
            and not getattr(delta, "fulfill_requirements")
            and not getattr(delta, "add_evidence_bindings")
        )

    @classmethod
    def _validate_supplement_plan(
        cls,
        plan: TransitionPlan,
        user_facts: Sequence[DiagnosisItem],
    ) -> None:
        delta = plan.accepted_state_delta
        disallowed_delta = (
            delta.problem_spec_patch is not None
            or delta.proposed_facts
            or delta.add_active_hypotheses
            or delta.update_hypotheses
            or delta.reject_hypotheses
            or delta.add_open_questions
            or delta.resolve_questions
            or delta.add_pending_requirements
            or delta.add_evidence_bindings
        )
        next_job = plan.next_job_spec
        invalid_target = (
            plan.target_case_status is CaseStatus.RUNNING
            and (
                next_job is None
                or next_job.job_type is not JobType.DIAGNOSE
                or next_job.replacement_for_job_id is not None
            )
        ) or (
            plan.target_case_status
            in {CaseStatus.WAITING_INPUT, CaseStatus.WAITING_ATTACHMENT}
            and next_job is not None
        ) or plan.target_case_status not in {
            CaseStatus.RUNNING,
            CaseStatus.WAITING_INPUT,
            CaseStatus.WAITING_ATTACHMENT,
        }
        if (
            disallowed_delta
            or delta.add_user_facts
            != sorted(user_facts, key=lambda item: item.item_id)
            or plan.job_updates
            or plan.clear_active_job
            or plan.selected_skill_update is not None
            or plan.case_failure_update is not None
            or plan.candidate_mutation is not None
            or plan.final_result_target is not None
            or invalid_target
        ):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The SubmitSupplement transition plan is invalid.",
            )

    @classmethod
    def _validate_resume_plan(
        cls,
        plan: TransitionPlan,
        source_job: Job,
        *,
        assets_available: bool,
    ) -> None:
        common_invalid = (
            not cls._delta_is_empty(plan.accepted_state_delta)
            or plan.job_updates
            or plan.selected_skill_update is not None
            or plan.candidate_mutation is not None
            or plan.final_result_target is not None
        )
        if assets_available:
            next_job = plan.next_job_spec
            expected_status = (
                CaseStatus.REVIEWING
                if source_job.job_type is JobType.REVIEW
                else CaseStatus.RUNNING
            )
            invalid = (
                common_invalid
                or plan.case_failure_update is not None
                or next_job is None
                or next_job.job_type is not source_job.job_type
                or next_job.replacement_for_job_id != source_job.job_id
                or plan.target_case_status is not expected_status
            )
        else:
            invalid = (
                common_invalid
                or plan.target_case_status is not CaseStatus.FAILED
                or plan.next_job_spec is not None
                or plan.case_failure_update is None
            )
        if invalid:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The ResumeCase transition plan is invalid.",
            )

    @classmethod
    def _validate_cancel_plan(
        cls,
        plan: TransitionPlan,
        aggregate: CaseAggregate,
        *,
        occurred_at: str,
    ) -> None:
        active_job_id = aggregate.case.active_job_id
        if active_job_id is None:
            lifecycle_invalid = bool(plan.job_updates)
        else:
            active_job = aggregate.jobs.get(active_job_id)
            lifecycle_invalid = active_job is None or len(plan.job_updates) != 1
            if not lifecycle_invalid:
                update = plan.job_updates[0]
                lifecycle_invalid = (
                    update.job_id != active_job.job_id
                    or update.expected_status is not active_job.status
                    or update.target_status is not JobStatus.CANCELLED
                    or update.started_at is not None
                    or update.finished_at != occurred_at
                    or update.runtime_epoch is not None
                )
        if (
            plan.target_case_status is not CaseStatus.CANCELLED
            or plan.next_job_spec is not None
            or not plan.clear_active_job
            or not cls._delta_is_empty(plan.accepted_state_delta)
            or plan.selected_skill_update is not None
            or plan.case_failure_update is not None
            or plan.candidate_mutation is not None
            or plan.final_result_target is not None
            or lifecycle_invalid
        ):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The CancelCase transition plan is invalid.",
            )

    def _provisional_case(
        self,
        trigger: ValidatedTrigger,
        occurred_at: str,
    ) -> Case:
        payload = trigger.payload
        if trigger.trigger_type is not TriggerType.CREATE_CASE:
            raise ValueError("CREATE_CASE trigger required")
        diagnosis_state = DiagnosisState(
            revision=1,
            problem_spec=payload.problem_spec,
            user_facts=list(payload.initial_user_facts),
            confirmed_facts=[],
            active_hypotheses=[],
            rejected_hypotheses=[],
            open_questions=[],
            pending_requirements=[],
            evidence_refs=[],
            candidate_conclusion=None,
        )
        return Case(
            case_id=trigger.case_id,
            status=CaseStatus.NEW,
            case_revision=1,
            diagnosis_state=diagnosis_state,
            active_job_id=None,
            selected_skill_ref=None,
            final_result=None,
            failure=None,
            created_at=occurred_at,
            updated_at=occurred_at,
        )

    @staticmethod
    def _require_case(snapshot: StateFile, case_id: str) -> CaseAggregate:
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            _raise_port_error(
                ErrorCode.CASE_NOT_FOUND,
                "The requested Case does not exist.",
                [_detail(resource_type="case", resource_id=case_id)],
            )
        return aggregate

    @staticmethod
    def _require_expected_revision(case: Case, expected_revision: int) -> None:
        if case.case_revision != expected_revision:
            _raise_port_error(
                ErrorCode.REVISION_CONFLICT,
                "The expected Case revision is stale.",
                [
                    _detail(
                        field="expected_case_revision",
                        resource_type="case",
                        resource_id=case.case_id,
                        expected=expected_revision,
                        actual=case.case_revision,
                    )
                ],
            )

    @staticmethod
    def _require_nonterminal(case: Case) -> None:
        if case.status in _TERMINAL_CASE_STATUSES:
            _raise_port_error(
                ErrorCode.INVALID_CASE_STATE,
                "The command is not valid for a terminal Case.",
                [
                    _detail(
                        resource_type="case",
                        resource_id=case.case_id,
                        actual=case.status.value,
                    )
                ],
            )

    @staticmethod
    def _validate_supplement_inputs(
        case: Case,
        inputs: Mapping[str, str],
    ) -> None:
        open_inputs = {
            requirement.name: requirement
            for requirement in case.diagnosis_state.pending_requirements
            if requirement.status is RequirementStatus.OPEN
            and requirement.kind is RequirementKind.INPUT
        }
        for name, value in inputs.items():
            requirement = open_inputs.get(name)
            if requirement is None:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "A supplement input does not match an OPEN requirement.",
                    [_detail(field=f"inputs.{name}", expected="OPEN requirement")],
                )
            constraints = requirement.constraints
            if not isinstance(constraints, InputRequirementConstraints):
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "An INPUT requirement has invalid constraints.",
                )
            size = len(value.encode("utf-8"))
            if not constraints.min_utf8_bytes <= size <= constraints.max_utf8_bytes:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "A supplement input violates its byte constraints.",
                    [
                        _detail(
                            field=f"inputs.{name}",
                            limit=constraints.max_utf8_bytes,
                            observed=size,
                        )
                    ],
                )
            if constraints.pattern is not None and re.fullmatch(
                constraints.pattern,
                value,
            ) is None:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "A supplement input violates its required pattern.",
                    [_detail(field=f"inputs.{name}", expected="pattern match")],
                )
            if constraints.allowed_values and value not in constraints.allowed_values:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "A supplement input is not in its allowed value set.",
                    [_detail(field=f"inputs.{name}", expected="allowed value")],
                )

    @staticmethod
    def _validate_supplement_attachments(
        snapshot: StateFile,
        case: Case,
        attachment_ids: Sequence[str],
    ) -> None:
        open_attachment_requirements = [
            requirement
            for requirement in case.diagnosis_state.pending_requirements
            if requirement.status is RequirementStatus.OPEN
            and requirement.kind is RequirementKind.ATTACHMENT
        ]
        if not attachment_ids:
            return
        if len(open_attachment_requirements) != 1:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "Attachments require exactly one OPEN Attachment requirement.",
            )
        requirement = open_attachment_requirements[0]
        constraints = requirement.constraints
        if not isinstance(constraints, AttachmentRequirementConstraints):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "An ATTACHMENT requirement has invalid constraints.",
            )
        cumulative_refs = list(requirement.fulfilled_by_refs)
        cumulative_refs.extend(
            attachment_id
            for attachment_id in attachment_ids
            if attachment_id not in cumulative_refs
        )
        count = len(cumulative_refs)
        if not constraints.min_count <= count <= constraints.max_count:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The submitted Attachment count violates the requirement.",
                [
                    _detail(
                        field="attachment_ids",
                        expected=f"{constraints.min_count}..{constraints.max_count}",
                        actual=count,
                    )
                ],
            )
        aggregate = snapshot.cases[case.case_id]
        for attachment_id in attachment_ids:
            attachment = aggregate.attachments.get(attachment_id)
            if attachment is None:
                owner = next(
                    (
                        candidate.case.case_id
                        for candidate in snapshot.cases.values()
                        if attachment_id in candidate.attachments
                    ),
                    None,
                )
                if owner is not None:
                    _raise_port_error(
                        ErrorCode.RESOURCE_CASE_MISMATCH,
                        "The Attachment belongs to another Case.",
                        [
                            _detail(
                                resource_type="attachment",
                                resource_id=attachment_id,
                                expected=case.case_id,
                                actual=owner,
                            )
                        ],
                    )
                _raise_port_error(
                    ErrorCode.ATTACHMENT_NOT_FOUND,
                    "The requested Attachment does not exist.",
                    [
                        _detail(
                            resource_type="attachment",
                            resource_id=attachment_id,
                        )
                    ],
                )
            if attachment.status is not AttachmentStatus.READY:
                _raise_port_error(
                    ErrorCode.ATTACHMENT_NOT_READY,
                    "The Attachment is not READY.",
                    [
                        _detail(
                            resource_type="attachment",
                            resource_id=attachment_id,
                            expected=AttachmentStatus.READY.value,
                            actual=attachment.status.value,
                        )
                    ],
                )
            if (
                constraints.allowed_content_types
                and attachment.content_type not in constraints.allowed_content_types
            ):
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The Attachment content type is not allowed.",
                    [
                        _detail(
                            resource_type="attachment",
                            resource_id=attachment_id,
                            expected="allowed content type",
                            actual=attachment.content_type,
                        )
                    ],
                )

    @staticmethod
    def _validate_supplement_target(
        source_case: Case,
        target_case: Case,
        user_facts: Sequence[DiagnosisItem],
        attachment_ids: Sequence[str],
    ) -> None:
        fact_id_set = {item.item_id for item in user_facts}
        facts = {
            item.item_id for item in target_case.diagnosis_state.user_facts
        }
        if not fact_id_set <= facts:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The supplement plan omitted validated user facts.",
            )

        open_inputs = {
            requirement.name: requirement
            for requirement in source_case.diagnosis_state.pending_requirements
            if requirement.status is RequirementStatus.OPEN
            and requirement.kind is RequirementKind.INPUT
        }
        target_requirements = {
            requirement.requirement_id: requirement
            for requirement in target_case.diagnosis_state.pending_requirements
        }
        expected_requirement_by_fact: dict[str, str] = {}
        for fact in user_facts:
            source_requirement = open_inputs.get(fact.provenance.input_name)
            if source_requirement is None:
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "A supplied input did not originate from an OPEN input requirement.",
                )
            assert source_requirement is not None
            target_requirement = target_requirements.get(
                source_requirement.requirement_id
            )
            if (
                target_requirement is None
                or target_requirement.kind is not RequirementKind.INPUT
                or target_requirement.name != fact.provenance.input_name
                or target_requirement.status is not RequirementStatus.FULFILLED
                or target_requirement.fulfilled_by_refs != [fact.item_id]
            ):
                _raise_port_error(
                    ErrorCode.VALIDATION_ERROR,
                    "The supplement plan did not fulfill the matching input requirement.",
                )
            expected_requirement_by_fact[fact.item_id] = (
                source_requirement.requirement_id
            )

        if any(
            expected_requirement_by_fact.get(ref)
            not in {None, requirement.requirement_id}
            for requirement in target_case.diagnosis_state.pending_requirements
            for ref in requirement.fulfilled_by_refs
        ):
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "A supplied input fulfilled a different requirement.",
            )

        fulfilled = {
            ref
            for requirement in target_case.diagnosis_state.pending_requirements
            if requirement.status is RequirementStatus.FULFILLED
            for ref in requirement.fulfilled_by_refs
        }
        if not set(attachment_ids) <= fulfilled:
            _raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "The supplement plan omitted validated Attachments.",
            )

    @staticmethod
    def _retry_revision_conflict(
        error: ApplicationPortError,
        attempt: int,
    ) -> bool:
        return error.error.code is ErrorCode.REVISION_CONFLICT and attempt < 2

    def _after_commit(
        self,
        committed: _CommittedCommand,
        *,
        wait_seconds: int,
    ) -> ApplicationResponse:
        case_id = committed.receipt.case_id
        assert case_id is not None
        related_job = (
            committed.created_job or committed.submit_job or committed.cancel_job
        )
        record_journey_event(
            committed.event,
            timestamp=committed.occurred_at,
            request_id=committed.request_id,
            case_id=case_id,
            job_id=None if related_job is None else related_job.job_id,
            job_type=None if related_job is None else related_job.job_type,
            data={"generation": committed.generation, **committed.data},
        )
        previous = committed.previous_case
        current = committed.committed_case
        if previous is not None and previous.status is not current.status:
            record_journey_event(
                "case.status.changed",
                timestamp=committed.occurred_at,
                request_id=committed.request_id,
                case_id=case_id,
                job_id=None if related_job is None else related_job.job_id,
                job_type=None if related_job is None else related_job.job_type,
                data={
                    "source_event": committed.event,
                    "from_status": previous.status.value,
                    "to_status": current.status.value,
                    "from_case_revision": previous.case_revision,
                    "to_case_revision": current.case_revision,
                    "diagnosis_state_revision": current.diagnosis_state.revision,
                    "active_job_id": current.active_job_id,
                    "pending_requirements": current.diagnosis_state.pending_requirements,
                    "selected_skill_ref": current.selected_skill_ref,
                    "failure": current.failure,
                    "generation": committed.generation,
                },
            )
        if committed.created_job is not None:
            created = committed.created_job
            record_journey_event(
                "job.pending_persisted",
                timestamp=committed.occurred_at,
                request_id=committed.request_id,
                case_id=created.case_id,
                job_id=created.job_id,
                job_type=created.job_type,
                data={
                    "cause_event": committed.event,
                    "job": created,
                    "generation": committed.generation,
                },
            )
        try:
            self._notifier.notify(case_id, committed.generation)
        except Exception:
            # Notification is an optimization after the durable commit.
            pass

        dispatch_pending = False
        if committed.submit_job is not None:
            submit_job = committed.submit_job
            try:
                dispatch = self._dispatcher.submit(submit_job.job_id)
                dispatch_pending = not (dispatch.accepted or dispatch.duplicate)
                record_journey_event(
                    "job.queued" if dispatch.accepted else "job.queue.duplicate",
                    timestamp=committed.occurred_at,
                    request_id=committed.request_id,
                    case_id=submit_job.case_id,
                    job_id=submit_job.job_id,
                    job_type=submit_job.job_type,
                    data={
                        "accepted": dispatch.accepted,
                        "duplicate": dispatch.duplicate,
                    },
                )
            except Exception as exc:
                dispatch_pending = True
                record_journey_event(
                    "job.queue.failed",
                    level=logging.WARNING,
                    timestamp=committed.occurred_at,
                    request_id=committed.request_id,
                    case_id=submit_job.case_id,
                    job_id=submit_job.job_id,
                    job_type=submit_job.job_type,
                    data={"exception_type": type(exc).__name__},
                )
        if committed.cancel_job is not None:
            cancel_job = committed.cancel_job
            try:
                cancel = self._dispatcher.cancel(cancel_job.job_id)
                record_journey_event(
                    "job.cancel.signalled",
                    timestamp=committed.occurred_at,
                    request_id=committed.request_id,
                    case_id=cancel_job.case_id,
                    job_id=cancel_job.job_id,
                    job_type=cancel_job.job_type,
                    data={"signalled": cancel.signalled},
                )
            except Exception as exc:
                record_journey_event(
                    "job.cancel.signal_failed",
                    level=logging.WARNING,
                    timestamp=committed.occurred_at,
                    request_id=committed.request_id,
                    case_id=cancel_job.case_id,
                    job_id=cancel_job.job_id,
                    job_type=cancel_job.job_type,
                    data={"exception_type": type(exc).__name__},
                )
        try:
            state = self._repository.read_snapshot()
            return self._response_from_state(
                state,
                committed.receipt,
                wait_seconds=wait_seconds,
                dispatch_pending=dispatch_pending,
            )
        except ApplicationPortError as error:
            if error.error.code not in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                raise
            # The mutation and BusinessReceipt are already durable.  Preserve
            # that success even when the optional post-commit projection or
            # finite wait cannot refresh state; readiness reports the storage
            # fault independently.
            return self._saved_response_without_projection(
                committed.receipt,
                dispatch_pending=dispatch_pending,
            )

    def _respond(
        self,
        snapshot: StateFile,
        receipt: BusinessReceipt,
        wait_seconds: int,
    ) -> ApplicationResponse:
        dispatch_pending = False
        if receipt.job_id is not None and receipt.case_id is not None:
            aggregate = snapshot.cases.get(receipt.case_id)
            job = None if aggregate is None else aggregate.jobs.get(receipt.job_id)
            if receipt.operation == "CancelCase":
                try:
                    self._dispatcher.cancel(receipt.job_id)
                except Exception:
                    pass
            elif job is not None and job.status is JobStatus.PENDING:
                try:
                    dispatch = self._dispatcher.submit(receipt.job_id)
                    dispatch_pending = not (dispatch.accepted or dispatch.duplicate)
                except Exception:
                    dispatch_pending = True
        try:
            return self._response_from_state(
                snapshot,
                receipt,
                wait_seconds=wait_seconds,
                dispatch_pending=dispatch_pending,
            )
        except ApplicationPortError as error:
            if error.error.code not in {
                ErrorCode.STATE_CORRUPT,
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            }:
                raise
            # An idempotent replay is backed by the same durable receipt as a
            # just-committed command.  A dynamic wait reread must not replace
            # that receipt with a state-projection failure.
            return self._saved_response_without_projection(
                receipt,
                dispatch_pending=dispatch_pending,
            )

    @staticmethod
    def _saved_response_without_projection(
        receipt: BusinessReceipt,
        *,
        dispatch_pending: bool,
    ) -> ApplicationResponse:
        return ApplicationResponse(
            business_receipt=receipt,
            case_view=None,
            wait_timed_out=False,
            dispatch_pending=dispatch_pending,
        )

    def _response_from_state(
        self,
        snapshot: StateFile,
        receipt: BusinessReceipt,
        *,
        wait_seconds: int,
        dispatch_pending: bool,
    ) -> ApplicationResponse:
        case_id = receipt.case_id
        if case_id is None:
            return ApplicationResponse(
                business_receipt=receipt,
                case_view=None,
                wait_timed_out=False,
                dispatch_pending=dispatch_pending,
            )
        wait_timed_out = False
        if wait_seconds > 0 and receipt.job_id is not None:
            deadline = self._monotonic() + wait_seconds
            first_wait = True
            while self._response_wait_is_live(snapshot, case_id, receipt.job_id):
                remaining = (
                    float(wait_seconds)
                    if first_wait
                    else deadline - self._monotonic()
                )
                first_wait = False
                if remaining <= 0:
                    wait_timed_out = True
                    break
                try:
                    changed = self._notifier.wait_for_change(
                        case_id,
                        snapshot.generation,
                        remaining,
                    )
                except Exception:
                    changed = False
                snapshot = self._repository.read_snapshot()
                if not changed and self._response_wait_is_live(
                    snapshot,
                    case_id,
                    receipt.job_id,
                ):
                    wait_timed_out = True
                    break
        return ApplicationResponse(
            business_receipt=receipt,
            case_view=project_case_view(snapshot, case_id),
            wait_timed_out=wait_timed_out,
            dispatch_pending=dispatch_pending,
        )

    @staticmethod
    def _response_wait_is_live(
        snapshot: StateFile,
        case_id: str,
        job_id: str,
    ) -> bool:
        aggregate = snapshot.cases.get(case_id)
        if aggregate is None:
            return False
        if aggregate.case.status in {
            CaseStatus.WAITING_INPUT,
            CaseStatus.WAITING_ATTACHMENT,
            CaseStatus.RESOLVED,
            CaseStatus.FAILED,
            CaseStatus.CANCELLED,
            CaseStatus.INTERRUPTED,
        }:
            return False
        job = aggregate.jobs.get(job_id)
        return job is not None and job.status in {
            JobStatus.PENDING,
            JobStatus.RUNNING,
        }


__all__ = ["ExternalCommandHandler", "ExternalNonUploadCommand"]
