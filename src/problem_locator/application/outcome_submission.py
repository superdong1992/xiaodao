"""Deterministic finalized-Outcome processing for S03 JobControl."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    Artifact,
    ArtifactKind,
    AuditBundleMetadata,
    Case,
    CaseStatus,
    CaseView,
    ContinuationResourceView,
    DiagnosisOutcome,
    DiagnosisOutcomeTriggerPayload,
    ErrorCode,
    ExecutionFailure,
    ExecutionFailedTriggerPayload,
    ExecutionFileRef,
    Job,
    JobOutcome,
    JobStatus,
    JobType,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    OutcomeReceipt,
    OutcomeResultType,
    PlannedResourceTarget,
    PublishedJobReceipt,
    ResourceRef,
    ResourceKind,
    ResourceType,
    ReviewOutcomeTriggerPayload,
    RouteKind,
    RouteOutcomeTriggerPayload,
    RuntimeBindings,
    StagedResourceRef,
    StaleActiveOutcomeTriggerPayload,
    StateFile,
    SubmitJobOutcome,
    TransitionPlan,
    TriggerType,
    UserResultPayload,
    ValidatedTrigger,
    VersionedRef,
    canonical_json_bytes,
    finalize_unresolved_result,
)
from problem_locator.contracts.errors import deterministic_outcome_failure
from problem_locator.contracts.outcomes import (
    coordinator_outcome_error_failure,
    validate_coordinator_plan_result,
    validate_outcome_for_job,
    validate_transition_plan_for_outcome,
    validate_user_result_for_outcome,
    validate_user_result_resolution,
)
from problem_locator.contracts.ports import (
    AssetCatalogPort,
    Clock,
    ContextSnapshotProjector,
    Coordinator,
    Dispatcher,
    ExecutionRecordStore,
    IdGenerator,
    PublicationCommitGuard,
    ResourceStore,
    StateChangeNotifier,
    StateRepository,
)
from problem_locator.journey import record_journey_event

from .errors import raise_port_error
from .formalization import (
    apply_diagnosis_state_delta,
    build_job,
    formalize_accepted_artifacts,
    formalize_accepted_candidate,
    formalize_accepted_evidence,
    resolve_evidence_binding,
)
from .audit_bundle import AUDIT_BUNDLE_FORMAT_ID
from .audit_bundle_assembler import assemble_unresolved_audit_bundle
from .mutations import apply_transition_plan_to_case, build_state_mutation
from .job_control import _validate_control_plan
from .outcome_processing import (
    OutcomeActivity,
    classify_outcome_activity,
    make_outcome_processing_record,
    validate_published_job_recovery,
    validate_published_outcome,
)
from .preparation import runtime_bindings_from_job
from .projection import (
    continuation_for_outcome,
    project_case_components,
    project_case_view,
)
from .runtime_bindings import (
    rebuild_runtime_bindings_for_role,
    runtime_bindings_from_job_spec,
    runtime_bindings_match_role,
)


_MAX_COMMIT_ATTEMPTS = 3
_SERVER_AUDIT_PROPOSAL_KEY = "server-audit-bundle"


@dataclass(frozen=True, slots=True)
class _DeterministicRejection:
    code: ErrorCode
    details: tuple[ApplicationErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class _AppliedCommit:
    case_id: str
    generation: int
    occurred_at: str
    event: str
    source_job: Job
    outcome: JobOutcome
    previous_case: Case
    committed_case: Case
    created_job: Job | None
    case_view: CaseView
    data: dict[str, Any]


class OutcomeSubmissionService:
    """Process one finalized ``job_outcome.json`` through the frozen pipeline."""

    def __init__(
        self,
        repository: StateRepository,
        resource_store: ResourceStore,
        publication_guard: PublicationCommitGuard,
        execution_records: ExecutionRecordStore,
        coordinator: Coordinator,
        projector: ContextSnapshotProjector,
        asset_catalog: AssetCatalogPort,
        dispatcher: Dispatcher,
        notifier: StateChangeNotifier,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._repository = repository
        self._resource_store = resource_store
        self._publication_guard = publication_guard
        self._execution_records = execution_records
        self._coordinator = coordinator
        self._projector = projector
        self._asset_catalog = asset_catalog
        self._dispatcher = dispatcher
        self._notifier = notifier
        self._clock = clock
        self._ids = ids

    def submit_outcome(
        self,
        job_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
    ) -> OutcomeReceipt:
        # Rebuild nested contract objects as raw data first. Pydantic accepts
        # already-constructed nested models without re-running every nested
        # validator, including deliberately unsafe ``model_construct`` input.
        # The public Port boundary must reject that input before any clock,
        # ID, repository, execution-record, Catalog, Coordinator, or lease use.
        try:
            command = SubmitJobOutcome.model_validate(
                {
                    "job_outcome": _rebuild_contract_input(job_outcome),
                    "outcome_file_ref": _rebuild_contract_input(
                        outcome_file_ref
                    ),
                },
                strict=True,
            )
        except (TypeError, ValueError, ValidationError):
            raise_port_error(
                ErrorCode.VALIDATION_ERROR,
                "JobControlPort.submit_outcome received invalid raw input.",
            )
        processed_at = self._clock.now()
        outcome_trigger_id = self._ids.new("trigger")
        control_trigger_id = self._ids.new("trigger")
        last_conflict: ApplicationPortError | None = None
        for _ in range(_MAX_COMMIT_ATTEMPTS):
            try:
                return self._submit_once(
                    command,
                    processed_at=processed_at,
                    outcome_trigger_id=outcome_trigger_id,
                    control_trigger_id=control_trigger_id,
                )
            except ApplicationPortError as error:
                if error.error.code is not ErrorCode.REVISION_CONFLICT:
                    raise
                last_conflict = error
        assert last_conflict is not None
        raise last_conflict

    def _submit_once(
        self,
        command: SubmitJobOutcome,
        *,
        processed_at: str,
        outcome_trigger_id: str,
        control_trigger_id: str,
    ) -> OutcomeReceipt:
        snapshot = self._repository.read_snapshot()
        located = _find_job(snapshot, command.job_outcome.job_id)
        if located is None:
            raise_port_error(ErrorCode.JOB_NOT_FOUND, "The Job does not exist.")
        aggregate = snapshot.cases[located.case_id]

        replay = _find_processing_record(snapshot, command.job_outcome.outcome_id)
        if replay is not None:
            replay_case_id, record = replay
            if (
                record.job_id != command.job_outcome.job_id
                or record.outcome_hash != command.outcome_file_ref.sha256
            ):
                raise_port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The Outcome ID is already bound to different content.",
                )
            return OutcomeReceipt(
                disposition=OutcomeDisposition.DUPLICATE,
                case_view=project_case_view(snapshot, replay_case_id),
            )

        # The execution store is authoritative.  Its typed corruption failure
        # is itself auditable and must not escape as an unrecorded rejection.
        try:
            published = self._execution_records.read_published_outcome(
                command.job_outcome.job_id
            )
        except ApplicationPortError as error:
            if error.error.code is not ErrorCode.EXECUTION_RECORD_FAILED:
                raise
            return self._reject(
                snapshot,
                located,
                command.job_outcome,
                command.outcome_file_ref,
                trusted_outcome=None,
                rejection=_DeterministicRejection(
                    ErrorCode.EXECUTION_RECORD_FAILED
                ),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
            )

        validation = validate_published_outcome(
            command.job_outcome,
            command.outcome_file_ref,
            published,
        )
        if validation.error_code is not None:
            return self._reject(
                snapshot,
                located,
                command.job_outcome,
                command.outcome_file_ref,
                trusted_outcome=None,
                rejection=_DeterministicRejection(validation.error_code),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
            )
        assert validation.outcome is not None
        outcome = validation.outcome

        # Validate every invariant fixed by the source Job before consulting
        # mutable current Case state.  In particular, a REVIEW Outcome that
        # forges its Job.review_target is invalid even when a legitimate newer
        # Candidate would otherwise make the old Job stale.
        try:
            validate_outcome_for_job(located, outcome, aggregate)
            _validate_user_result_bytes(located, outcome)
        except (TypeError, ValueError):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=(
                    outcome
                    if outcome.case_id == aggregate.case.case_id
                    else None
                ),
                rejection=_DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
            )

        activity = classify_outcome_activity(aggregate, outcome)
        if activity.activity is OutcomeActivity.JOB_NOT_FOUND:
            raise_port_error(ErrorCode.JOB_NOT_FOUND, "The Job does not exist.")
        if activity.activity is OutcomeActivity.INVALID:
            trusted = outcome if outcome.case_id == aggregate.case.case_id else None
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=trusted,
                rejection=_DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
            )
        if activity.activity is OutcomeActivity.STALE:
            return self._record_stale(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
            )

        technical_rejection, missing_staged_proposal_keys = (
            self._validate_active_outcome(
                snapshot,
                located,
                outcome,
            )
        )
        if technical_rejection is not None:
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=technical_rejection,
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        prospective_job_id = self._ids.derive(
            "job",
            [
                snapshot.installation_id,
                located.case_id,
                outcome.outcome_id,
                "next_job",
            ],
        )
        try:
            recovered_job = self._execution_records.read_published_job(
                prospective_job_id
            )
        except ApplicationPortError as error:
            if error.error.code is not ErrorCode.EXECUTION_RECORD_FAILED:
                raise
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(
                    ErrorCode.EXECUTION_RECORD_FAILED
                ),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )
        if recovered_job is not None and not validate_published_job_recovery(
            recovered_job,
            job_id=prospective_job_id,
            case_id=located.case_id,
            created_at=outcome.produced_at,
        ):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(
                    ErrorCode.EXECUTION_RECORD_FAILED
                ),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        expected_next_job_type = _expected_next_job_type(located, outcome)
        recovered_bindings = (
            None
            if recovered_job is None
            else runtime_bindings_from_job(recovered_job.job)
        )
        if recovered_job is not None and (
            expected_next_job_type is None
            or recovered_job.job.job_type is not expected_next_job_type
            or not runtime_bindings_match_role(
                expected_next_job_type,
                recovered_bindings,
                expected_skill_ref=_expected_next_job_skill_ref(
                    located,
                    outcome,
                    expected_next_job_type,
                ),
            )
        ):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(
                    ErrorCode.EXECUTION_RECORD_FAILED
                ),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )
        if recovered_job is not None:
            assert expected_next_job_type is not None
            assert recovered_bindings is not None
            bindings = {expected_next_job_type: recovered_bindings}
        else:
            bindings = self._bindings_for_outcome(located, outcome)
        try:
            trigger = _outcome_trigger(
                snapshot,
                outcome,
                continuation_for_outcome(snapshot, outcome),
                bindings,
                outcome_trigger_id,
            )
            result = validate_coordinator_plan_result(
                trigger,
                self._coordinator.plan(
                    _case_snapshot(snapshot, located.case_id),
                    trigger,
                ),
            )
        except (TypeError, ValueError):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        if isinstance(result, ApplicationError):
            if result.code is ErrorCode.INVALID_CASE_STATE:
                return self._record_stale(
                    snapshot,
                    located,
                    outcome,
                    command.outcome_file_ref,
                    processed_at=processed_at,
                    control_trigger_id=control_trigger_id,
                )
            failure = coordinator_outcome_error_failure(trigger, result)
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(
                    failure.code,
                    tuple(failure.details),
                ),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        try:
            plan = validate_transition_plan_for_outcome(result, outcome)
            if plan.outcome_disposition is not OutcomeDisposition.APPLIED:
                raise ValueError("an active valid Outcome requires an APPLIED plan")
            _validate_applied_outcome_lifecycle(located, outcome, plan)
            _validate_next_job_bindings(plan, bindings)
        except (TypeError, ValueError):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        accepted_resource_keys = {
            *plan.accepted_evidence_proposal_keys,
            *plan.accepted_artifact_proposal_keys,
        }
        if not missing_staged_proposal_keys <= accepted_resource_keys:
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=_DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )

        applied = self._apply_plan(
            snapshot,
            located,
            outcome,
            command.outcome_file_ref,
            plan,
            prospective_job_id=prospective_job_id,
            recovered_job=recovered_job,
            missing_staged_proposal_keys=missing_staged_proposal_keys,
            processed_at=processed_at,
        )
        if isinstance(applied, _DeterministicRejection):
            return self._reject(
                snapshot,
                located,
                outcome,
                command.outcome_file_ref,
                trusted_outcome=outcome,
                rejection=applied,
                processed_at=processed_at,
                control_trigger_id=control_trigger_id,
                reclassify_stale=True,
            )
        self._discard_proposals(
            outcome,
            retained_keys={
                *plan.accepted_evidence_proposal_keys,
                *plan.accepted_artifact_proposal_keys,
            },
        )
        self._after_commit(applied)
        return OutcomeReceipt(
            disposition=OutcomeDisposition.APPLIED,
            case_view=applied.case_view,
        )

    def _validate_active_outcome(
        self,
        snapshot: StateFile,
        job: Job,
        outcome: JobOutcome,
    ) -> tuple[_DeterministicRejection | None, frozenset[str]]:
        missing_staged_proposal_keys: set[str] = set()
        proposals = [*outcome.proposed_evidence, *outcome.proposed_artifacts]
        for proposal in proposals:
            staged_ref = proposal.staged_resource_ref
            if staged_ref is None:
                continue
            try:
                self._resource_store.validate_staged(staged_ref)
            except ApplicationPortError as error:
                if error.error.code is ErrorCode.RESOURCE_NOT_FOUND:
                    missing_staged_proposal_keys.add(proposal.proposal_key)
                    continue
                if error.error.code is ErrorCode.RESOURCE_HASH_MISMATCH:
                    return (
                        _DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                        frozenset(),
                    )
                raise

        try:
            continuation_for_outcome(snapshot, outcome)
        except ApplicationPortError as error:
            if error.error.code in {
                ErrorCode.RESOURCE_NOT_FOUND,
                ErrorCode.RESOURCE_HASH_MISMATCH,
            }:
                return (
                    _DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                    frozenset(),
                )
            raise
        except (TypeError, ValueError):
            return (
                _DeterministicRejection(ErrorCode.OUTCOME_INVALID),
                frozenset(),
            )
        return None, frozenset(missing_staged_proposal_keys)

    def _bindings_for_outcome(
        self,
        job: Job,
        outcome: JobOutcome,
    ) -> dict[JobType, RuntimeBindings]:
        next_job_type = _expected_next_job_type(job, outcome)
        if next_job_type is None:
            return {}
        if next_job_type is JobType.ROUTE:
            return {
                JobType.ROUTE: _validate_catalog_bindings(
                    JobType.ROUTE,
                    self._asset_catalog.route_bindings(),
                )
            }
        payload = outcome.payload
        skill_ref = (
            getattr(payload, "skill_ref", None)
            if job.job_type is JobType.ROUTE
            else job.skill_ref
        )
        assert skill_ref is not None
        bindings = (
            self._asset_catalog.review_bindings(skill_ref)
            if next_job_type is JobType.REVIEW
            else self._asset_catalog.diagnose_bindings(skill_ref)
        )
        return {
            next_job_type: _validate_catalog_bindings(
                next_job_type,
                bindings,
                expected_skill_ref=skill_ref,
            )
        }

    def _apply_plan(
        self,
        snapshot: StateFile,
        job: Job,
        outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
        plan: TransitionPlan,
        *,
        prospective_job_id: str,
        recovered_job: PublishedJobReceipt | None,
        missing_staged_proposal_keys: frozenset[str],
        processed_at: str,
    ) -> _AppliedCommit | _DeterministicRejection:
        aggregate = snapshot.cases[job.case_id]
        evidence_by_key = {item.proposal_key: item for item in outcome.proposed_evidence}
        artifacts_by_key = {item.proposal_key: item for item in outcome.proposed_artifacts}
        evidence_ids = {
            key: self._ids.derive(
                "evidence",
                [snapshot.installation_id, job.case_id, outcome.outcome_id, key],
            )
            for key in plan.accepted_evidence_proposal_keys
        }
        artifact_ids = {
            key: self._ids.derive(
                "artifact",
                [snapshot.installation_id, job.case_id, outcome.outcome_id, key],
            )
            for key in plan.accepted_artifact_proposal_keys
        }
        generated_audit_artifact_id: str | None = None
        generated_audit_staged: StagedResourceRef | None = None
        generated_audit_target: PlannedResourceTarget | None = None
        unresolved_evidence_refs: list[str] = []
        if plan.unresolved_result_draft is not None:
            try:
                for binding in plan.unresolved_result_draft.evidence_bindings:
                    evidence_ref = resolve_evidence_binding(
                        binding,
                        existing_evidence_ids=aggregate.evidence,
                        evidence_ids_by_proposal_key=evidence_ids,
                    )
                    if evidence_ref not in unresolved_evidence_refs:
                        unresolved_evidence_refs.append(evidence_ref)
            except (KeyError, TypeError, ValueError, ValidationError):
                raise_port_error(
                    ErrorCode.OUTCOME_INVALID,
                    "The unresolved result cites Evidence outside its accepted closure.",
                )
            generated_audit_artifact_id = self._ids.derive(
                "artifact",
                [
                    snapshot.installation_id,
                    job.case_id,
                    outcome.outcome_id,
                    "audit-bundle",
                ],
            )
            generated_staging_id = self._ids.derive(
                "resource_staging",
                [
                    snapshot.installation_id,
                    job.case_id,
                    outcome.outcome_id,
                    "audit-bundle",
                ],
            )
            try:
                built_audit = assemble_unresolved_audit_bundle(
                    aggregate=aggregate,
                    source_job=job,
                    source_outcome=outcome,
                    unresolved=plan.unresolved_result_draft,
                    resolved_evidence_refs=unresolved_evidence_refs,
                    execution_records=self._execution_records,
                )
            except ApplicationPortError:
                raise_port_error(
                    ErrorCode.EXECUTION_RECORD_FAILED,
                    "The required V2 audit record could not be read.",
                )
            except (KeyError, OSError, TypeError, ValueError, ValidationError):
                raise_port_error(
                    ErrorCode.EXECUTION_RECORD_FAILED,
                    "The required V2 audit record is incomplete.",
                )
            try:
                generated_audit_staged = self._resource_store.stage_generated_file(
                    job.job_id,
                    _SERVER_AUDIT_PROPOSAL_KEY,
                    generated_staging_id,
                    io.BytesIO(built_audit.payload),
                    expected_size=len(built_audit.payload),
                    expected_sha256=built_audit.sha256,
                )
                generated_audit_target = self._resource_store.plan_target(
                    job.case_id,
                    ResourceType.ARTIFACT,
                    generated_audit_artifact_id,
                    ResourceKind.FILE,
                    generated_audit_staged.size,
                    generated_audit_staged.sha256,
                )
            except ApplicationPortError as error:
                if error.error.code in {
                    ErrorCode.RESOURCE_STAGE_FAILED,
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    ErrorCode.RESOURCE_SIZE_MISMATCH,
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    ErrorCode.PATH_VIOLATION,
                    ErrorCode.VALIDATION_ERROR,
                }:
                    raise_port_error(
                        ErrorCode.RESOURCE_PUBLISH_FAILED,
                        "The server audit bundle could not be staged safely.",
                    )
                raise
            except (OSError, TypeError, ValueError, ValidationError):
                raise_port_error(
                    ErrorCode.RESOURCE_PUBLISH_FAILED,
                    "The server audit bundle could not be staged safely.",
                )
        candidate_ids: dict[str, str] = {}
        draft = (
            outcome.payload.candidate_conclusion_draft
            if isinstance(outcome.payload, DiagnosisOutcome)
            else None
        )
        if (
            plan.accepted_candidate_proposal_key is not None
            and draft is not None
            and draft.existing_conclusion_id is None
        ):
            key = plan.accepted_candidate_proposal_key
            candidate_ids[key] = self._ids.derive(
                "candidate_conclusion",
                [snapshot.installation_id, job.case_id, outcome.outcome_id, key],
            )

        target_rows: list[
            tuple[str, StagedResourceRef, PlannedResourceTarget]
        ] = []
        if generated_audit_staged is not None and generated_audit_target is not None:
            target_rows.append(
                (
                    _SERVER_AUDIT_PROPOSAL_KEY,
                    generated_audit_staged,
                    generated_audit_target,
                )
            )
        try:
            for key in plan.accepted_evidence_proposal_keys:
                proposal = evidence_by_key[key]
                staged = proposal.staged_resource_ref
                if staged is None:
                    continue
                target = self._resource_store.plan_target(
                    job.case_id,
                    ResourceType.EVIDENCE,
                    evidence_ids[key],
                    staged.resource_kind,
                    staged.size,
                    staged.sha256,
                )
                target_rows.append((key, staged, target))
            for key in plan.accepted_artifact_proposal_keys:
                proposal = artifacts_by_key[key]
                staged = proposal.staged_resource_ref
                target = self._resource_store.plan_target(
                    job.case_id,
                    ResourceType.ARTIFACT,
                    artifact_ids[key],
                    staged.resource_kind,
                    staged.size,
                    staged.sha256,
                )
                target_rows.append((key, staged, target))
        except (KeyError, TypeError, ValueError):
            return _DeterministicRejection(ErrorCode.OUTCOME_INVALID)
        except ApplicationPortError as error:
            if error.error.code is ErrorCode.VALIDATION_ERROR:
                return _DeterministicRejection(ErrorCode.OUTCOME_INVALID)
            raise

        target_rows.sort(key=lambda row: row[2].final_storage_key)
        planned_targets = {key: target for key, _, target in target_rows}
        lease = self._publication_guard.acquire()
        rejection: _DeterministicRejection | None = None
        committed: _AppliedCommit | None = None
        formal_artifacts = {}
        formal_generated_artifacts: dict[str, Artifact] = {}
        formal_evidence = {}
        created_job: Job | None = None
        target_case_view: CaseView | None = None
        try:
            published_resources: dict[str, ResourceRef] = {}
            if target_rows:
                try:
                    self._resource_store.validate_case_capacity(
                        job.case_id,
                        [row[2] for row in target_rows],
                    )
                except ApplicationPortError as error:
                    if error.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED:
                        rejection = _DeterministicRejection(
                            ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                            tuple(error.error.details),
                        )
                    elif error.error.code in {
                        ErrorCode.RESOURCE_HASH_MISMATCH,
                    }:
                        rejection = _DeterministicRejection(
                            ErrorCode.RESOURCE_HASH_MISMATCH
                        )
                    elif error.error.code is ErrorCode.PATH_VIOLATION:
                        raise_port_error(
                            ErrorCode.RESOURCE_PUBLISH_FAILED,
                            "The accepted Outcome targets could not be validated.",
                        )
                    else:
                        raise
            if rejection is None:
                publication_rows = sorted(
                    target_rows,
                    key=lambda row: (
                        row[0] not in missing_staged_proposal_keys,
                        row[2].final_storage_key,
                    ),
                )
                for key, staged, target in publication_rows:
                    try:
                        published = self._resource_store.publish(
                            staged,
                            target.final_storage_key,
                        )
                        if published.storage_key != target.final_storage_key:
                            rejection = _DeterministicRejection(
                                ErrorCode.OUTCOME_INVALID
                            )
                            break
                        published_resources[key] = published
                    except ApplicationPortError as error:
                        if error.error.code is ErrorCode.RESOURCE_HASH_MISMATCH:
                            rejection = _DeterministicRejection(
                                ErrorCode.RESOURCE_HASH_MISMATCH
                            )
                        elif (
                            error.error.code is ErrorCode.RESOURCE_NOT_FOUND
                            and key in missing_staged_proposal_keys
                        ):
                            rejection = _DeterministicRejection(
                                ErrorCode.OUTCOME_INVALID
                            )
                        elif error.error.code in {
                            ErrorCode.RESOURCE_NOT_FOUND,
                            ErrorCode.PATH_VIOLATION,
                        }:
                            raise_port_error(
                                ErrorCode.RESOURCE_PUBLISH_FAILED,
                                "The accepted Outcome resource could not be published.",
                            )
                        elif error.error.code is ErrorCode.RESOURCE_PUBLISH_FAILED:
                            raise
                        else:
                            raise
                        break

            if rejection is None:
                try:
                    formal_artifacts = formalize_accepted_artifacts(
                        outcome.proposed_artifacts,
                        plan.accepted_artifact_proposal_keys,
                        case_id=job.case_id,
                        created_by_job_id=job.job_id,
                        artifact_ids_by_proposal_key=artifact_ids,
                        planned_targets_by_proposal_key=planned_targets,
                        published_resources_by_proposal_key=published_resources,
                        occurred_at=outcome.produced_at,
                    )
                    existing_sources = {
                        *aggregate.attachments,
                        *aggregate.artifacts,
                        *aggregate.outcomes,
                        *(item.item_id for item in aggregate.case.diagnosis_state.user_facts),
                    }
                    formal_evidence = formalize_accepted_evidence(
                        outcome.proposed_evidence,
                        plan.accepted_evidence_proposal_keys,
                        case_id=job.case_id,
                        evidence_ids_by_proposal_key=evidence_ids,
                        existing_source_refs=existing_sources,
                        artifacts_by_proposal_key=formal_artifacts,
                        planned_targets_by_proposal_key=planned_targets,
                        published_resources_by_proposal_key=published_resources,
                        occurred_at=outcome.produced_at,
                    )
                    if generated_audit_artifact_id is not None:
                        target = planned_targets.get(_SERVER_AUDIT_PROPOSAL_KEY)
                        published = published_resources.get(
                            _SERVER_AUDIT_PROPOSAL_KEY
                        )
                        if (
                            target is None
                            or published is None
                            or published.storage_key != target.final_storage_key
                            or published.resource_kind is not ResourceKind.FILE
                            or published.size != target.size
                            or published.sha256 != target.sha256
                        ):
                            raise ValueError(
                                "server-generated audit resource did not match its target"
                            )
                        formal_generated_artifacts[
                            generated_audit_artifact_id
                        ] = Artifact(
                            artifact_id=generated_audit_artifact_id,
                            case_id=job.case_id,
                            kind=ArtifactKind.AUDIT_BUNDLE,
                            name="problem-locator-audit-bundle.zip",
                            content_type="application/zip",
                            resource_kind=ResourceKind.FILE,
                            size=published.size,
                            sha256=published.sha256,
                            storage_key=published.storage_key,
                            metadata=AuditBundleMetadata(
                                schema_version=1,
                                format_id=AUDIT_BUNDLE_FORMAT_ID,
                                description=(
                                    "Server-generated observable diagnosis and "
                                    "review audit bundle."
                                ),
                                case_id=job.case_id,
                                source_job_id=job.job_id,
                                source_outcome_id=outcome.outcome_id,
                            ),
                            created_by_job_id=job.job_id,
                            created_at=processed_at,
                        )
                    formal_candidate = formalize_accepted_candidate(
                        draft,
                        plan.accepted_candidate_proposal_key,
                        current_candidate=aggregate.case.diagnosis_state.candidate_conclusion,
                        problem_completion_criteria=(
                            aggregate.case.diagnosis_state.problem_spec.completion_criteria
                        ),
                        existing_evidence_ids=aggregate.evidence,
                        evidence_ids_by_proposal_key=evidence_ids,
                        candidate_ids_by_proposal_key=candidate_ids,
                        proposed_by_job_id=job.job_id,
                    )
                    candidates = (
                        {}
                        if formal_candidate is None
                        else {plan.accepted_candidate_proposal_key: formal_candidate}
                    )
                    target_state = apply_diagnosis_state_delta(
                        aggregate.case.diagnosis_state,
                        plan.accepted_state_delta,
                        evidence_ids_by_proposal_key=evidence_ids,
                        candidate_mutation=plan.candidate_mutation,
                        candidates_by_proposal_key=candidates,
                        expected_target_revision=(
                            None
                            if plan.next_job_spec is None
                            else plan.next_job_spec.target_state_revision
                        ),
                    )
                    if formal_candidate is not None:
                        result = _expected_user_result(job, outcome)
                        if result is None:
                            raise ValueError(
                                "formal Candidate has no USER_RESULT payload"
                            )
                        validate_user_result_resolution(
                            result,
                            formal_candidate,
                            evidence_ids,
                        )
                    created_job = (
                        None
                        if plan.next_job_spec is None
                        else build_job(
                            plan.next_job_spec,
                            job_id=prospective_job_id,
                            case_id=job.case_id,
                            created_at=outcome.produced_at,
                            target_diagnosis_state=target_state,
                            projector=self._projector,
                            existing_evidence_ids=aggregate.evidence,
                            evidence_ids_by_proposal_key=evidence_ids,
                            existing_artifact_ids=aggregate.artifacts,
                            artifact_ids_by_proposal_key=artifact_ids,
                            existing_candidate=(
                                aggregate.case.diagnosis_state.candidate_conclusion
                            ),
                            candidates_by_proposal_key=candidates,
                        )
                    )
                    unresolved_result = (
                        None
                        if plan.unresolved_result_draft is None
                        else finalize_unresolved_result(
                            plan.unresolved_result_draft,
                            generated_audit_artifact_id,
                            unresolved_evidence_refs,
                        )
                    )
                    new_case = apply_transition_plan_to_case(
                        aggregate.case,
                        plan,
                        target_state,
                        created_job=created_job,
                        processed_at=processed_at,
                        unresolved_result=unresolved_result,
                    )
                    active_job = (
                        None
                        if new_case.active_job_id is None
                        else created_job
                    )
                    if (
                        new_case.active_job_id is not None
                        and (
                            active_job is None
                            or active_job.job_id != new_case.active_job_id
                        )
                    ):
                        raise ValueError(
                            "Outcome target active Job was not created by its plan"
                        )
                    target_case_view = project_case_components(
                        new_case,
                        active_job,
                        [
                            *aggregate.artifacts.values(),
                            *formal_artifacts.values(),
                            *formal_generated_artifacts.values(),
                        ],
                    )
                except (KeyError, TypeError, ValueError):
                    rejection = _DeterministicRejection(ErrorCode.OUTCOME_INVALID)

            if rejection is None:
                if created_job is None:
                    if recovered_job is not None:
                        rejection = _DeterministicRejection(
                            ErrorCode.EXECUTION_RECORD_FAILED
                        )
                elif recovered_job is not None:
                    if canonical_json_bytes(recovered_job.job) != canonical_json_bytes(
                        created_job
                    ):
                        rejection = _DeterministicRejection(
                            ErrorCode.EXECUTION_RECORD_FAILED
                        )
                else:
                    try:
                        self._execution_records.publish_job(created_job)
                    except ApplicationPortError as error:
                        if error.error.code is ErrorCode.IDEMPOTENCY_CONFLICT:
                            rejection = _DeterministicRejection(
                                ErrorCode.EXECUTION_RECORD_FAILED
                            )
                        elif error.error.code is ErrorCode.EXECUTION_RECORD_FAILED:
                            # A finalized Outcome is already the durable outbox.
                            # Failure of its first next-job publication has no
                            # disposition: the caller retries only this exact
                            # submission receipt and never Runtime.execute.
                            raise
                        else:
                            raise

            if rejection is None:
                assert target_case_view is not None
                processing = make_outcome_processing_record(
                    outcome,
                    outcome_file_ref,
                    disposition=OutcomeDisposition.APPLIED,
                    processed_at=processed_at,
                    error_code=None,
                    accepted_evidence_ids=sorted(
                        item.evidence_id for item in formal_evidence.values()
                    ),
                    accepted_artifact_ids=sorted(
                        item.artifact_id for item in formal_artifacts.values()
                    ),
                    generated_artifact_ids=sorted(formal_generated_artifacts),
                    created_job_id=None if created_job is None else created_job.job_id,
                    reason=plan.reason,
                )
                receipt = self._repository.commit(
                    snapshot.generation,
                    aggregate.case.case_revision,
                    build_state_mutation(
                        upsert_case=new_case,
                        insert_jobs=[] if created_job is None else [created_job],
                        job_lifecycle_updates=plan.job_updates,
                        insert_outcomes=[outcome],
                        insert_outcome_processing_records=[processing],
                        insert_evidence=formal_evidence.values(),
                        insert_artifacts=[
                            *formal_artifacts.values(),
                            *formal_generated_artifacts.values(),
                        ],
                    ),
                )
                committed = _AppliedCommit(
                    case_id=job.case_id,
                    generation=receipt.generation,
                    occurred_at=processed_at,
                    event="job.outcome.applied",
                    source_job=job,
                    outcome=outcome,
                    previous_case=aggregate.case,
                    committed_case=new_case,
                    created_job=created_job,
                    case_view=target_case_view,
                    data={
                        "disposition": OutcomeDisposition.APPLIED.value,
                        "outcome": outcome,
                        "outcome_file_ref": outcome_file_ref,
                        "processing": processing,
                        "plan_reason": plan.reason,
                        "accepted_evidence": list(formal_evidence.values()),
                        "accepted_artifacts": list(formal_artifacts.values()),
                        "generated_artifacts": list(
                            formal_generated_artifacts.values()
                        ),
                        "created_job": created_job,
                        "case_view": target_case_view,
                    },
                )
        finally:
            lease.release()
        if rejection is not None:
            return rejection
        assert committed is not None
        return committed

    def _reject(
        self,
        snapshot: StateFile,
        job: Job,
        claimed_outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
        *,
        trusted_outcome: JobOutcome | None,
        rejection: _DeterministicRejection,
        processed_at: str,
        control_trigger_id: str,
        reclassify_stale: bool = False,
    ) -> OutcomeReceipt:
        # A deterministic rejection may have been discovered while holding a
        # publication lease.  Release that lease in the caller, then restart
        # the rejection decision from an authoritative snapshot so an Outcome
        # that became stale is audited as STALE instead of failing an unrelated
        # live Job from the old generation.
        snapshot = self._repository.read_snapshot()
        current_job = _find_job(snapshot, job.job_id)
        if current_job is None:
            raise_port_error(ErrorCode.JOB_NOT_FOUND, "The Job does not exist.")
        job = current_job

        replay = _find_processing_record(snapshot, claimed_outcome.outcome_id)
        if replay is not None:
            replay_case_id, record = replay
            if (
                record.job_id != claimed_outcome.job_id
                or record.outcome_hash != outcome_file_ref.sha256
            ):
                raise_port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The Outcome ID is already bound to different content.",
                )
            if trusted_outcome is not None:
                self._discard_proposals(trusted_outcome)
            return OutcomeReceipt(
                disposition=OutcomeDisposition.DUPLICATE,
                case_view=project_case_view(snapshot, replay_case_id),
            )

        aggregate = snapshot.cases[job.case_id]
        if reclassify_stale and trusted_outcome is not None:
            activity = classify_outcome_activity(aggregate, trusted_outcome)
            if activity.activity is OutcomeActivity.JOB_NOT_FOUND:
                raise_port_error(ErrorCode.JOB_NOT_FOUND, "The Job does not exist.")
            if activity.activity is OutcomeActivity.STALE:
                return self._record_stale(
                    snapshot,
                    job,
                    trusted_outcome,
                    outcome_file_ref,
                    processed_at=processed_at,
                    control_trigger_id=control_trigger_id,
                )
        active = (
            job.status is JobStatus.RUNNING
            and aggregate.case.active_job_id == job.job_id
        )
        plan: TransitionPlan | None = None
        new_case: Case
        if active:
            trigger = _failure_trigger(
                snapshot,
                job,
                claimed_outcome.outcome_id,
                deterministic_outcome_failure(rejection.code, rejection.details),
                processed_at,
                control_trigger_id,
            )
            try:
                result = validate_coordinator_plan_result(
                    trigger,
                    self._coordinator.plan(
                        _case_snapshot(snapshot, job.case_id),
                        trigger,
                    ),
                )
            except (TypeError, ValueError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The active Outcome rejection decision is invalid.",
                )
            if isinstance(result, ApplicationError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The active Outcome rejection could not be materialized.",
                )
            plan = result
            try:
                new_case = _validate_control_plan(
                    aggregate,
                    plan,
                    source_job=job,
                    target_job_statuses=[
                        JobStatus.FAILED,
                        JobStatus.INTERRUPTED,
                    ],
                    occurred_at=processed_at,
                )
            except (TypeError, ValueError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The active Outcome rejection plan is invalid.",
                )
        else:
            new_case = _audit_case(aggregate.case, processed_at)

        processing = _processing_record(
            claimed_outcome,
            outcome_file_ref,
            disposition=OutcomeDisposition.REJECTED,
            processed_at=processed_at,
            error_code=rejection.code,
            reason=f"Technical Outcome rejection: {rejection.code.value}.",
        )
        active_job = (
            None
            if new_case.active_job_id is None
            else aggregate.jobs[new_case.active_job_id]
        )
        target_case_view = project_case_components(
            new_case,
            active_job,
            aggregate.artifacts.values(),
        )
        lease = self._publication_guard.acquire()
        try:
            receipt = self._repository.commit(
                snapshot.generation,
                aggregate.case.case_revision,
                build_state_mutation(
                    upsert_case=new_case,
                    job_lifecycle_updates=[] if plan is None else plan.job_updates,
                    insert_outcomes=[] if trusted_outcome is None else [trusted_outcome],
                    insert_outcome_processing_records=[processing],
                ),
            )
        finally:
            lease.release()
        if trusted_outcome is not None:
            self._discard_proposals(trusted_outcome)
        applied = _AppliedCommit(
            case_id=job.case_id,
            generation=receipt.generation,
            occurred_at=processed_at,
            event="job.outcome.rejected",
            source_job=job,
            outcome=claimed_outcome,
            previous_case=aggregate.case,
            committed_case=new_case,
            created_job=None,
            case_view=target_case_view,
            data={
                "disposition": OutcomeDisposition.REJECTED.value,
                "rejection_code": rejection.code.value,
                "trusted_outcome": trusted_outcome,
                "outcome_file_ref": outcome_file_ref,
                "processing": processing,
                "case_view": target_case_view,
            },
        )
        self._after_commit(applied)
        return OutcomeReceipt(
            disposition=OutcomeDisposition.REJECTED,
            case_view=target_case_view,
        )

    def _record_stale(
        self,
        snapshot: StateFile,
        job: Job,
        outcome: JobOutcome,
        outcome_file_ref: ExecutionFileRef,
        *,
        processed_at: str,
        control_trigger_id: str,
    ) -> OutcomeReceipt:
        aggregate = snapshot.cases[job.case_id]
        plan: TransitionPlan | None = None
        if (
            job.status is JobStatus.RUNNING
            and aggregate.case.active_job_id == job.job_id
            and aggregate.case.diagnosis_state.revision != outcome.base_state_revision
        ):
            trigger = _stale_trigger(
                snapshot,
                job,
                outcome,
                processed_at,
                control_trigger_id,
            )
            try:
                result = validate_coordinator_plan_result(
                    trigger,
                    self._coordinator.plan(
                        _case_snapshot(snapshot, job.case_id),
                        trigger,
                    ),
                )
            except (TypeError, ValueError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The stale active Outcome decision is invalid.",
                )
            if isinstance(result, ApplicationError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The stale active Outcome could not be materialized.",
                )
            plan = result
            try:
                new_case = _validate_control_plan(
                    aggregate,
                    plan,
                    source_job=job,
                    target_job_statuses=[JobStatus.INTERRUPTED],
                    occurred_at=processed_at,
                )
            except (TypeError, ValueError):
                raise_port_error(
                    ErrorCode.STATE_WRITE_FAILED,
                    "The stale active Outcome plan is invalid.",
                )
        else:
            new_case = _audit_case(aggregate.case, processed_at)
        processing = make_outcome_processing_record(
            outcome,
            outcome_file_ref,
            disposition=OutcomeDisposition.STALE,
            processed_at=processed_at,
            error_code=None,
            accepted_evidence_ids=[],
            accepted_artifact_ids=[],
            generated_artifact_ids=[],
            created_job_id=None,
            reason="The finalized Outcome is stale.",
        )
        active_job = (
            None
            if new_case.active_job_id is None
            else aggregate.jobs[new_case.active_job_id]
        )
        target_case_view = project_case_components(
            new_case,
            active_job,
            aggregate.artifacts.values(),
        )
        lease = self._publication_guard.acquire()
        try:
            receipt = self._repository.commit(
                snapshot.generation,
                aggregate.case.case_revision,
                build_state_mutation(
                    upsert_case=new_case,
                    job_lifecycle_updates=[] if plan is None else plan.job_updates,
                    insert_outcomes=[outcome],
                    insert_outcome_processing_records=[processing],
                ),
            )
        finally:
            lease.release()
        self._discard_proposals(outcome)
        applied = _AppliedCommit(
            case_id=job.case_id,
            generation=receipt.generation,
            occurred_at=processed_at,
            event="job.outcome.stale",
            source_job=job,
            outcome=outcome,
            previous_case=aggregate.case,
            committed_case=new_case,
            created_job=None,
            case_view=target_case_view,
            data={
                "disposition": OutcomeDisposition.STALE.value,
                "outcome": outcome,
                "outcome_file_ref": outcome_file_ref,
                "processing": processing,
                "case_view": target_case_view,
            },
        )
        self._after_commit(applied)
        return OutcomeReceipt(
            disposition=OutcomeDisposition.STALE,
            case_view=target_case_view,
        )

    def _after_commit(self, committed: _AppliedCommit) -> None:
        job = committed.source_job
        outcome = committed.outcome
        record_journey_event(
            committed.event,
            timestamp=committed.occurred_at,
            case_id=committed.case_id,
            job_id=job.job_id,
            job_type=job.job_type,
            outcome_id=outcome.outcome_id,
            data={"generation": committed.generation, **committed.data},
        )
        if committed.previous_case.status is not committed.committed_case.status:
            current = committed.committed_case
            record_journey_event(
                "case.status.changed",
                timestamp=committed.occurred_at,
                case_id=committed.case_id,
                job_id=job.job_id,
                job_type=job.job_type,
                outcome_id=outcome.outcome_id,
                data={
                    "source_event": committed.event,
                    "from_status": committed.previous_case.status.value,
                    "to_status": current.status.value,
                    "from_case_revision": committed.previous_case.case_revision,
                    "to_case_revision": current.case_revision,
                    "diagnosis_state_revision": current.diagnosis_state.revision,
                    "active_job_id": current.active_job_id,
                    "pending_requirements": current.diagnosis_state.pending_requirements,
                    "selected_skill_ref": current.selected_skill_ref,
                    "final_result": committed.case_view.final_result,
                    "failure": current.failure,
                    "generation": committed.generation,
                },
            )
        if committed.created_job is not None:
            created = committed.created_job
            record_journey_event(
                "job.pending_persisted",
                timestamp=committed.occurred_at,
                case_id=created.case_id,
                job_id=created.job_id,
                job_type=created.job_type,
                outcome_id=outcome.outcome_id,
                data={
                    "cause_event": committed.event,
                    "parent_job_id": job.job_id,
                    "parent_outcome_id": outcome.outcome_id,
                    "job": created,
                    "generation": committed.generation,
                },
            )
        try:
            self._notifier.notify(committed.case_id, committed.generation)
        except Exception:
            # Notifications are hints and never roll back the durable result.
            pass
        if committed.created_job is not None:
            created = committed.created_job
            try:
                dispatch = self._dispatcher.submit(created.job_id)
                record_journey_event(
                    "job.queued" if dispatch.accepted else "job.queue.duplicate",
                    timestamp=committed.occurred_at,
                    case_id=created.case_id,
                    job_id=created.job_id,
                    job_type=created.job_type,
                    outcome_id=outcome.outcome_id,
                    data={
                        "accepted": dispatch.accepted,
                        "duplicate": dispatch.duplicate,
                    },
                )
            except Exception as exc:
                # Dispatch is an idempotent post-commit signal.  S05 and a
                # duplicate delivery will re-submit the durable PENDING Job.
                record_journey_event(
                    "job.queue.failed",
                    level=logging.WARNING,
                    timestamp=committed.occurred_at,
                    case_id=created.case_id,
                    job_id=created.job_id,
                    job_type=created.job_type,
                    outcome_id=outcome.outcome_id,
                    data={"exception_type": type(exc).__name__},
                )

    def _discard_proposals(
        self,
        outcome: JobOutcome,
        *,
        retained_keys: set[str] | None = None,
    ) -> None:
        retained = retained_keys or set()
        staged = [
            proposal.staged_resource_ref
            for proposal in outcome.proposed_evidence
            if proposal.staged_resource_ref is not None
            and proposal.proposal_key not in retained
        ]
        staged.extend(
            proposal.staged_resource_ref
            for proposal in outcome.proposed_artifacts
            if proposal.proposal_key not in retained
        )
        for staged_ref in staged:
            try:
                self._resource_store.discard(staged_ref)
            except Exception:
                # Processing is already durable; cleanup is idempotent and may
                # be retried by duplicate delivery or S02 orphan collection.
                pass


def _find_job(snapshot: StateFile, job_id: str) -> Job | None:
    for aggregate in snapshot.cases.values():
        job = aggregate.jobs.get(job_id)
        if job is not None:
            return job
    return None


def _rebuild_contract_input(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", warnings=False)
    return value


def _find_processing_record(snapshot: StateFile, outcome_id: str):
    for case_id, aggregate in snapshot.cases.items():
        record = aggregate.outcome_processing_records.get(outcome_id)
        if record is not None:
            return case_id, record
    return None


def _expected_next_job_type(
    job: Job,
    outcome: JobOutcome,
) -> JobType | None:
    """Return the sole role for which this Outcome may need pinned bindings."""

    payload = outcome.payload
    if job.job_type is JobType.ROUTE:
        if (
            outcome.result_type is OutcomeResultType.COMPLETED
            and getattr(payload, "kind", None) is RouteKind.MATCHED
            and getattr(payload, "skill_ref", None) is not None
        ):
            return JobType.DIAGNOSE
        return None
    if job.job_type is JobType.DIAGNOSE:
        if outcome.result_type is OutcomeResultType.REROUTE:
            return JobType.ROUTE
        if (
            outcome.result_type is OutcomeResultType.COMPLETED
            and isinstance(payload, DiagnosisOutcome)
            and job.skill_ref is not None
        ):
            return (
                JobType.DIAGNOSE
                if payload.candidate_conclusion_draft is None
                else JobType.REVIEW
            )
        return None
    # REVIEW never starts another diagnosis automatically in V2.  PASS
    # resolves; semantic rejection and invalid NEED_MORE terminate unresolved;
    # an eligible MISSING_ONLY request waits without a Job.
    return None


def _expected_next_job_skill_ref(
    job: Job,
    outcome: JobOutcome,
    next_job_type: JobType,
) -> VersionedRef | None:
    if next_job_type is JobType.ROUTE:
        return None
    if job.job_type is JobType.ROUTE:
        return getattr(outcome.payload, "skill_ref", None)
    return job.skill_ref


def _validate_catalog_bindings(
    job_type: JobType,
    bindings: RuntimeBindings,
    *,
    expected_skill_ref: VersionedRef | None = None,
) -> RuntimeBindings:
    """Reject a malformed Catalog success without substituting asset versions."""

    try:
        return rebuild_runtime_bindings_for_role(
            job_type,
            bindings,
            expected_skill_ref=expected_skill_ref,
        )
    except (TypeError, ValueError, ValidationError):
        raise_port_error(
            ErrorCode.CONFIG_INVALID,
            "The pinned runtime bindings are invalid for their requested role.",
        )


def _validate_next_job_bindings(
    plan: TransitionPlan,
    offered: dict[JobType, RuntimeBindings],
) -> None:
    """Require Coordinator to copy the one offered pinned binding verbatim."""

    spec = plan.next_job_spec
    if spec is None:
        return
    expected = offered.get(spec.job_type)
    if expected is None or runtime_bindings_from_job_spec(spec) != expected:
        raise ValueError(
            "next Job runtime bindings must exactly match the Trigger binding"
        )


def _case_snapshot(snapshot: StateFile, case_id: str):
    from .projection import build_case_snapshot

    return build_case_snapshot(snapshot, case_id)


def _outcome_trigger(
    snapshot: StateFile,
    outcome: JobOutcome,
    continuation: ContinuationResourceView,
    bindings: dict[JobType, RuntimeBindings],
    trigger_id: str,
) -> ValidatedTrigger:
    if outcome.job_type is JobType.ROUTE:
        trigger_type = TriggerType.ROUTE_OUTCOME
        payload = RouteOutcomeTriggerPayload(job_outcome=outcome)
    elif outcome.job_type is JobType.DIAGNOSE:
        trigger_type = TriggerType.DIAGNOSIS_OUTCOME
        payload = DiagnosisOutcomeTriggerPayload(job_outcome=outcome)
    elif outcome.job_type is JobType.REVIEW:
        trigger_type = TriggerType.REVIEW_OUTCOME
        payload = ReviewOutcomeTriggerPayload(job_outcome=outcome)
    else:
        raise ValueError("unsupported Outcome job type")
    return ValidatedTrigger(
        trigger_id=trigger_id,
        trigger_type=trigger_type,
        case_id=outcome.case_id,
        expected_case_revision=snapshot.cases[outcome.case_id].case.case_revision,
        idempotency_key=outcome.outcome_id,
        payload=payload,
        continuation_resources=continuation,
        runtime_bindings_by_job_type=bindings,
        occurred_at=outcome.produced_at,
    )


def _failure_trigger(
    snapshot: StateFile,
    job: Job,
    outcome_id: str,
    failure: ExecutionFailure,
    processed_at: str,
    trigger_id: str,
) -> ValidatedTrigger:
    return ValidatedTrigger(
        trigger_id=trigger_id,
        trigger_type=TriggerType.EXECUTION_FAILED,
        case_id=job.case_id,
        expected_case_revision=snapshot.cases[job.case_id].case.case_revision,
        idempotency_key=outcome_id,
        payload=ExecutionFailedTriggerPayload(
            source_job_id=job.job_id,
            source_outcome_id=outcome_id,
            execution_failure=failure,
        ),
        continuation_resources=ContinuationResourceView(
            evidence_refs=[],
            attachment_refs=[],
            artifact_refs=[],
            previous_outcome_refs=[],
        ),
        runtime_bindings_by_job_type={},
        occurred_at=processed_at,
    )


def _stale_trigger(
    snapshot: StateFile,
    job: Job,
    outcome: JobOutcome,
    processed_at: str,
    trigger_id: str,
) -> ValidatedTrigger:
    aggregate = snapshot.cases[job.case_id]
    return ValidatedTrigger(
        trigger_id=trigger_id,
        trigger_type=TriggerType.STALE_ACTIVE_OUTCOME,
        case_id=job.case_id,
        expected_case_revision=aggregate.case.case_revision,
        idempotency_key=outcome.outcome_id,
        payload=StaleActiveOutcomeTriggerPayload(
            source_job_id=job.job_id,
            outcome_id=outcome.outcome_id,
            expected_base_state_revision=outcome.base_state_revision,
            actual_state_revision=aggregate.case.diagnosis_state.revision,
        ),
        continuation_resources=ContinuationResourceView(
            evidence_refs=[],
            attachment_refs=[],
            artifact_refs=[],
            previous_outcome_refs=[],
        ),
        runtime_bindings_by_job_type={},
        occurred_at=processed_at,
    )


def _validate_user_result_bytes(job: Job, outcome: JobOutcome) -> None:
    result = _expected_user_result(job, outcome)
    if result is not None:
        validate_user_result_for_outcome(job, outcome, canonical_json_bytes(result))


def _expected_user_result(
    job: Job,
    outcome: JobOutcome,
) -> UserResultPayload | None:
    payload = outcome.payload
    if not isinstance(payload, DiagnosisOutcome):
        return None
    candidate = payload.candidate_conclusion_draft
    if candidate is None:
        return None
    return UserResultPayload(
        schema_version=1,
        format_id="problem-locator-diagnosis-v1",
        problem_statement=job.context_snapshot.problem_spec.statement,
        candidate_statement=candidate.statement,
        supporting_evidence_bindings=candidate.supporting_evidence_bindings,
        completion_criteria_mapping=candidate.completion_criteria_mapping,
    )


def _audit_case(case: Case, processed_at: str) -> Case:
    payload = case.model_dump(mode="python")
    payload.update(
        case_revision=case.case_revision + 1,
        updated_at=processed_at,
    )
    return Case.model_validate(payload)


def _processing_record(
    claimed: JobOutcome,
    outcome_file_ref: ExecutionFileRef,
    *,
    disposition: OutcomeDisposition,
    processed_at: str,
    error_code: ErrorCode | None,
    reason: str,
) -> OutcomeProcessingRecord:
    return OutcomeProcessingRecord(
        outcome_id=claimed.outcome_id,
        job_id=claimed.job_id,
        outcome_hash=outcome_file_ref.sha256,
        outcome_file_ref=outcome_file_ref,
        disposition=disposition,
        processed_at=processed_at,
        error_code=error_code,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason=reason,
    )


def _validate_applied_outcome_lifecycle(
    source_job: Job,
    outcome: JobOutcome,
    plan: TransitionPlan,
) -> None:
    """Require an APPLIED plan to terminate exactly its active source Job."""

    if (
        source_job.case_id != outcome.case_id
        or source_job.job_id != outcome.job_id
        or source_job.status is not JobStatus.RUNNING
        or not plan.clear_active_job
        or len(plan.job_updates) != 1
    ):
        raise ValueError("Outcome plan must terminate exactly its active source Job")

    if outcome.result_type is OutcomeResultType.FAILED:
        if outcome.error is None:
            raise ValueError("FAILED Outcome requires its frozen ExecutionFailure")
        expected_target_status = (
            JobStatus.INTERRUPTED if outcome.error.retryable else JobStatus.FAILED
        )
        expected_case_status = (
            CaseStatus.INTERRUPTED
            if expected_target_status is JobStatus.INTERRUPTED
            else CaseStatus.FAILED
        )
        if (
            plan.target_case_status is not expected_case_status
            or plan.next_job_spec is not None
        ):
            raise ValueError("FAILED Outcome plan has an invalid terminal transition")
    else:
        expected_target_status = JobStatus.SUCCEEDED

    update = plan.job_updates[0]
    if (
        update.job_id != source_job.job_id
        or update.expected_status is not JobStatus.RUNNING
        or update.target_status is not expected_target_status
        or update.started_at is not None
        or update.finished_at != outcome.produced_at
        or update.runtime_epoch is not None
    ):
        raise ValueError("Outcome plan has an invalid source Job lifecycle update")


__all__ = ["OutcomeSubmissionService"]
