from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.application.outcome_submission import (
    OutcomeSubmissionService,
    _expected_user_result,
)
from problem_locator.application.preparation import (
    make_user_fact,
    runtime_bindings_from_job,
)
from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    ArtifactKind,
    ArtifactProposal,
    CandidateMutation,
    CandidateMutationAction,
    CandidateStatus,
    CaseFailure,
    CaseFailureUpdate,
    CaseStatus,
    DiagnosticExportMetadata,
    DiagnosisStateDelta,
    ErrorCode,
    ERROR_SPECS,
    Evidence,
    EvidenceSourceType,
    ExecutionFileRef,
    FieldUpdateAction,
    Job,
    JobLifecycleUpdate,
    JobOutcome,
    JobSpec,
    JobStatus,
    JobType,
    OutcomeDisposition,
    PlannedResourceBinding,
    ResourceKind,
    ResourceRef,
    ReviewTargetBinding,
    RuntimeBindings,
    SelectedSkillUpdate,
    StateFile,
    TransitionPlan,
    TriggerType,
    UserFactInput,
    VersionedRef,
    canonical_json_bytes,
    validate_user_result_for_outcome,
)
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryBinaryStream,
    InMemoryExecutionRecordStore,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    PureContextSnapshotProjector,
    RecordingDispatcher,
    ScriptedCoordinator,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/contracts/positive"

CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000010"
DIAGNOSE_JOB_ID = "00000000-0000-0000-0000-000000000011"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
USER_FACT_ID = "00000000-0000-0000-0000-000000000030"
USER_FACT_TRIGGER_ID = "00000000-0000-0000-0000-000000000031"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000090"
PROCESSED_AT = "2026-07-31T00:05:00.000Z"


class _CountingRepository(InMemoryStateRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.read_snapshot_calls = 0

    def read_snapshot(self) -> StateFile:
        self.read_snapshot_calls += 1
        return super().read_snapshot()


class _ReadFailsAfterCommitRepository(_CountingRepository):
    def __init__(self, state: StateFile) -> None:
        super().__init__(state)
        self.fail_next_read = False

    def read_snapshot(self) -> StateFile:
        if self.fail_next_read:
            self.fail_next_read = False
            raise _port_error(ErrorCode.STATE_CORRUPT, "injected post-commit failure")
        return super().read_snapshot()

    def commit(self, expected_generation, expected_case_revision, mutation):
        receipt = super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )
        self.fail_next_read = True
        return receipt


class _RaisingDispatcher(RecordingDispatcher):
    def submit(self, job_id: str):
        self.submit_calls.append(job_id)
        raise RuntimeError("injected dispatch failure")


class _WrongStorageKeyResourceStore(InMemoryResourceStore):
    def publish(self, staged_ref, final_storage_key):
        resource_ref = super().publish(staged_ref, final_storage_key)
        return resource_ref.model_copy(
            update={
                "storage_key": (
                    f"resources/cases/{CASE_ID}/artifacts/"
                    "00000000-0000-0000-0000-000000000099/payload"
                )
            }
        )


class _PublishThenFailExecutionRecordStore(InMemoryExecutionRecordStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after_publish: ApplicationPortError | None = None

    def publish_job(self, job: Job) -> ExecutionFileRef:
        file_ref = super().publish_job(job)
        failure = self.fail_after_publish
        self.fail_after_publish = None
        if failure is not None:
            raise failure
        return file_ref


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _running_state() -> StateFile:
    payload = _load("state.json")
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["jobs"][JOB_ID].update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:01:00.000Z",
        runtime_epoch=RUNTIME_EPOCH,
    )
    return StateFile.model_validate(payload)


def _running_diagnosis_candidate_state() -> StateFile:
    payload = _load("state.json")
    source_payload = _load("job-diagnose.json")
    source_payload.update(
        status=JobStatus.RUNNING,
        started_at="2026-07-31T00:01:00.000Z",
        runtime_epoch=RUNTIME_EPOCH,
        attachment_refs=[],
        artifact_refs=[],
        previous_outcome_refs=[],
    )
    source = Job.model_validate(source_payload)
    fact = make_user_fact(
        UserFactInput(name="request_id", value="payment-42"),
        item_id=USER_FACT_ID,
        trigger_id=USER_FACT_TRIGGER_ID,
        created_revision=2,
    )
    evidence = Evidence(
        evidence_id=EVIDENCE_ID,
        case_id=CASE_ID,
        source_type=EvidenceSourceType.USER_FACT,
        source_ref=USER_FACT_ID,
        locator={"kind": "USER_FACT", "input_name": "request_id"},
        summary="The request identifier observed in the diagnosis input.",
        collected_at="2026-07-31T00:00:30.000Z",
        content_hash=None,
        resource_ref=None,
    )
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        case_revision=2,
        active_job_id=source.job_id,
        selected_skill_ref=source.skill_ref,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["case"]["diagnosis_state"].update(
        revision=2,
        user_facts=[fact.model_dump(mode="python")],
        evidence_refs=[EVIDENCE_ID],
    )
    aggregate["jobs"] = {
        source.job_id: source.model_dump(mode="python")
    }
    aggregate["evidence"] = {
        evidence.evidence_id: evidence.model_dump(mode="python")
    }
    payload["generation"] = 2
    return StateFile.model_validate(payload)


def _cancelled_state() -> StateFile:
    payload = _load("state.json")
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(
        status=CaseStatus.CANCELLED,
        case_revision=2,
        active_job_id=None,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    aggregate["jobs"][JOB_ID].update(
        status=JobStatus.CANCELLED,
        finished_at="2026-07-31T00:01:00.000Z",
    )
    return StateFile.model_validate(payload)


def _active_base_drift_state() -> StateFile:
    payload = _running_state().model_dump(mode="python")
    payload["cases"][CASE_ID]["case"]["diagnosis_state"]["revision"] = 2
    return StateFile.model_validate(payload)


def _outcome(name: str) -> JobOutcome:
    return JobOutcome.model_validate(_load(name))


def _file_ref(outcome: JobOutcome) -> ExecutionFileRef:
    data = canonical_json_bytes(outcome)
    return ExecutionFileRef(
        relative_key=f"jobs/{outcome.job_id}/job_outcome.json",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


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


def _failed_plan(snapshot, trigger, *, applied: bool) -> TransitionPlan:
    job = snapshot.active_job
    assert job is not None
    if applied:
        assert trigger.trigger_type is TriggerType.ROUTE_OUTCOME
        failure = trigger.payload.job_outcome.error
        assert failure is not None
    else:
        assert trigger.trigger_type is TriggerType.EXECUTION_FAILED
        failure = trigger.payload.execution_failure
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.FAILED,
        job_updates=[
            JobLifecycleUpdate(
                job_id=job.job_id,
                expected_status=JobStatus.RUNNING,
                target_status=JobStatus.FAILED,
                started_at=None,
                finished_at=trigger.occurred_at,
                runtime_epoch=None,
            )
        ],
        outcome_disposition=(
            OutcomeDisposition.APPLIED if applied else None
        ),
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=CaseFailureUpdate(
            action=FieldUpdateAction.SET,
            value=CaseFailure(
                code=failure.code,
                message=failure.message,
                source_job_id=job.job_id,
                source_outcome_id=(
                    trigger.payload.job_outcome.outcome_id if applied else trigger.payload.source_outcome_id
                ),
                occurred_at=trigger.occurred_at,
            ),
        ),
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Apply the finalized Outcome failure atomically.",
    )


def _interrupt_stale_active_plan(snapshot, trigger) -> TransitionPlan:
    assert trigger.trigger_type is TriggerType.STALE_ACTIVE_OUTCOME
    job = snapshot.active_job
    assert job is not None
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.INTERRUPTED,
        job_updates=[
            JobLifecycleUpdate(
                job_id=job.job_id,
                expected_status=JobStatus.RUNNING,
                target_status=JobStatus.INTERRUPTED,
                started_at=None,
                finished_at=trigger.occurred_at,
                runtime_epoch=None,
            )
        ],
        outcome_disposition=None,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Interrupt the active Job and atomically retain its stale Outcome audit.",
    )


def _service(
    state: StateFile | InMemoryStateRepository,
    coordinator: ScriptedCoordinator,
    execution_records: InMemoryExecutionRecordStore,
    *,
    catalog: FakeAssetCatalog | None = None,
    ids: DeterministicIdGenerator | None = None,
    resources: InMemoryResourceStore | None = None,
    guard: InMemoryPublicationCommitGuard | None = None,
    dispatcher: RecordingDispatcher | None = None,
):
    repository = (
        state if isinstance(state, InMemoryStateRepository) else InMemoryStateRepository(state)
    )
    guard = guard or InMemoryPublicationCommitGuard()
    resources = resources or InMemoryResourceStore(publication_guard=guard)
    dispatcher = dispatcher or RecordingDispatcher()
    notifier = InMemoryStateChangeNotifier()
    clock = FakeClock(PROCESSED_AT)
    service = OutcomeSubmissionService(
        repository,
        resources,
        guard,
        execution_records,
        coordinator,
        PureContextSnapshotProjector(),
        catalog or FakeAssetCatalog(),
        dispatcher,
        notifier,
        clock,
        ids or DeterministicIdGenerator(),
    )
    return service, repository, resources, guard, dispatcher, notifier, clock


def _route_to_diagnose_plan(snapshot, trigger) -> TransitionPlan:
    assert trigger.trigger_type is TriggerType.ROUTE_OUTCOME
    source = snapshot.active_job
    assert source is not None
    bindings = trigger.runtime_bindings_by_job_type[JobType.DIAGNOSE]
    outcome = trigger.payload.job_outcome
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.RUNNING,
        job_updates=[
            JobLifecycleUpdate(
                job_id=source.job_id,
                expected_status=JobStatus.RUNNING,
                target_status=JobStatus.SUCCEEDED,
                started_at=None,
                finished_at=trigger.occurred_at,
                runtime_epoch=None,
            )
        ],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=SelectedSkillUpdate(
            action=FieldUpdateAction.SET,
            value=outcome.payload.skill_ref,
        ),
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=JobSpec(
            job_type=JobType.DIAGNOSE,
            goal="Diagnose the routed RPC timeout.",
            target_state_revision=snapshot.case.diagnosis_state.revision,
            evidence_bindings=[],
            attachment_refs=[],
            previous_outcome_refs=[outcome.outcome_id],
            artifact_bindings=[],
            agent_profile_ref=bindings.agent_profile_ref,
            available_skill_refs=list(bindings.available_skill_refs),
            skill_ref=bindings.skill_ref,
            tool_bundle_ref=bindings.tool_bundle_ref,
            context_policy_ref=bindings.context_policy_ref,
            output_contract_ref=bindings.output_contract_ref,
            logparse_tool_ref=bindings.logparse_tool_ref,
            logparse_product=bindings.logparse_product,
            review_target_binding=None,
            replacement_for_job_id=None,
            resource_limits=bindings.resource_limits,
        ),
        final_result_target=None,
        clear_active_job=True,
        reason="Apply the route and create its fixed DIAGNOSE Job.",
    )


def _diagnosis_to_review_plan(snapshot, trigger) -> TransitionPlan:
    assert trigger.trigger_type is TriggerType.DIAGNOSIS_OUTCOME
    source = snapshot.active_job
    assert source is not None
    outcome = trigger.payload.job_outcome
    candidate = outcome.payload.candidate_conclusion_draft
    assert candidate is not None
    bindings = trigger.runtime_bindings_by_job_type[JobType.REVIEW]
    review_template = Job.model_validate(_load("job-review.json"))
    return TransitionPlan(
        accepted_state_delta=outcome.payload.state_delta,
        target_case_status=CaseStatus.REVIEWING,
        job_updates=[
            JobLifecycleUpdate(
                job_id=source.job_id,
                expected_status=JobStatus.RUNNING,
                target_status=JobStatus.SUCCEEDED,
                started_at=None,
                finished_at=trigger.occurred_at,
                runtime_epoch=None,
            )
        ],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=["user_result"],
        accepted_candidate_proposal_key=candidate.proposal_key,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=CandidateMutation(
            action=CandidateMutationAction.INSTALL,
            candidate_binding=ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key=candidate.proposal_key,
            ),
            expected_status=None,
            target_status=CandidateStatus.REVIEWING,
            reason=None,
        ),
        next_job_spec=JobSpec(
            job_type=JobType.REVIEW,
            goal=review_template.goal,
            target_state_revision=snapshot.case.diagnosis_state.revision + 1,
            evidence_bindings=[
                PlannedResourceBinding(
                    existing_resource_id=evidence_id,
                    accepted_proposal_key=None,
                )
                for evidence_id in source.evidence_refs
            ],
            attachment_refs=[],
            previous_outcome_refs=[outcome.outcome_id],
            artifact_bindings=[],
            agent_profile_ref=bindings.agent_profile_ref,
            available_skill_refs=list(bindings.available_skill_refs),
            skill_ref=bindings.skill_ref,
            tool_bundle_ref=bindings.tool_bundle_ref,
            context_policy_ref=bindings.context_policy_ref,
            output_contract_ref=bindings.output_contract_ref,
            logparse_tool_ref=bindings.logparse_tool_ref,
            logparse_product=bindings.logparse_product,
            review_target_binding=ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key=candidate.proposal_key,
            ),
            replacement_for_job_id=None,
            resource_limits=bindings.resource_limits,
        ),
        final_result_target=None,
        clear_active_job=True,
        reason="Install the fixed Candidate and create its REVIEW Job.",
    )


def _route_to_diagnose_with_export_plan(snapshot, trigger) -> TransitionPlan:
    plan = _route_to_diagnose_plan(snapshot, trigger)
    payload = plan.model_dump(mode="python")
    payload["accepted_artifact_proposal_keys"] = ["diagnostic_export"]
    return TransitionPlan.model_validate(payload)


def _route_outcome_with_export(
    resources: InMemoryResourceStore,
) -> tuple[JobOutcome, object]:
    body = b'{"diagnostic":"export"}\n'
    staged = resources.stage_file(
        JOB_ID,
        "diagnostic_export",
        InMemoryBinaryStream(body),
        expected_size=len(body),
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )
    payload = _outcome("job-outcome-route.json").model_dump(mode="python")
    payload["proposed_artifacts"] = [
        ArtifactProposal(
            proposal_key=staged.proposal_key,
            artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
            name="diagnostic-export.json",
            content_type="application/json",
            resource_kind=ResourceKind.FILE,
            size=staged.size,
            sha256=staged.sha256,
            staged_resource_ref=staged,
            metadata=DiagnosticExportMetadata(
                schema_version=1,
                format_id="diagnostic-export-v1",
                description="A deterministic diagnostic export.",
            ),
        )
    ]
    return JobOutcome.model_validate(payload), staged


def _diagnose_catalog(bindings: RuntimeBindings) -> FakeAssetCatalog:
    assert bindings.skill_ref is not None
    ref = bindings.skill_ref
    return FakeAssetCatalog(
        diagnose={(ref.id, ref.version, ref.content_hash): bindings}
    )


def _port_error(
    code: ErrorCode,
    message: str,
    *,
    details: tuple[ApplicationErrorDetail, ...] = (),
) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=list(details),
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


@pytest.mark.parametrize(
    "invalid_field",
    ["outcome", "nested_ref", "file_ref", "binding"],
)
def test_submit_outcome_rebuilds_raw_nested_dtos_before_every_dependency(
    invalid_field: str,
) -> None:
    outcome = _outcome("job-outcome-route.json")
    file_ref = _file_ref(outcome)
    invalid_outcome: object = outcome
    invalid_ref: object = file_ref
    if invalid_field == "outcome":
        payload = outcome.model_dump(mode="python")
        payload["job_id"] = "not-a-job-id"
        invalid_outcome = JobOutcome.model_construct(**payload)
    elif invalid_field == "nested_ref":
        invalid_skill_ref = VersionedRef.model_construct(
            id="",
            version="",
            content_hash="not-a-sha256",
        )
        invalid_payload = outcome.payload.model_copy(
            update={"skill_ref": invalid_skill_ref}
        )
        invalid_outcome = outcome.model_copy(update={"payload": invalid_payload})
    elif invalid_field == "file_ref":
        payload = file_ref.model_dump(mode="python")
        payload["size"] = -1
        invalid_ref = ExecutionFileRef.model_construct(**payload)
    else:
        payload = file_ref.model_dump(mode="python")
        payload["sha256"] = "f" * 64
        invalid_ref = ExecutionFileRef.model_construct(**payload)

    repository = _CountingRepository(_running_state())
    records = InMemoryExecutionRecordStore()
    coordinator = ScriptedCoordinator()
    catalog = FakeAssetCatalog()
    ids = DeterministicIdGenerator(seed="raw-submit-validation")
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    service, _, _, _, dispatcher, notifier, clock = _service(
        repository,
        coordinator,
        records,
        catalog=catalog,
        ids=ids,
        resources=resources,
        guard=guard,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(invalid_outcome, invalid_ref)  # type: ignore[arg-type]

    assert captured.value.error.code is ErrorCode.VALIDATION_ERROR
    assert captured.value.error.retryable is False
    assert clock.calls == 0
    assert ids.new_calls == []
    assert ids.derive_calls == []
    assert repository.read_snapshot_calls == 0
    assert repository.commit_calls == []
    assert records.publish_job_calls == []
    assert records.publish_outcome_calls == []
    assert catalog.check_calls == []
    assert catalog.resolve_calls == []
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert coordinator.calls == []
    assert resources.validate_staged_calls == []
    assert guard.acquire_calls == guard.release_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_submit_outcome_propagates_precommit_state_read_fault_exactly(
    code: ErrorCode,
) -> None:
    outcome = _outcome("job-outcome-route.json")
    file_ref = _file_ref(outcome)
    repository = InMemoryStateRepository(_running_state())
    state_fault = _port_error(code, "The frozen state cannot be read.")
    repository.inject_read_failure("read_snapshot", state_fault)
    records = InMemoryExecutionRecordStore()
    coordinator = ScriptedCoordinator()
    catalog = FakeAssetCatalog()
    ids = DeterministicIdGenerator(seed="state-read-fault")
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    service, _, _, _, dispatcher, notifier, _ = _service(
        repository,
        coordinator,
        records,
        catalog=catalog,
        ids=ids,
        resources=resources,
        guard=guard,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value is state_fault
    assert repository.commit_calls == []
    assert records.publish_job_calls == []
    assert records.publish_outcome_calls == []
    assert catalog.check_calls == []
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert coordinator.calls == []
    assert resources.validate_staged_calls == []
    assert guard.acquire_calls == guard.release_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_missing_finalized_record_is_rejected_atomically_then_replays_duplicate() -> None:
    outcome = _outcome("job-outcome-route.json")
    file_ref = _file_ref(outcome)
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=False)]
    )
    service, repository, resources, guard, dispatcher, notifier, _ = _service(
        _running_state(),
        coordinator,
        InMemoryExecutionRecordStore(),
    )

    rejected = service.submit_outcome(outcome, file_ref)

    assert rejected.disposition is OutcomeDisposition.REJECTED
    state = repository.read_snapshot()
    aggregate = state.cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.REJECTED
    assert processing.error_code is ErrorCode.OUTCOME_MISSING
    assert outcome.outcome_id not in aggregate.outcomes
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert aggregate.case.status is CaseStatus.FAILED
    assert aggregate.case.case_revision == 3
    assert aggregate.case.diagnosis_state.revision == 1
    assert resources.publish_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, state.generation)]

    commit_count = len(repository.commit_calls)
    coordinator_count = len(coordinator.calls)
    duplicate = service.submit_outcome(outcome, file_ref)
    assert duplicate.disposition is OutcomeDisposition.DUPLICATE
    assert len(repository.commit_calls) == commit_count
    assert len(coordinator.calls) == coordinator_count
    assert guard.acquire_calls == guard.release_calls == 1


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("missing", ErrorCode.OUTCOME_MISSING),
        ("record_error", ErrorCode.EXECUTION_RECORD_FAILED),
        ("receipt_mismatch", ErrorCode.OUTCOME_INVALID),
    ],
)
def test_untrusted_claimed_outcome_cannot_discard_staged_resources(
    fault: str,
    expected_code: ErrorCode,
) -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    claimed_outcome, staged = _route_outcome_with_export(resources)
    claimed_ref = _file_ref(claimed_outcome)
    records = InMemoryExecutionRecordStore()
    if fault == "record_error":
        records.inject_failure(
            "read_published_outcome",
            _port_error(
                ErrorCode.EXECUTION_RECORD_FAILED,
                "The finalized record is corrupt.",
            ),
        )
    elif fault == "receipt_mismatch":
        canonical_outcome = _outcome("job-outcome-route.json")
        records.publish_outcome_bytes(
            canonical_outcome.job_id,
            canonical_json_bytes(canonical_outcome),
        )

    service, repository, _, _, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator(
            [
                lambda snapshot, trigger: _failed_plan(
                    snapshot,
                    trigger,
                    applied=False,
                )
            ]
        ),
        records,
        resources=resources,
        guard=guard,
    )

    receipt = service.submit_outcome(claimed_outcome, claimed_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    processing = repository.read_snapshot().cases[CASE_ID].outcome_processing_records[
        claimed_outcome.outcome_id
    ]
    assert processing.error_code is expected_code
    assert resources.discard_calls == []
    assert resources.staged_resource_count == 1
    assert staged.proposal_key == "diagnostic_export"
    assert guard.acquire_calls == guard.release_calls == 1


def test_rejection_control_plan_cannot_accept_or_smuggle_outcome_proposals() -> None:
    outcome = _outcome("job-outcome-route.json")
    file_ref = _file_ref(outcome)

    def invalid_failure_plan(snapshot, trigger):
        return _failed_plan(snapshot, trigger, applied=False).model_copy(
            update={"accepted_evidence_proposal_keys": ["smuggled"]}
        )

    service, repository, _, guard, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([invalid_failure_plan]),
        InMemoryExecutionRecordStore(),
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0


def test_finalized_outcome_for_cancelled_job_is_saved_as_stale_audit() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    coordinator = ScriptedCoordinator()
    service, repository, _, guard, dispatcher, notifier, _ = _service(
        _cancelled_state(),
        coordinator,
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.STALE
    state = repository.read_snapshot()
    aggregate = state.cases[CASE_ID]
    assert aggregate.outcomes[outcome.outcome_id] == outcome
    assert (
        aggregate.outcome_processing_records[outcome.outcome_id].disposition
        is OutcomeDisposition.STALE
    )
    assert aggregate.case.status is CaseStatus.CANCELLED
    assert aggregate.case.case_revision == 3
    assert aggregate.jobs[JOB_ID].status is JobStatus.CANCELLED
    assert coordinator.calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, state.generation)]


def test_stale_outcome_with_forged_job_binding_is_rejected_before_stale_audit() -> None:
    payload = _outcome("job-outcome-route.json").model_dump(mode="python")
    payload["payload"]["skill_ref"] = VersionedRef(
        id="unavailable-skill",
        version="9.9.9",
        content_hash="9" * 64,
    )
    outcome = JobOutcome.model_validate(payload)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    coordinator = ScriptedCoordinator()
    service, repository, _, guard, dispatcher, _, _ = _service(
        _cancelled_state(),
        coordinator,
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.REJECTED
    assert processing.error_code is ErrorCode.OUTCOME_INVALID
    assert aggregate.case.status is CaseStatus.CANCELLED
    assert coordinator.calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_active_base_drift_interrupt_and_stale_audit_share_one_commit() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    coordinator = ScriptedCoordinator([_interrupt_stale_active_plan])
    service, repository, resources, guard, dispatcher, notifier, _ = _service(
        _active_base_drift_state(),
        coordinator,
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.STALE
    state = repository.read_snapshot()
    aggregate = state.cases[CASE_ID]
    assert aggregate.outcomes[outcome.outcome_id] == outcome
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.STALE
    assert processing.error_code is None
    assert aggregate.jobs[JOB_ID].status is JobStatus.INTERRUPTED
    assert aggregate.case.status is CaseStatus.INTERRUPTED
    assert aggregate.case.active_job_id is None
    assert aggregate.case.case_revision == 3
    assert aggregate.case.diagnosis_state.revision == 2
    assert len(repository.commit_calls) == 1
    assert coordinator.calls[0][1].trigger_type is TriggerType.STALE_ACTIVE_OUTCOME
    assert resources.publish_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, state.generation)]


def test_stale_active_control_plan_cannot_smuggle_semantic_acceptance() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )

    def invalid_stale_plan(snapshot, trigger):
        return _interrupt_stale_active_plan(snapshot, trigger).model_copy(
            update={"accepted_artifact_proposal_keys": ["smuggled"]}
        )

    service, repository, _, guard, _, _, _ = _service(
        _active_base_drift_state(),
        ScriptedCoordinator([invalid_stale_plan]),
        records,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0


@pytest.mark.parametrize("control_path", ["rejection", "stale"])
def test_invalid_secondary_coordinator_union_uses_typed_state_write_failure(
    control_path: str,
) -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    if control_path == "stale":
        file_ref = records.publish_outcome_bytes(
            outcome.job_id,
            canonical_json_bytes(outcome),
        )
        state = _active_base_drift_state()
    else:
        file_ref = _file_ref(outcome)
        state = _running_state()
    invalid_decision = ApplicationError(
        code=ErrorCode.ACTIVE_JOB_EXISTS,
        message="This error code is not legal for the secondary control Trigger.",
        details=[],
        retryable=False,
    )
    coordinator = ScriptedCoordinator([invalid_decision])
    service, repository, _, guard, _, _, _ = _service(
        state,
        coordinator,
        records,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1].trigger_type is (
        TriggerType.STALE_ACTIVE_OUTCOME
        if control_path == "stale"
        else TriggerType.EXECUTION_FAILED
    )


def test_finalized_failure_outcome_applies_with_processing_in_one_commit() -> None:
    outcome = _outcome("job-outcome-failure.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=True)]
    )
    service, repository, resources, guard, dispatcher, notifier, _ = _service(
        _running_state(),
        coordinator,
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    state = repository.read_snapshot()
    aggregate = state.cases[CASE_ID]
    assert aggregate.outcomes[outcome.outcome_id] == outcome
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.APPLIED
    assert processing.error_code is None
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert aggregate.case.status is CaseStatus.FAILED
    assert aggregate.case.case_revision == 3
    assert aggregate.case.diagnosis_state.revision == 1
    assert len(repository.commit_calls) == 1
    assert resources.publish_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, state.generation)]


def test_post_commit_state_read_failure_cannot_replace_applied_outcome() -> None:
    outcome = _outcome("job-outcome-failure.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    repository = _ReadFailsAfterCommitRepository(_running_state())
    service, _, _, guard, _, notifier, _ = _service(
        repository,
        ScriptedCoordinator(
            [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=True)]
        ),
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    assert receipt.case_view.status is CaseStatus.FAILED
    assert repository.fail_next_read is True
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls
    repository.fail_next_read = False
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.outcome_processing_records[outcome.outcome_id].disposition is OutcomeDisposition.APPLIED


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "wrong_job",
        "wrong_expected_status",
        "wrong_target_status",
        "rewrite_started_at",
        "wrong_finished_at",
        "rewrite_runtime_epoch",
        "does_not_clear_source",
    ],
)
def test_applied_plan_must_exactly_terminate_its_active_source_job(
    fault: str,
) -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )

    def invalid_applied_plan(snapshot, trigger):
        plan = _route_to_diagnose_plan(snapshot, trigger)
        update = plan.job_updates[0]
        if fault == "missing":
            return plan.model_copy(update={"job_updates": []})
        if fault == "extra":
            extra = update.model_copy(
                update={"job_id": "00000000-0000-0000-0000-000000000098"}
            )
            return plan.model_copy(update={"job_updates": [update, extra]})
        if fault == "does_not_clear_source":
            return plan.model_copy(update={"clear_active_job": False})
        replacements = {
            "wrong_job": {
                "job_id": "00000000-0000-0000-0000-000000000098"
            },
            "wrong_expected_status": {"expected_status": JobStatus.PENDING},
            "wrong_target_status": {"target_status": JobStatus.FAILED},
            "rewrite_started_at": {"started_at": outcome.produced_at},
            "wrong_finished_at": {"finished_at": PROCESSED_AT},
            "rewrite_runtime_epoch": {"runtime_epoch": RUNTIME_EPOCH},
        }
        return plan.model_copy(
            update={"job_updates": [update.model_copy(update=replacements[fault])]}
        )

    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    coordinator = ScriptedCoordinator(
        [
            invalid_applied_plan,
            lambda snapshot, trigger: _failed_plan(
                snapshot,
                trigger,
                applied=False,
            ),
        ]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=_diagnose_catalog(binding),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.OUTCOME_INVALID
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert len(aggregate.jobs) == 1
    assert len(repository.commit_calls) == 1
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_finalized_outcome_coordinator_semantic_error_becomes_deterministic_invalid() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    detail = ApplicationErrorDetail(
        field="route_decision",
        resource_type="JOB",
        resource_id=JOB_ID,
        resource_ref=None,
        expected="accepted",
        actual="invalid",
        limit=None,
        observed=None,
    )
    semantic_error = ApplicationError(
        code=ErrorCode.VALIDATION_ERROR,
        message="The finalized route decision is not semantically acceptable.",
        details=[detail],
        retryable=False,
    )

    def failure_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.EXECUTION_FAILED
        failure = trigger.payload.execution_failure
        assert failure.code is ErrorCode.OUTCOME_INVALID
        assert failure.retryable is False
        assert failure.details == [detail]
        return _failed_plan(snapshot, trigger, applied=False)

    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    coordinator = ScriptedCoordinator([semantic_error, failure_plan])
    service, repository, _, guard, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=_diagnose_catalog(binding),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert (
        aggregate.outcome_processing_records[outcome.outcome_id].error_code
        is ErrorCode.OUTCOME_INVALID
    )
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert len(coordinator.calls) == 2
    assert records.publish_job_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_outcome_coordinator_invalid_case_state_uses_stale_receipt_channel() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    stale_decision = ApplicationError(
        code=ErrorCode.INVALID_CASE_STATE,
        message="The Case no longer admits this finalized Outcome transition.",
        details=[],
        retryable=False,
    )
    coordinator = ScriptedCoordinator([stale_decision])
    service, repository, _, guard, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=_diagnose_catalog(binding),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.STALE
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.STALE
    assert processing.error_code is None
    assert aggregate.jobs[JOB_ID].status is JobStatus.RUNNING
    assert aggregate.case.active_job_id == JOB_ID
    assert records.publish_job_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_coordinator_cannot_replace_the_catalog_runtime_binding() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )

    def substituted_binding_plan(snapshot, trigger):
        plan = _route_to_diagnose_plan(snapshot, trigger)
        assert plan.next_job_spec is not None
        replacement = plan.next_job_spec.model_copy(
            update={
                "agent_profile_ref": VersionedRef(
                    id="replacement-profile",
                    version="9.9.9",
                    content_hash="9" * 64,
                )
            }
        )
        return plan.model_copy(update={"next_job_spec": replacement})

    coordinator = ScriptedCoordinator(
        [
            substituted_binding_plan,
            lambda snapshot, trigger: _failed_plan(
                snapshot,
                trigger,
                applied=False,
            ),
        ]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=_diagnose_catalog(binding),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert (
        aggregate.outcome_processing_records[outcome.outcome_id].error_code
        is ErrorCode.OUTCOME_INVALID
    )
    assert len(aggregate.jobs) == 1
    assert records.publish_job_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


@pytest.mark.parametrize(
    "code",
    [ErrorCode.ASSET_VERSION_UNAVAILABLE, ErrorCode.CONFIG_INVALID],
)
def test_finalized_outcome_asset_binding_failure_parks_outbox_until_retry(
    code: ErrorCode,
) -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    outcome, staged = _route_outcome_with_export(resources)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    catalog = _diagnose_catalog(binding)
    parked = _port_error(code, "The pinned Outcome binding is unavailable.")
    catalog.inject_failure("diagnose_bindings", parked)
    coordinator = ScriptedCoordinator([_route_to_diagnose_plan])
    service, repository, _, _, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=catalog,
        resources=resources,
        guard=guard,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value is parked
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert outcome.outcome_id not in aggregate.outcome_processing_records
    assert repository.commit_calls == []
    assert coordinator.calls == []
    assert records.publish_job_calls == []
    assert resources.publish_calls == []
    assert resources.discard_calls == []
    assert resources.staged_resource_count == 1
    assert staged.proposal_key == "diagnostic_export"
    assert guard.acquire_calls == guard.release_calls == 0

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    assert catalog.diagnose_calls == [outcome.payload.skill_ref]
    assert resources.discard_calls == [staged]
    assert dispatcher.submit_calls
    assert guard.acquire_calls == guard.release_calls == 1


def test_catalog_success_cannot_substitute_a_different_skill_version() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    expected_ref = outcome.payload.skill_ref
    assert expected_ref is not None
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    substituted = binding.model_copy(
        update={
            "skill_ref": VersionedRef(
                id=expected_ref.id,
                version="9.9.9",
                content_hash="8" * 64,
            )
        }
    )
    catalog = FakeAssetCatalog(
        diagnose={
            (expected_ref.id, expected_ref.version, expected_ref.content_hash): substituted
        }
    )
    service, repository, _, guard, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator(),
        records,
        catalog=catalog,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value.error.code is ErrorCode.CONFIG_INVALID
    assert repository.commit_calls == []
    assert records.publish_job_calls == []
    assert guard.acquire_calls == guard.release_calls == 0


def test_expected_user_result_matches_the_frozen_canonical_fixture() -> None:
    source = Job.model_validate(_load("job-diagnose.json"))
    outcome = _outcome("job-outcome-diagnosis.json")

    result = _expected_user_result(source, outcome)

    assert result is not None
    assert result.schema_version == 1
    assert result.format_id == "problem-locator-diagnosis-v1"
    result_bytes = canonical_json_bytes(result)
    assert result_bytes == (FIXTURES / "user-result.json").read_bytes()
    assert validate_user_result_for_outcome(
        source,
        outcome,
        result_bytes,
    ) == result


def test_candidate_outcome_formalizes_user_result_and_creates_review_job() -> None:
    state = _running_diagnosis_candidate_state()
    source = state.cases[CASE_ID].jobs[DIAGNOSE_JOB_ID]
    result_bytes = (FIXTURES / "user-result.json").read_bytes()
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    staged = resources.stage_file(
        source.job_id,
        "user_result",
        InMemoryBinaryStream(result_bytes),
        expected_size=len(result_bytes),
        expected_sha256=hashlib.sha256(result_bytes).hexdigest(),
    )
    outcome_payload = _load("job-outcome-diagnosis.json")
    outcome_payload["proposed_artifacts"][0]["staged_resource_ref"] = (
        staged.model_dump(mode="python")
    )
    outcome = JobOutcome.model_validate(outcome_payload)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    review_bindings = runtime_bindings_from_job(
        Job.model_validate(_load("job-review.json"))
    )
    assert source.skill_ref is not None
    catalog = FakeAssetCatalog(
        review={
            (
                source.skill_ref.id,
                source.skill_ref.version,
                source.skill_ref.content_hash,
            ): review_bindings
        }
    )
    coordinator = ScriptedCoordinator([_diagnosis_to_review_plan])
    service, repository, resources, guard, dispatcher, notifier, _ = _service(
        state,
        coordinator,
        records,
        catalog=catalog,
        resources=resources,
        guard=guard,
        ids=DeterministicIdGenerator(seed="candidate-user-result"),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.APPLIED
    assert len(processing.accepted_artifact_ids) == 1
    user_result = aggregate.artifacts[processing.accepted_artifact_ids[0]]
    assert user_result.kind is ArtifactKind.USER_RESULT
    assert user_result.metadata.schema_version == 1
    assert user_result.metadata.format_id == "problem-locator-diagnosis-v1"
    assert user_result.size == len(result_bytes)
    assert user_result.sha256 == hashlib.sha256(result_bytes).hexdigest()
    published = resources.open_read(
        ResourceRef(
            resource_kind=user_result.resource_kind,
            storage_key=user_result.storage_key,
            size=user_result.size,
            sha256=user_result.sha256,
        )
    )
    assert published.read(len(result_bytes) + 1) == result_bytes
    assert published.read(1) == b""
    published.close()
    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    assert candidate is not None
    assert candidate.status is CandidateStatus.REVIEWING
    assert candidate.supporting_evidence_refs == [EVIDENCE_ID]
    assert aggregate.case.status is CaseStatus.REVIEWING
    assert aggregate.case.diagnosis_state.revision == 3
    assert aggregate.jobs[source.job_id].status is JobStatus.SUCCEEDED
    assert processing.created_job_id is not None
    review_job = aggregate.jobs[processing.created_job_id]
    assert review_job.job_type is JobType.REVIEW
    assert review_job.review_target is not None
    assert review_job.review_target.candidate_conclusion_id == candidate.conclusion_id
    assert review_job.review_target.candidate_revision == candidate.revision
    assert review_job.review_target.candidate_content_hash == candidate.content_hash
    assert review_job.context_snapshot.candidate_conclusion == candidate
    assert catalog.review_calls == [source.skill_ref]
    assert [job.job_id for job in records.publish_job_calls] == [review_job.job_id]
    assert dispatcher.submit_calls == [review_job.job_id]
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 3)]
    assert len(coordinator.calls) == 1


def test_candidate_outcome_rejects_noncanonical_user_result_bytes() -> None:
    state = _running_diagnosis_candidate_state()
    wrong_bytes = b'{}\n'
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    staged = resources.stage_file(
        DIAGNOSE_JOB_ID,
        "user_result",
        InMemoryBinaryStream(wrong_bytes),
        expected_size=len(wrong_bytes),
        expected_sha256=hashlib.sha256(wrong_bytes).hexdigest(),
    )
    outcome_payload = _load("job-outcome-diagnosis.json")
    proposal = outcome_payload["proposed_artifacts"][0]
    proposal.update(
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged.model_dump(mode="python"),
    )
    outcome = JobOutcome.model_validate(outcome_payload)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=False)]
    )
    service, repository, resources, guard, dispatcher, notifier, _ = _service(
        state,
        coordinator,
        records,
        resources=resources,
        guard=guard,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.OUTCOME_INVALID
    assert processing.accepted_artifact_ids == []
    assert processing.created_job_id is None
    assert aggregate.artifacts == {}
    assert aggregate.case.diagnosis_state.candidate_conclusion is None
    assert records.publish_job_calls == []
    assert resources.publish_calls == []
    assert resources.discard_calls == [staged]
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 3)]
    assert len(coordinator.calls) == 1


def test_completed_diagnosis_without_candidate_offers_same_pinned_diagnose_binding() -> None:
    source_job = Job.model_validate(_load("job-diagnose.json"))
    assert source_job.skill_ref is not None
    payload = _outcome("job-outcome-diagnosis.json").model_dump(mode="python")
    payload["payload"]["candidate_conclusion_draft"] = None
    payload["proposed_artifacts"] = [
        proposal
        for proposal in payload["proposed_artifacts"]
        if proposal["artifact_kind"] is not ArtifactKind.USER_RESULT
    ]
    outcome = JobOutcome.model_validate(payload)
    binding = runtime_bindings_from_job(source_job)
    catalog = _diagnose_catalog(binding)
    service, _, _, _, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator(),
        InMemoryExecutionRecordStore(),
        catalog=catalog,
    )

    offered = service._bindings_for_outcome(source_job, outcome)

    assert offered == {JobType.DIAGNOSE: binding}
    assert catalog.diagnose_calls == [source_job.skill_ref]


def test_state_write_retry_reuses_published_next_job_a_not_current_catalog_b() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding_a = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    assert binding_a.skill_ref is not None
    binding_b_payload = binding_a.model_dump(mode="python")
    binding_b_payload["agent_profile_ref"] = VersionedRef(
        id="specialist-profile",
        version="2.0.0",
        content_hash="9" * 64,
    )
    binding_b = RuntimeBindings.model_validate(binding_b_payload)
    ids = DeterministicIdGenerator(seed="outcome-a-to-b")
    first_coordinator = ScriptedCoordinator([_route_to_diagnose_plan])
    first_service, repository, _, first_guard, first_dispatcher, _, _ = _service(
        _running_state(),
        first_coordinator,
        records,
        catalog=_diagnose_catalog(binding_a),
        ids=ids,
    )
    write_failure = _port_error(
        ErrorCode.STATE_WRITE_FAILED,
        "State commit failed after job.json publication.",
    )
    repository.fail_next_commit(write_failure)

    with pytest.raises(ApplicationPortError) as captured:
        first_service.submit_outcome(outcome, file_ref)

    assert captured.value is write_failure
    assert repository.read_snapshot().generation == 1
    assert first_dispatcher.submit_calls == []
    assert first_guard.acquire_calls == first_guard.release_calls == 1
    assert len(records.publish_job_calls) == 1
    published_a = records.publish_job_calls[0]
    assert published_a.agent_profile_ref == binding_a.agent_profile_ref

    catalog_b = _diagnose_catalog(binding_b)
    second_coordinator = ScriptedCoordinator([_route_to_diagnose_plan])
    second_service, _, _, second_guard, second_dispatcher, _, _ = _service(
        repository,
        second_coordinator,
        records,
        catalog=catalog_b,
        ids=DeterministicIdGenerator(seed="outcome-a-to-b"),
    )
    receipt = second_service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    assert catalog_b.diagnose_calls == []
    recovered_bindings = second_coordinator.calls[0][1].runtime_bindings_by_job_type[
        JobType.DIAGNOSE
    ]
    assert recovered_bindings == binding_a
    assert recovered_bindings != binding_b
    committed = repository.read_snapshot().cases[CASE_ID]
    created_job_id = committed.case.active_job_id
    assert created_job_id == published_a.job_id
    assert committed.jobs[created_job_id] == published_a
    assert len(records.publish_job_calls) == 1
    assert second_dispatcher.submit_calls == [created_job_id]
    assert second_guard.acquire_calls == second_guard.release_calls == 1


def test_first_next_job_publish_failure_has_no_disposition_and_reuses_durable_job() -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    outcome, staged = _route_outcome_with_export(resources)
    records = _PublishThenFailExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    publish_failure = _port_error(
        ErrorCode.EXECUTION_RECORD_FAILED,
        "The first next Job publication acknowledgement failed.",
    )
    records.fail_after_publish = publish_failure
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    catalog = _diagnose_catalog(binding)
    coordinator = ScriptedCoordinator(
        [
            _route_to_diagnose_with_export_plan,
            _route_to_diagnose_with_export_plan,
        ]
    )
    service, repository, _, _, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
        catalog=catalog,
        ids=DeterministicIdGenerator(seed="next-job-publication-retry"),
        resources=resources,
        guard=guard,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value is publish_failure
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert outcome.outcome_id not in aggregate.outcomes
    assert outcome.outcome_id not in aggregate.outcome_processing_records
    assert repository.commit_calls == []
    assert len(records.publish_job_calls) == 1
    published_job = records.publish_job_calls[0]
    assert catalog.diagnose_calls == [outcome.payload.skill_ref]
    assert len(resources.publish_calls) == 1
    assert resources.discard_calls == []
    assert resources.staged_resource_count == 0
    assert staged.proposal_key == "diagnostic_export"
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.disposition is OutcomeDisposition.APPLIED
    assert processing.created_job_id == published_job.job_id
    assert aggregate.jobs[published_job.job_id] == published_job
    assert len(aggregate.artifacts) == 1
    assert len(records.publish_job_calls) == 1
    assert catalog.diagnose_calls == [outcome.payload.skill_ref]
    assert len(resources.publish_calls) == 2
    assert resources.discard_calls == []
    assert dispatcher.submit_calls == [published_job.job_id]
    assert guard.acquire_calls == guard.release_calls == 2


def test_existing_next_job_record_read_fault_is_deterministically_rejected() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    records.inject_failure(
        "read_published_job",
        _port_error(
            ErrorCode.EXECUTION_RECORD_FAILED,
            "The existing deterministic next Job record is corrupt.",
        ),
    )
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=False)]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        _running_state(),
        coordinator,
        records,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.EXECUTION_RECORD_FAILED
    assert coordinator.calls[0][1].trigger_type is TriggerType.EXECUTION_FAILED
    assert records.publish_job_calls == []
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_legal_existing_next_job_with_drifted_bytes_is_rejected_without_overwrite() -> None:
    state = _running_state()
    outcome = _outcome("job-outcome-route.json")
    seed = "existing-next-job-byte-drift"
    prospective_job_id = DeterministicIdGenerator(seed=seed).derive(
        "job",
        [
            state.installation_id,
            CASE_ID,
            outcome.outcome_id,
            "next_job",
        ],
    )
    drifted_payload = Job.model_validate(
        _load("job-diagnose.json")
    ).model_dump(mode="python")
    drifted_payload.update(
        job_id=prospective_job_id,
        case_id=CASE_ID,
        status=JobStatus.PENDING,
        goal="A different but individually valid recovered Job goal.",
        created_at=outcome.produced_at,
        started_at=None,
        finished_at=None,
        runtime_epoch=None,
    )
    drifted_job = Job.model_validate(drifted_payload)
    records = InMemoryExecutionRecordStore()
    records.publish_job(drifted_job)
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    catalog = FakeAssetCatalog()
    coordinator = ScriptedCoordinator(
        [
            _route_to_diagnose_plan,
            lambda snapshot, trigger: _failed_plan(
                snapshot,
                trigger,
                applied=False,
            ),
        ]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        state,
        coordinator,
        records,
        catalog=catalog,
        ids=DeterministicIdGenerator(seed=seed),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.EXECUTION_RECORD_FAILED
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert records.publish_job_calls == [drifted_job]
    assert records.read_published_job(prospective_job_id).job == drifted_job
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert len(coordinator.calls) == 2
    assert coordinator.calls[1][1].trigger_type is TriggerType.EXECUTION_FAILED
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 2


def test_legal_existing_next_job_with_wrong_role_is_rejected_before_outcome_plan() -> None:
    state = _running_state()
    outcome = _outcome("job-outcome-route.json")
    seed = "existing-next-job-role-drift"
    prospective_job_id = DeterministicIdGenerator(seed=seed).derive(
        "job",
        [
            state.installation_id,
            CASE_ID,
            outcome.outcome_id,
            "next_job",
        ],
    )
    wrong_role_payload = Job.model_validate(
        _load("job-route.json")
    ).model_dump(mode="python")
    wrong_role_payload.update(
        job_id=prospective_job_id,
        case_id=CASE_ID,
        status=JobStatus.PENDING,
        created_at=outcome.produced_at,
        started_at=None,
        finished_at=None,
        runtime_epoch=None,
    )
    wrong_role_job = Job.model_validate(wrong_role_payload)
    records = InMemoryExecutionRecordStore()
    records.publish_job(wrong_role_job)
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    catalog = FakeAssetCatalog()
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=False)]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        state,
        coordinator,
        records,
        catalog=catalog,
        ids=DeterministicIdGenerator(seed=seed),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.EXECUTION_RECORD_FAILED
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert records.publish_job_calls == [wrong_role_job]
    assert records.read_published_job(prospective_job_id).job == wrong_role_job
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1].trigger_type is TriggerType.EXECUTION_FAILED
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_recovered_next_job_cannot_replace_the_outcome_selected_skill() -> None:
    state = _running_state()
    outcome = _outcome("job-outcome-route.json")
    expected_skill_ref = getattr(outcome.payload, "skill_ref", None)
    assert expected_skill_ref is not None
    seed = "existing-next-job-skill-drift"
    prospective_job_id = DeterministicIdGenerator(seed=seed).derive(
        "job",
        [
            state.installation_id,
            CASE_ID,
            outcome.outcome_id,
            "next_job",
        ],
    )
    replacement_skill_ref = VersionedRef(
        id=expected_skill_ref.id,
        version="9.9.9",
        content_hash="8" * 64,
    )
    drifted_payload = Job.model_validate(
        _load("job-diagnose.json")
    ).model_dump(mode="python")
    drifted_payload.update(
        job_id=prospective_job_id,
        case_id=CASE_ID,
        status=JobStatus.PENDING,
        skill_ref=replacement_skill_ref,
        created_at=outcome.produced_at,
        started_at=None,
        finished_at=None,
        runtime_epoch=None,
    )
    drifted_job = Job.model_validate(drifted_payload)
    records = InMemoryExecutionRecordStore()
    records.publish_job(drifted_job)
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    catalog = FakeAssetCatalog()
    coordinator = ScriptedCoordinator(
        [lambda snapshot, trigger: _failed_plan(snapshot, trigger, applied=False)]
    )
    service, repository, _, guard, dispatcher, _, _ = _service(
        state,
        coordinator,
        records,
        catalog=catalog,
        ids=DeterministicIdGenerator(seed=seed),
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.EXECUTION_RECORD_FAILED
    assert prospective_job_id not in aggregate.jobs
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert records.publish_job_calls == [drifted_job]
    assert records.read_published_job(prospective_job_id).job == drifted_job
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1].trigger_type is TriggerType.EXECUTION_FAILED
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_applied_outcome_discards_unaccepted_stage_after_releasing_lease() -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    payload = b'{"unused":true}\n'
    staged = resources.stage_file(
        JOB_ID,
        "unused_export",
        InMemoryBinaryStream(payload),
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    outcome_payload = _outcome("job-outcome-route.json").model_dump(mode="python")
    outcome_payload["proposed_artifacts"] = [
        ArtifactProposal(
            proposal_key=staged.proposal_key,
            artifact_kind=ArtifactKind.DIAGNOSTIC_EXPORT,
            name="unused-export.json",
            content_type="application/json",
            resource_kind=ResourceKind.FILE,
            size=staged.size,
            sha256=staged.sha256,
            staged_resource_ref=staged,
            metadata=DiagnosticExportMetadata(
                schema_version=1,
                format_id="diagnostic-export-v1",
                description="A valid but unaccepted diagnostic export.",
            ),
        )
    ]
    outcome = JobOutcome.model_validate(outcome_payload)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    service, repository, _, _, dispatcher, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([_route_to_diagnose_plan]),
        records,
        catalog=_diagnose_catalog(binding),
        resources=resources,
        guard=guard,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    assert repository.read_snapshot().cases[CASE_ID].artifacts == {}
    assert resources.publish_calls == []
    assert resources.discard_calls == [staged]
    assert resources.staged_resource_count == 0
    assert guard.acquire_calls == guard.release_calls == 1
    assert guard.held_by_current_thread() is False
    assert dispatcher.submit_calls


def test_duplicate_short_circuit_does_not_cleanup_untrusted_claimed_proposals() -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    canonical_outcome, first_stage = _route_outcome_with_export(resources)
    records = InMemoryExecutionRecordStore()
    canonical_ref = records.publish_outcome_bytes(
        canonical_outcome.job_id,
        canonical_json_bytes(canonical_outcome),
    )
    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    service, repository, _, _, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([_route_to_diagnose_plan]),
        records,
        catalog=_diagnose_catalog(binding),
        resources=resources,
        guard=guard,
    )
    applied = service.submit_outcome(canonical_outcome, canonical_ref)
    assert applied.disposition is OutcomeDisposition.APPLIED
    assert resources.discard_calls == [first_stage]
    assert resources.staged_resource_count == 0

    claimed_outcome, staged = _route_outcome_with_export(resources)
    assert claimed_outcome == canonical_outcome
    duplicate = service.submit_outcome(claimed_outcome, canonical_ref)

    assert duplicate.disposition is OutcomeDisposition.DUPLICATE
    assert resources.discard_calls == [first_stage]
    assert resources.staged_resource_count == 1
    assert staged.proposal_key == "diagnostic_export"
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1


def test_capacity_rejection_reloads_state_and_calls_failure_coordinator_unlocked() -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    outcome, staged = _route_outcome_with_export(resources)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    repository = _CountingRepository(_running_state())
    observed = MAX_CASE_RESOURCE_BYTES + 37
    capacity_detail = ApplicationErrorDetail(
        field="case_resource_bytes",
        resource_type="CASE",
        resource_id=CASE_ID,
        resource_ref=None,
        expected=None,
        actual=None,
        limit=MAX_CASE_RESOURCE_BYTES,
        observed=observed,
    )

    def failure_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.EXECUTION_FAILED
        assert repository.read_snapshot_calls == 2
        assert guard.held_by_current_thread() is False
        assert trigger.payload.execution_failure.details == [capacity_detail]
        assert trigger.payload.execution_failure.details[0].observed == observed
        return _failed_plan(snapshot, trigger, applied=False)

    coordinator = ScriptedCoordinator(
        [_route_to_diagnose_with_export_plan, failure_plan]
    )
    resources.inject_failure(
        "validate_case_capacity",
        _port_error(
            ErrorCode.RESOURCE_LIMIT_EXCEEDED,
            "Case resource capacity exceeded.",
            details=(capacity_detail,),
        ),
    )
    binding = runtime_bindings_from_job(Job.model_validate(_load("job-diagnose.json")))
    service, _, _, _, dispatcher, _, _ = _service(
        repository,
        coordinator,
        records,
        catalog=_diagnose_catalog(binding),
        resources=resources,
        guard=guard,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    current = repository.read_snapshot().cases[CASE_ID]
    processing = current.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert current.case.status is CaseStatus.FAILED
    assert resources.publish_calls == []
    assert resources.discard_calls == [staged]
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 2


def test_publish_receipt_with_wrong_storage_key_is_rejected_before_applied_commit() -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = _WrongStorageKeyResourceStore(publication_guard=guard)
    outcome, staged = _route_outcome_with_export(resources)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )

    def failure_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.EXECUTION_FAILED
        assert trigger.payload.execution_failure.code is ErrorCode.OUTCOME_INVALID
        assert guard.held_by_current_thread() is False
        return _failed_plan(snapshot, trigger, applied=False)

    binding = runtime_bindings_from_job(
        Job.model_validate(_load("job-diagnose.json"))
    )
    service, repository, _, _, dispatcher, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([_route_to_diagnose_with_export_plan, failure_plan]),
        records,
        catalog=_diagnose_catalog(binding),
        resources=resources,
        guard=guard,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.REJECTED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    processing = aggregate.outcome_processing_records[outcome.outcome_id]
    assert processing.error_code is ErrorCode.OUTCOME_INVALID
    assert aggregate.artifacts == {}
    assert aggregate.jobs[JOB_ID].status is JobStatus.FAILED
    assert len(aggregate.jobs) == 1
    assert len(repository.commit_calls) == 1
    assert len(resources.publish_calls) == 1
    assert resources.discard_calls == [staged]
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 2


@pytest.mark.parametrize(
    ("operation", "source_code"),
    [
        ("validate_case_capacity", ErrorCode.PATH_VIOLATION),
        ("publish", ErrorCode.RESOURCE_NOT_FOUND),
    ],
)
def test_resource_delivery_fault_is_retryable_and_creates_no_disposition(
    operation: str,
    source_code: ErrorCode,
) -> None:
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    outcome, staged = _route_outcome_with_export(resources)
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    resources.inject_failure(
        operation,
        _port_error(source_code, "The staged resource could not be delivered."),
    )
    binding = runtime_bindings_from_job(Job.model_validate(_load("job-diagnose.json")))
    service, repository, _, _, dispatcher, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([_route_to_diagnose_with_export_plan]),
        records,
        catalog=_diagnose_catalog(binding),
        resources=resources,
        guard=guard,
    )

    with pytest.raises(ApplicationPortError) as captured:
        service.submit_outcome(outcome, file_ref)

    assert captured.value.error.code is ErrorCode.RESOURCE_PUBLISH_FAILED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert outcome.outcome_id not in aggregate.outcome_processing_records
    assert resources.discard_calls == []
    assert resources.staged_resource_count == 1
    assert staged.proposal_key == "diagnostic_export"
    assert dispatcher.submit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1


def test_dispatch_failure_after_applied_commit_does_not_hide_outcome_receipt() -> None:
    outcome = _outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(
        outcome.job_id,
        canonical_json_bytes(outcome),
    )
    binding = runtime_bindings_from_job(Job.model_validate(_load("job-diagnose.json")))
    dispatcher = _RaisingDispatcher()
    service, repository, _, guard, _, _, _ = _service(
        _running_state(),
        ScriptedCoordinator([_route_to_diagnose_plan]),
        records,
        catalog=_diagnose_catalog(binding),
        dispatcher=dispatcher,
    )

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert aggregate.outcome_processing_records[outcome.outcome_id].disposition is OutcomeDisposition.APPLIED
    assert aggregate.case.active_job_id is not None
    assert aggregate.jobs[aggregate.case.active_job_id].status is JobStatus.PENDING
    assert dispatcher.submit_calls == [aggregate.case.active_job_id]
    assert guard.acquire_calls == guard.release_calls == 1
