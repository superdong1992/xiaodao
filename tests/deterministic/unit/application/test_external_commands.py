from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from problem_locator.application.external_commands import ExternalCommandHandler
from problem_locator.contracts import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationPortError,
    Attachment,
    AttachmentRequirementConstraints,
    AttachmentStatus,
    CancelCase,
    Case,
    CaseAggregate,
    CaseStatus,
    CreateCase,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    DecisionAuditV2,
    ErrorCode,
    ExecutionFileRef,
    InputRequirementConstraints,
    Job,
    JobOutcome,
    JobSpec,
    JobStatus,
    JobType,
    MAX_ATTACHMENT_BYTES,
    MAX_CASE_RESOURCE_BYTES,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    OutcomeResultType,
    PendingRequirement,
    PrepareAttachment,
    RequirementFulfillment,
    RequirementKind,
    RequirementStatus,
    ResumeCase,
    RuntimeBindings,
    StateFile,
    SubmitSupplement,
    TransitionPlan,
    TriggerType,
    canonical_json_bytes,
    canonical_json_sha256,
)
from problem_locator.journey import configure_journey
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeAssetCatalog,
    FakeClock,
    InMemoryPublicationCommitGuard,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
    InMemoryStateChangeNotifier,
    InMemoryStateRepository,
    PureContextSnapshotProjector,
    RecordingDispatcher,
    ScriptedCoordinator,
)
from tests.deterministic.contracts.scenario_fakes import assets_for_bindings


ROOT = Path(__file__).resolve().parents[4]
CASE_ID = "00000000-0000-0000-0000-000000000001"
NEW_CASE_ID = "00000000-0000-0000-0000-000000000101"
TRIGGER_ID = "00000000-0000-0000-0000-000000000102"
NEW_JOB_ID = "00000000-0000-0000-0000-000000000103"
FACT_ID = "00000000-0000-0000-0000-000000000104"
SECOND_FACT_ID = "00000000-0000-0000-0000-000000000106"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000105"
SECOND_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000107"
SOURCE_ATTACHMENT_ID = "00000000-0000-0000-0000-000000000109"
SOURCE_JOB_ID = "00000000-0000-0000-0000-000000000111"
WAIT_OUTCOME_ID = "00000000-0000-0000-0000-000000000121"
REQUIREMENT_ID = "00000000-0000-0000-0000-000000000131"
SECOND_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000132"
NOW = "2026-07-31T01:02:03.000Z"


@pytest.fixture
def journey_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_journey(stream=stream)
    yield stream
    configure_journey()


def _state() -> StateFile:
    return StateFile.model_validate(
        json.loads(
            (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
                encoding="utf-8"
            )
        )
    )


def _job(name: str) -> Job:
    return Job.model_validate_json(
        (ROOT / f"tests/fixtures/contracts/positive/{name}").read_text(
            encoding="utf-8"
        )
    )


def _bindings(job: Job) -> RuntimeBindings:
    return RuntimeBindings(
        diagnosis_mode=job.diagnosis_mode,
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


def _empty_delta(**updates: object) -> DiagnosisStateDelta:
    values = {
        "problem_spec_patch": None,
        "add_user_facts": [],
        "proposed_facts": [],
        "add_active_hypotheses": [],
        "update_hypotheses": [],
        "reject_hypotheses": [],
        "add_open_questions": [],
        "resolve_questions": [],
        "add_pending_requirements": [],
        "fulfill_requirements": [],
        "add_evidence_bindings": [],
    }
    values.update(updates)
    return DiagnosisStateDelta.model_validate(values)


def _decision_audit(job: Job) -> DecisionAuditV2:
    """Build the minimal server-owned V2 audit needed by waiting fixtures."""

    assert job.skill_ref is not None
    rule_id = "missing_requirement_policy"
    return DecisionAuditV2.model_validate(
        {
            "schema_version": 2,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "skill_ref": job.skill_ref.model_dump(mode="json"),
            "source_draft_sha256": "1" * 64,
            "subject_hash": "2" * 64,
            "candidate_target": None,
            "diagnosis_audit_hash": None,
            "required_rule_ids": [rule_id],
            "required_evidence_bindings": [],
            "rules": [
                {
                    "rule_id": rule_id,
                    "agent_claim": {
                        "rule_id": rule_id,
                        "claimed_result": "PASS",
                        "fact_refs": [],
                        "citations": [],
                        "explanation": "The declared requirement is still missing.",
                    },
                    "server_evaluation": {
                        "rule_id": rule_id,
                        "rule_kind": "SEMANTIC_CAUSALITY",
                        "status": "SEMANTIC_ONLY",
                        "fact_refs": [],
                        "evidence_bindings": [],
                        "anchor_id": None,
                        "derived_anchor_time": None,
                        "observed_times": [],
                        "line_ranges": [],
                        "issues": [],
                    },
                }
            ],
        }
    )


def _job_spec(
    template: Job,
    *,
    target_revision: int,
    previous_outcome_refs: list[str] | None = None,
    replacement_for_job_id: str | None = None,
) -> JobSpec:
    return JobSpec(
        job_type=template.job_type,
        diagnosis_mode=template.diagnosis_mode,
        generic_skill_name=template.generic_skill_name,
        generic_problem_text=template.generic_problem_text,
        goal=template.goal,
        target_state_revision=target_revision,
        evidence_bindings=[],
        attachment_refs=[],
        previous_outcome_refs=previous_outcome_refs or [],
        artifact_bindings=[],
        agent_profile_ref=template.agent_profile_ref,
        available_skill_refs=template.available_skill_refs,
        skill_ref=template.skill_ref,
        tool_bundle_ref=template.tool_bundle_ref,
        context_policy_ref=template.context_policy_ref,
        output_contract_ref=template.output_contract_ref,
        logparse_tool_ref=template.logparse_tool_ref,
        logparse_product=template.logparse_product,
        review_target_binding=None,
        replacement_for_job_id=replacement_for_job_id,
        resource_limits=template.resource_limits,
    )


def _plan(
    *,
    target_status: CaseStatus,
    delta: DiagnosisStateDelta | None = None,
    next_job_spec: JobSpec | None = None,
    job_updates: list[object] | None = None,
    clear_active_job: bool = False,
) -> TransitionPlan:
    return TransitionPlan(
        accepted_state_delta=delta or _empty_delta(),
        target_case_status=target_status,
        job_updates=job_updates or [],
        outcome_disposition=None,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=next_job_spec,
        final_result_target=None,
        clear_active_job=clear_active_job,
        reason="Apply the validated external command.",
    )


def _command_error(code: ErrorCode, message: str) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=message,
        details=[],
        retryable=False,
    )


class _ImmediateTimeoutNotifier:
    def __init__(self) -> None:
        self.notify_calls: list[tuple[str, int]] = []
        self.wait_calls: list[tuple[str, int, float]] = []

    def notify(self, case_id: str, generation: int) -> None:
        self.notify_calls.append((case_id, generation))

    def wait_for_change(
        self,
        case_id: str,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        self.wait_calls.append((case_id, after_generation, timeout_seconds))
        return False


class _InjectReadFaultOnWaitNotifier(_ImmediateTimeoutNotifier):
    def __init__(
        self,
        repository: InMemoryStateRepository,
        failure: ApplicationPortError,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._failure = failure

    def wait_for_change(
        self,
        case_id: str,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        self.wait_calls.append((case_id, after_generation, timeout_seconds))
        self._repository.inject_read_failure("read_snapshot", self._failure)
        return False


class _LeaseCheckingExecutionRecords(InMemoryExecutionRecordStore):
    def __init__(self) -> None:
        super().__init__()
        self.guard: InMemoryPublicationCommitGuard | None = None
        self.lease_observations: list[bool] = []

    def publish_job(self, job: Job) -> ExecutionFileRef:
        held = self.guard is not None and self.guard.held_by_current_thread()
        self.lease_observations.append(held)
        assert held, "job.json publication must share the commit lease"
        return super().publish_job(job)


class _ReadFailsAfterCommitRepository(InMemoryStateRepository):
    def __init__(
        self,
        state: StateFile,
        error_code: ErrorCode = ErrorCode.STATE_CORRUPT,
    ) -> None:
        super().__init__(state)
        self.fail_next_read = False
        self.error_code = error_code

    def read_snapshot(self) -> StateFile:
        if self.fail_next_read:
            self.fail_next_read = False
            raise ApplicationPortError(
                _command_error(
                    self.error_code,
                    "injected post-commit state failure",
                )
            )
        return super().read_snapshot()

    def commit(self, expected_generation, expected_case_revision, mutation):
        receipt = super().commit(
            expected_generation,
            expected_case_revision,
            mutation,
        )
        self.fail_next_read = True
        return receipt


class _RotatingDiagnoseCatalog(FakeAssetCatalog):
    def __init__(self, bindings: list[RuntimeBindings]) -> None:
        super().__init__()
        self._scripted_bindings = iter(bindings)

    def diagnose_bindings(self, skill_ref):
        self.diagnose_calls.append(skill_ref.model_copy(deep=True))
        return next(self._scripted_bindings).model_copy(deep=True)


def _handler(
    state: StateFile,
    coordinator: ScriptedCoordinator,
    *,
    ids: DeterministicIdGenerator | None = None,
    clock: FakeClock | None = None,
    catalog: FakeAssetCatalog | None = None,
    notifier: object | None = None,
    execution_records: InMemoryExecutionRecordStore | None = None,
    repository: InMemoryStateRepository | None = None,
):
    repository = repository or InMemoryStateRepository(state)
    guard = InMemoryPublicationCommitGuard()
    resources = InMemoryResourceStore(publication_guard=guard)
    dispatcher = RecordingDispatcher()
    notifier = notifier or InMemoryStateChangeNotifier()
    execution_records = execution_records or InMemoryExecutionRecordStore()
    clock = clock or FakeClock(NOW)
    ids = ids or DeterministicIdGenerator()
    catalog = catalog or FakeAssetCatalog(route=_bindings(_job("job-route.json")))
    handler = ExternalCommandHandler(
        repository=repository,
        coordinator=coordinator,
        projector=PureContextSnapshotProjector(),
        publication_guard=guard,
        resource_store=resources,
        execution_records=execution_records,
        asset_catalog=catalog,
        dispatcher=dispatcher,
        notifier=notifier,
        clock=clock,
        ids=ids,
    )
    return handler, repository, guard, resources, dispatcher, notifier, clock, ids


def _create_command(
    *,
    statement: str = "RPC timeout",
    wait_seconds: int = 0,
) -> CreateCase:
    return CreateCase(
        idempotency_key="create-1",
        raw_problem_text=statement,
        problem_spec={
            "statement": statement,
            "expected_behavior": "RPC succeeds",
            "actual_behavior": "RPC times out",
            "scope": "payment to inventory",
            "goals": ["Locate the cause"],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["Cause has evidence"],
        },
        initial_user_facts=[{"name": "region", "value": "us-east"}],
        wait_seconds=wait_seconds,
    )


def _expect_port_error(callback, code: ErrorCode) -> ApplicationPortError:
    try:
        callback()
    except ApplicationPortError as error:
        assert error.error.code is code
        return error
    raise AssertionError(f"expected ApplicationPortError({code.value})")


def test_create_case_commits_then_notifies_dispatches_and_replays(
    journey_stream: io.StringIO,
) -> None:
    route = _job("job-route.json")

    def create_plan(snapshot, trigger):
        assert snapshot.case.status is CaseStatus.NEW
        assert trigger.trigger_type is TriggerType.CREATE_CASE
        return _plan(
            target_status=CaseStatus.RUNNING,
            next_job_spec=_job_spec(route, target_revision=1),
        )

    state = _state().model_copy(update={"generation": 0, "cases": {}})
    ids = DeterministicIdGenerator(
        scripted_ids={
            "case": [NEW_CASE_ID],
            "trigger": [TRIGGER_ID],
            "job": [NEW_JOB_ID],
            "diagnosis_item": [FACT_ID],
        }
    )
    coordinator = ScriptedCoordinator([create_plan])
    execution_records = _LeaseCheckingExecutionRecords()
    handler, repository, guard, _, dispatcher, notifier, clock, ids = _handler(
        state,
        coordinator,
        ids=ids,
        execution_records=execution_records,
    )
    execution_records.guard = guard

    first = handler.execute(_create_command())
    second = handler.execute(_create_command(wait_seconds=0))

    journey_events = [
        json.loads(line) for line in journey_stream.getvalue().splitlines()
    ]
    assert [event["event"] for event in journey_events] == [
        "case.created",
        "job.pending_persisted",
        "job.queued",
    ]
    assert all(event["case_id"] == NEW_CASE_ID for event in journey_events)
    assert all(event["job_id"] == NEW_JOB_ID for event in journey_events)
    assert all(event["job_type"] == "ROUTE" for event in journey_events)
    assert journey_events[0]["data"]["problem_spec"]["statement"] == "RPC timeout"

    stored = repository.read_snapshot()
    aggregate = stored.cases[NEW_CASE_ID]
    assert first.business_receipt == second.business_receipt
    assert first.business_receipt.case_revision == 1
    assert aggregate.case.status is CaseStatus.RUNNING
    assert aggregate.case.case_revision == 1
    assert aggregate.case.diagnosis_state.revision == 1
    assert aggregate.case.diagnosis_state.user_facts[0].item_id == FACT_ID
    assert aggregate.case.active_job_id == NEW_JOB_ID
    assert aggregate.jobs[NEW_JOB_ID].status is JobStatus.PENDING
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(NEW_CASE_ID, 1)]
    assert dispatcher.submit_calls == [NEW_JOB_ID, NEW_JOB_ID]
    assert clock.calls == 1
    assert ids.new_calls == ["case", "trigger", "job", "diagnosis_item"]
    assert len(coordinator.calls) == 1
    assert [job.job_id for job in execution_records.publish_job_calls] == [
        NEW_JOB_ID
    ]
    assert execution_records.lease_observations == [True]

    _expect_port_error(
        lambda: handler.execute(_create_command(statement="Different target")),
        ErrorCode.IDEMPOTENCY_CONFLICT,
    )
    assert len(repository.commit_calls) == 1


def test_create_job_publication_errors_propagate_without_state_commit() -> None:
    route = _job("job-route.json")

    def create_plan(snapshot, trigger):
        return _plan(
            target_status=CaseStatus.RUNNING,
            next_job_spec=_job_spec(route, target_revision=1),
        )

    for code in {
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.EXECUTION_RECORD_FAILED,
    }:
        records = _LeaseCheckingExecutionRecords()
        records.inject_failure(
            "publish_job",
            ApplicationPortError(
                _command_error(code, "The Job execution record was rejected.")
            ),
        )
        ids = DeterministicIdGenerator(
            scripted_ids={
                "case": [NEW_CASE_ID],
                "trigger": [TRIGGER_ID],
                "job": [NEW_JOB_ID],
                "diagnosis_item": [FACT_ID],
            }
        )
        handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
            _state().model_copy(update={"generation": 0, "cases": {}}),
            ScriptedCoordinator([create_plan]),
            ids=ids,
            execution_records=records,
        )
        records.guard = guard

        _expect_port_error(lambda: handler.execute(_create_command()), code)

        stored = repository.read_snapshot()
        assert stored.generation == 0
        assert stored.cases == {}
        assert repository.commit_calls == []
        assert guard.acquire_calls == guard.release_calls == 1
        assert records.lease_observations == [True]
        assert notifier.notify_calls == []
        assert dispatcher.submit_calls == []


def test_precommit_state_read_faults_propagate_without_touching_dependencies() -> None:
    for code in {
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
    }:
        repository = InMemoryStateRepository(
            _state().model_copy(update={"generation": 0, "cases": {}})
        )
        failure = ApplicationPortError(
            _command_error(code, "The frozen state could not be read.")
        )
        repository.inject_read_failure("read_snapshot", failure)
        coordinator = ScriptedCoordinator()
        catalog = FakeAssetCatalog(route=_bindings(_job("job-route.json")))
        handler, _, guard, _, dispatcher, notifier, clock, ids = _handler(
            _state().model_copy(update={"generation": 0, "cases": {}}),
            coordinator,
            repository=repository,
            catalog=catalog,
        )

        captured = _expect_port_error(
            lambda: handler.execute(_create_command()),
            code,
        )

        assert captured is failure
        assert repository.commit_calls == []
        assert coordinator.calls == []
        assert catalog.route_calls == 0
        assert guard.acquire_calls == 0
        assert dispatcher.submit_calls == []
        assert notifier.notify_calls == []
        assert clock.calls == 0
        assert ids.new_calls == []


def test_create_rejects_trigger_illegal_coordinator_error_without_commit() -> None:
    coordinator = ScriptedCoordinator(
        [
            _command_error(
                ErrorCode.NEW_CASE_REQUIRED,
                "This code is not legal for CREATE_CASE.",
            )
        ]
    )
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        _state().model_copy(update={"generation": 0, "cases": {}}),
        coordinator,
        execution_records=execution_records,
    )

    _expect_port_error(
        lambda: handler.execute(_create_command()),
        ErrorCode.VALIDATION_ERROR,
    )

    assert len(coordinator.calls) == 1
    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_create_rejects_wrong_coordinator_union_type_without_commit() -> None:
    coordinator = ScriptedCoordinator([object()])
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        _state().model_copy(update={"generation": 0, "cases": {}}),
        coordinator,
        execution_records=execution_records,
    )

    _expect_port_error(
        lambda: handler.execute(_create_command()),
        ErrorCode.VALIDATION_ERROR,
    )

    assert len(coordinator.calls) == 1
    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_create_rejects_coordinator_runtime_binding_substitution() -> None:
    route = _job("job-route.json")
    replacement_profile = route.agent_profile_ref.model_copy(
        update={"version": "9.9.9", "content_hash": "f" * 64}
    )

    def substituted_plan(snapshot, trigger):
        assert trigger.runtime_bindings_by_job_type[JobType.ROUTE] == _bindings(
            route
        )
        return _plan(
            target_status=CaseStatus.RUNNING,
            next_job_spec=_job_spec(route, target_revision=1).model_copy(
                update={"agent_profile_ref": replacement_profile}
            ),
        )

    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        _state().model_copy(update={"generation": 0, "cases": {}}),
        ScriptedCoordinator([substituted_plan]),
        execution_records=execution_records,
    )

    _expect_port_error(
        lambda: handler.execute(_create_command()),
        ErrorCode.VALIDATION_ERROR,
    )

    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_create_catalog_config_fault_is_typed_and_commits_nothing() -> None:
    catalog = FakeAssetCatalog()
    coordinator = ScriptedCoordinator()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        _state().model_copy(update={"generation": 0, "cases": {}}),
        coordinator,
        catalog=catalog,
    )

    error = _expect_port_error(
        lambda: handler.execute(_create_command()),
        ErrorCode.CONFIG_INVALID,
    )

    assert error.error.retryable is False
    assert catalog.route_calls == 1
    assert catalog.route_user_fact_name_calls == [("region",)]
    assert coordinator.calls == []
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_prepare_attachment_classifies_limits_and_only_bumps_case_revision() -> None:
    coordinator = ScriptedCoordinator()
    ids = DeterministicIdGenerator(
        scripted_ids={"attachment": [ATTACHMENT_ID]}
    )
    handler, repository, guard, resources, dispatcher, notifier, clock, _ = _handler(
        _state(),
        coordinator,
        ids=ids,
    )
    command = PrepareAttachment(
        idempotency_key="prepare-1",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=5,
        declared_sha256=(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        ),
    )

    response = handler.execute(command)

    stored = repository.read_snapshot().cases[CASE_ID]
    assert response.business_receipt.primary_resource_id == ATTACHMENT_ID
    assert response.business_receipt.case_revision == 2
    assert stored.case.case_revision == 2
    assert stored.case.diagnosis_state.revision == 1
    assert stored.attachments[ATTACHMENT_ID].status is AttachmentStatus.UPLOADING
    assert len(resources.capacity_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert dispatcher.submit_calls == []
    assert coordinator.calls == []
    assert clock.calls == 1

    too_large = command.model_copy(
        update={
            "idempotency_key": "prepare-too-large",
            "expected_case_revision": 2,
            "declared_size": MAX_ATTACHMENT_BYTES + 1,
        }
    )
    _expect_port_error(
        lambda: handler.execute(too_large),
        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
    )
    assert len(repository.commit_calls) == 1


def test_post_commit_state_read_faults_preserve_external_business_receipt() -> None:
    for code in {
        ErrorCode.STATE_CORRUPT,
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
    }:
        repository = _ReadFailsAfterCommitRepository(_state(), code)
        handler, _, guard, _, _, notifier, _, _ = _handler(
            _state(),
            ScriptedCoordinator(),
            ids=DeterministicIdGenerator(
                scripted_ids={"attachment": [ATTACHMENT_ID]}
            ),
            repository=repository,
        )
        command = PrepareAttachment(
            idempotency_key=f"prepare-post-commit-{code.value}",
            case_id=CASE_ID,
            expected_case_revision=1,
            name="server.log",
            content_type="text/plain",
            declared_size=5,
            declared_sha256=(
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            ),
        )

        response = handler.execute(command)

        assert response.business_receipt.primary_resource_id == ATTACHMENT_ID
        assert response.business_receipt.case_revision == 2
        assert response.case_view is None
        assert response.wait_timed_out is False
        assert repository.fail_next_read is False
        assert guard.acquire_calls == guard.release_calls == 1
        assert notifier.notify_calls == [(CASE_ID, 2)]
        assert repository.read_snapshot().cases[CASE_ID].case.case_revision == 2


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_post_commit_read_fault_does_not_report_a_wait_timeout(
    code: ErrorCode,
) -> None:
    route = _job("job-route.json")
    state = _state().model_copy(update={"generation": 0, "cases": {}})
    repository = _ReadFailsAfterCommitRepository(state, code)
    handler, _, guard, _, dispatcher, notifier, _, _ = _handler(
        state,
        ScriptedCoordinator(
            [
                lambda snapshot, trigger: _plan(
                    target_status=CaseStatus.RUNNING,
                    next_job_spec=_job_spec(route, target_revision=1),
                )
            ]
        ),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "case": [NEW_CASE_ID],
                "trigger": [TRIGGER_ID],
                "job": [NEW_JOB_ID],
                "diagnosis_item": [FACT_ID],
            }
        ),
        repository=repository,
    )

    response = handler.execute(_create_command(wait_seconds=5))

    assert response.business_receipt.case_id == NEW_CASE_ID
    assert response.case_view is None
    assert response.wait_timed_out is False
    assert response.dispatch_pending is False
    assert guard.acquire_calls == guard.release_calls == 1
    assert dispatcher.submit_calls == [NEW_JOB_ID]
    assert notifier.notify_calls == [(NEW_CASE_ID, 1)]


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_replay_wait_read_fault_preserves_the_saved_business_receipt(
    code: ErrorCode,
) -> None:
    state = _state()
    active_job_id = state.cases[CASE_ID].case.active_job_id
    assert active_job_id is not None
    repository = InMemoryStateRepository(state)
    failure = ApplicationPortError(
        _command_error(code, "The replay projection could not read state.")
    )
    notifier = _InjectReadFaultOnWaitNotifier(repository, failure)
    handler, _, guard, _, dispatcher, _, clock, _ = _handler(
        state,
        ScriptedCoordinator(),
        repository=repository,
        notifier=notifier,
    )
    command = ResumeCase(
        idempotency_key=f"resume-replay-{code.value}",
        case_id=CASE_ID,
        expected_case_revision=1,
        wait_seconds=0,
    )

    first = handler.execute(command)
    replay = handler.execute(command.model_copy(update={"wait_seconds": 5}))

    assert replay.business_receipt == first.business_receipt
    assert replay.case_view is None
    assert replay.wait_timed_out is False
    assert replay.dispatch_pending is False
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.wait_calls == [(CASE_ID, 2, 5.0)]
    assert dispatcher.submit_calls == [active_job_id, active_job_id]
    assert clock.calls == 1


def test_prepare_attachment_recomputes_at_most_three_conflicting_commits() -> None:
    ids = DeterministicIdGenerator(
        scripted_ids={"attachment": [ATTACHMENT_ID]}
    )
    handler, repository, guard, resources, _, notifier, clock, ids = _handler(
        _state(),
        ScriptedCoordinator(),
        ids=ids,
    )
    conflict = lambda: ApplicationPortError(
        ApplicationError(
            code=ErrorCode.REVISION_CONFLICT,
            message="The state generation changed before commit.",
            details=[],
            retryable=True,
        )
    )
    repository.fail_next_commit(conflict())
    repository.fail_next_commit(conflict())
    command = PrepareAttachment(
        idempotency_key="prepare-retry",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=5,
        declared_sha256=(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        ),
    )

    response = handler.execute(command)

    assert response.business_receipt.primary_resource_id == ATTACHMENT_ID
    assert guard.acquire_calls == guard.release_calls == 3
    assert len(resources.capacity_calls) == 3
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert clock.calls == 1
    assert ids.new_calls == ["attachment"]

    failing_handler, failing_repository, failing_guard, _, _, _, _, _ = _handler(
        _state(),
        ScriptedCoordinator(),
        ids=DeterministicIdGenerator(
            scripted_ids={"attachment": [ATTACHMENT_ID]}
        ),
    )
    failing_repository.fail_next_commit(conflict())
    failing_repository.fail_next_commit(conflict())
    failing_repository.fail_next_commit(conflict())

    _expect_port_error(
        lambda: failing_handler.execute(command),
        ErrorCode.REVISION_CONFLICT,
    )
    assert failing_guard.acquire_calls == failing_guard.release_calls == 3
    assert failing_repository.read_snapshot().generation == 1


def test_prepare_attachment_maps_capacity_path_error_and_releases_lease() -> None:
    handler, repository, guard, resources, dispatcher, notifier, _, _ = _handler(
        _state(),
        ScriptedCoordinator(),
        ids=DeterministicIdGenerator(
            scripted_ids={"attachment": [ATTACHMENT_ID]}
        ),
    )
    resources.inject_failure(
        "validate_case_capacity",
        ApplicationPortError(
            _command_error(
                ErrorCode.PATH_VIOLATION,
                "The capacity target escaped the Case resource root.",
            )
        ),
    )
    command = PrepareAttachment(
        idempotency_key="prepare-path-failure",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=5,
        declared_sha256=(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        ),
    )

    error = _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.RESOURCE_PUBLISH_FAILED,
    )

    assert error.error.retryable is True
    assert repository.commit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_prepare_attachment_preserves_r3_capacity_observation_details() -> None:
    handler, repository, guard, resources, dispatcher, notifier, _, _ = _handler(
        _state(),
        ScriptedCoordinator(),
        ids=DeterministicIdGenerator(
            scripted_ids={"attachment": [ATTACHMENT_ID]}
        ),
    )
    detail = ApplicationErrorDetail(
        field="case_resource_bytes",
        resource_type="CASE",
        resource_id=CASE_ID,
        resource_ref=None,
        expected=None,
        actual=None,
        limit=MAX_CASE_RESOURCE_BYTES,
        observed=MAX_CASE_RESOURCE_BYTES + 17,
    )
    failure = ApplicationPortError(
        ApplicationError(
            code=ErrorCode.RESOURCE_LIMIT_EXCEEDED,
            message="The Case resource capacity is exceeded.",
            details=[detail],
            retryable=False,
        )
    )
    resources.inject_failure("validate_case_capacity", failure)
    command = PrepareAttachment(
        idempotency_key="prepare-capacity-observed",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="server.log",
        content_type="text/plain",
        declared_size=5,
        declared_sha256=(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        ),
    )

    captured = _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
    )

    assert captured is failure
    assert captured.error.details == [detail]
    assert repository.commit_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def _waiting_state(requirement_name: str = "rpc_method") -> StateFile:
    base = _state()
    original_case = base.cases[CASE_ID].case
    diagnose_template = _job("job-diagnose.json")
    projector = PureContextSnapshotProjector()
    selected_skill = diagnose_template.skill_ref
    assert selected_skill is not None
    source_job = Job(
        job_id=SOURCE_JOB_ID,
        case_id=CASE_ID,
        job_type=JobType.DIAGNOSE,
        diagnosis_mode=diagnose_template.diagnosis_mode,
        generic_skill_name=diagnose_template.generic_skill_name,
        generic_problem_text=diagnose_template.generic_problem_text,
        status=JobStatus.SUCCEEDED,
        goal="Collect a missing RPC method.",
        base_state_revision=1,
        context_snapshot=projector.project(original_case.diagnosis_state),
        evidence_refs=[],
        attachment_refs=[],
        previous_outcome_refs=[],
        artifact_refs=[],
        agent_profile_ref=diagnose_template.agent_profile_ref,
        available_skill_refs=diagnose_template.available_skill_refs,
        skill_ref=selected_skill,
        tool_bundle_ref=diagnose_template.tool_bundle_ref,
        context_policy_ref=diagnose_template.context_policy_ref,
        output_contract_ref=diagnose_template.output_contract_ref,
        logparse_tool_ref=diagnose_template.logparse_tool_ref,
        logparse_product=diagnose_template.logparse_product,
        review_target=None,
        replacement_for_job_id=None,
        resource_limits=diagnose_template.resource_limits,
        created_at="2026-07-31T00:00:30.000Z",
        started_at="2026-07-31T00:00:40.000Z",
        finished_at="2026-07-31T00:01:00.000Z",
        runtime_epoch="00000000-0000-0000-0000-000000000190",
    )
    requirement = PendingRequirement(
        requirement_id=REQUIREMENT_ID,
        kind=RequirementKind.INPUT,
        name=requirement_name,
        prompt="Provide the requested value.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=128,
            pattern=None,
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=SOURCE_JOB_ID,
        fulfilled_by_refs=[],
    )
    outcome = JobOutcome(
        outcome_id=WAIT_OUTCOME_ID,
        job_id=SOURCE_JOB_ID,
        case_id=CASE_ID,
        job_type=JobType.DIAGNOSE,
        base_state_revision=1,
        result_type=OutcomeResultType.NEED_INPUT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(add_pending_requirements=[requirement]),
            requested_input=[REQUIREMENT_ID],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Wait for the requested input.",
        ),
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        error=None,
        produced_at="2026-07-31T00:01:00.000Z",
        decision_audit=_decision_audit(source_job),
    )
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_ref = ExecutionFileRef(
        relative_key=f"jobs/{SOURCE_JOB_ID}/job_outcome.json",
        size=len(outcome_bytes),
        sha256=canonical_json_sha256(outcome),
    )
    processing = OutcomeProcessingRecord(
        outcome_id=WAIT_OUTCOME_ID,
        job_id=SOURCE_JOB_ID,
        outcome_hash=outcome_ref.sha256,
        outcome_file_ref=outcome_ref,
        disposition=OutcomeDisposition.APPLIED,
        processed_at="2026-07-31T00:01:01.000Z",
        error_code=None,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="Waiting input was applied.",
    )
    diagnosis_payload = original_case.diagnosis_state.model_dump(mode="python")
    diagnosis_payload.update(revision=2, pending_requirements=[requirement])
    waiting_case_payload = original_case.model_dump(mode="python")
    waiting_case_payload.update(
        status=CaseStatus.WAITING_INPUT,
        case_revision=2,
        diagnosis_state=diagnosis_payload,
        active_job_id=None,
        selected_skill_ref=selected_skill,
        updated_at="2026-07-31T00:01:01.000Z",
    )
    waiting_case = Case.model_validate(waiting_case_payload)
    aggregate = CaseAggregate(
        case=waiting_case,
        jobs={SOURCE_JOB_ID: source_job},
        outcomes={WAIT_OUTCOME_ID: outcome},
        outcome_processing_records={WAIT_OUTCOME_ID: processing},
        execution_failure_records={},
        attachments={},
        evidence={},
        artifacts={},
    )
    return base.model_copy(
        update={"generation": 2, "cases": {CASE_ID: aggregate}}
    )


def _attachment_record(
    attachment_id: str,
    *,
    status: AttachmentStatus = AttachmentStatus.READY,
) -> Attachment:
    ready = status is AttachmentStatus.READY
    return Attachment(
        attachment_id=attachment_id,
        case_id=CASE_ID,
        status=status,
        name=f"{attachment_id}.log",
        content_type="text/plain",
        declared_size=16,
        declared_sha256="a" * 64,
        size=16 if ready else None,
        sha256="a" * 64 if ready else None,
        storage_key=(
            f"resources/cases/{CASE_ID}/attachments/{attachment_id}/payload"
            if ready
            else None
        ),
        created_at="2026-07-31T00:00:10.000Z",
        updated_at="2026-07-31T00:00:20.000Z",
    )


def _waiting_attachment_state(
    attachments: list[Attachment],
    *,
    source_attachment_refs: list[str] | None = None,
) -> StateFile:
    base = _waiting_state()
    aggregate = base.cases[CASE_ID]
    source_job = aggregate.jobs[SOURCE_JOB_ID].model_copy(
        update={
            "goal": "Collect the requested logs.",
            "attachment_refs": list(source_attachment_refs or []),
        }
    )
    input_requirement = aggregate.case.diagnosis_state.pending_requirements[0]
    attachment_requirement = PendingRequirement(
        requirement_id=SECOND_REQUIREMENT_ID,
        kind=RequirementKind.ATTACHMENT,
        name="server_logs",
        prompt="Provide the server logs.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=["text/plain"],
            min_count=1,
            max_count=3,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=SOURCE_JOB_ID,
        fulfilled_by_refs=[],
    )
    outcome = JobOutcome(
        outcome_id=WAIT_OUTCOME_ID,
        job_id=SOURCE_JOB_ID,
        case_id=CASE_ID,
        job_type=JobType.DIAGNOSE,
        base_state_revision=1,
        result_type=OutcomeResultType.NEED_ATTACHMENT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(
                add_pending_requirements=[attachment_requirement]
            ),
            requested_input=[],
            requested_attachments=[SECOND_REQUIREMENT_ID],
            candidate_conclusion_draft=None,
            recommended_next_step="Wait for the requested logs.",
        ),
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        error=None,
        produced_at="2026-07-31T00:01:00.000Z",
        decision_audit=_decision_audit(source_job),
    )
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_ref = ExecutionFileRef(
        relative_key=f"jobs/{SOURCE_JOB_ID}/job_outcome.json",
        size=len(outcome_bytes),
        sha256=canonical_json_sha256(outcome),
    )
    processing = OutcomeProcessingRecord(
        outcome_id=WAIT_OUTCOME_ID,
        job_id=SOURCE_JOB_ID,
        outcome_hash=outcome_ref.sha256,
        outcome_file_ref=outcome_ref,
        disposition=OutcomeDisposition.APPLIED,
        processed_at="2026-07-31T00:01:01.000Z",
        error_code=None,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="Waiting Attachment was applied.",
    )
    diagnosis_payload = aggregate.case.diagnosis_state.model_dump(mode="python")
    diagnosis_payload.update(
        pending_requirements=[attachment_requirement, input_requirement]
    )
    case_payload = aggregate.case.model_dump(mode="python")
    case_payload.update(
        status=CaseStatus.WAITING_ATTACHMENT,
        diagnosis_state=diagnosis_payload,
    )
    waiting_case = Case.model_validate(case_payload)
    waiting_aggregate = CaseAggregate(
        case=waiting_case,
        jobs={SOURCE_JOB_ID: source_job},
        outcomes={WAIT_OUTCOME_ID: outcome},
        outcome_processing_records={WAIT_OUTCOME_ID: processing},
        execution_failure_records={},
        attachments={item.attachment_id: item for item in attachments},
        evidence={},
        artifacts={},
    )
    return base.model_copy(update={"cases": {CASE_ID: waiting_aggregate}})


def test_submit_supplement_catalog_binding_faults_are_typed_and_commit_nothing() -> None:
    waiting = _waiting_state()
    command = SubmitSupplement(
        idempotency_key="supplement-catalog-failure",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"rpc_method": "ReserveStock"},
        attachment_ids=[],
        wait_seconds=0,
    )

    for code in {
        ErrorCode.ASSET_VERSION_UNAVAILABLE,
        ErrorCode.CONFIG_INVALID,
    }:
        catalog = FakeAssetCatalog()
        if code is ErrorCode.CONFIG_INVALID:
            catalog.inject_failure(
                "diagnose_bindings",
                ApplicationPortError(
                    _command_error(
                        code,
                        "The diagnosis runtime bindings are invalid.",
                    )
                ),
            )
        coordinator = ScriptedCoordinator()
        execution_records = InMemoryExecutionRecordStore()
        handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
            waiting,
            coordinator,
            ids=DeterministicIdGenerator(
                scripted_ids={
                    "trigger": [TRIGGER_ID],
                    "job": [NEW_JOB_ID],
                    "diagnosis_item": [FACT_ID],
                }
            ),
            catalog=catalog,
            execution_records=execution_records,
        )

        error = _expect_port_error(lambda: handler.execute(command), code)

        assert error.error.retryable is False
        assert coordinator.calls == []
        assert repository.commit_calls == []
        assert execution_records.publish_job_calls == []
        assert guard.acquire_calls == 0
        assert dispatcher.submit_calls == []
        assert notifier.notify_calls == []


def test_submit_supplement_rejects_catalog_binding_for_another_skill() -> None:
    waiting = _waiting_state()
    aggregate = waiting.cases[CASE_ID]
    source = aggregate.jobs[SOURCE_JOB_ID]
    selected = aggregate.case.selected_skill_ref
    assert selected is not None
    wrong_skill = selected.model_copy(
        update={"version": "9.9.9", "content_hash": "f" * 64}
    )
    wrong_bindings = _bindings(source).model_copy(
        deep=True,
        update={"skill_ref": wrong_skill},
    )
    catalog = FakeAssetCatalog(
        diagnose={
            (selected.id, selected.version, selected.content_hash): wrong_bindings
        }
    )
    coordinator = ScriptedCoordinator()
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        coordinator,
        catalog=catalog,
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-wrong-skill-binding",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"rpc_method": "ReserveStock"},
        attachment_ids=[],
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.CONFIG_INVALID,
    )

    assert len(catalog.diagnose_calls) == 1
    assert coordinator.calls == []
    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_submit_supplement_validates_then_commits_one_diagnose_job() -> None:
    waiting = _waiting_state()
    source = waiting.cases[CASE_ID].jobs[SOURCE_JOB_ID]

    def supplement_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.SUBMIT_SUPPLEMENT
        assert trigger.payload.stable_target_changed is False
        fact = trigger.payload.user_facts[0]
        delta = _empty_delta(
            add_user_facts=[fact],
            fulfill_requirements=[
                RequirementFulfillment(
                    requirement_id=REQUIREMENT_ID,
                    fulfilled_by_refs=[fact.item_id],
                )
            ],
        )
        return _plan(
            target_status=CaseStatus.RUNNING,
            delta=delta,
            next_job_spec=_job_spec(
                source,
                target_revision=3,
                previous_outcome_refs=[WAIT_OUTCOME_ID],
            ),
        )

    selected = waiting.cases[CASE_ID].case.selected_skill_ref
    assert selected is not None
    catalog = FakeAssetCatalog(
        diagnose={
            (selected.id, selected.version, selected.content_hash): _bindings(source)
        }
    )
    ids = DeterministicIdGenerator(
        scripted_ids={
            "trigger": [TRIGGER_ID],
            "job": [NEW_JOB_ID],
            "diagnosis_item": [FACT_ID],
        }
    )
    coordinator = ScriptedCoordinator([supplement_plan])
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        coordinator,
        ids=ids,
        catalog=catalog,
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-1",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"rpc_method": "ReserveStock"},
        attachment_ids=[],
        wait_seconds=0,
    )

    response = handler.execute(command)

    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert response.business_receipt.job_id == NEW_JOB_ID
    assert aggregate.case.status is CaseStatus.RUNNING
    assert aggregate.case.case_revision == 3
    assert aggregate.case.diagnosis_state.revision == 3
    assert aggregate.case.diagnosis_state.user_facts[-1].item_id == FACT_ID
    assert (
        aggregate.case.diagnosis_state.pending_requirements[0].status
        is RequirementStatus.FULFILLED
    )
    assert aggregate.jobs[NEW_JOB_ID].previous_outcome_refs == [WAIT_OUTCOME_ID]
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 3)]
    assert dispatcher.submit_calls == [NEW_JOB_ID]
    assert [job.job_id for job in execution_records.publish_job_calls] == [
        NEW_JOB_ID
    ]


def test_submit_supplement_sends_current_ready_attachments_in_canonical_continuation() -> None:
    source_attachment = _attachment_record(SOURCE_ATTACHMENT_ID)
    current_first = _attachment_record(SECOND_ATTACHMENT_ID)
    current_second = _attachment_record(ATTACHMENT_ID)
    waiting = _waiting_attachment_state(
        [source_attachment, current_first, current_second],
        source_attachment_refs=[SOURCE_ATTACHMENT_ID],
    )
    source = waiting.cases[CASE_ID].jobs[SOURCE_JOB_ID]
    submitted_ids = [
        SECOND_ATTACHMENT_ID,
        SOURCE_ATTACHMENT_ID,
        ATTACHMENT_ID,
    ]

    def supplement_plan(snapshot, trigger):
        assert snapshot.case.status is CaseStatus.WAITING_ATTACHMENT
        assert trigger.trigger_type is TriggerType.SUBMIT_SUPPLEMENT
        assert trigger.payload.user_facts == []
        assert trigger.payload.ready_attachment_ids == submitted_ids
        assert trigger.continuation_resources.attachment_refs == [
            SOURCE_ATTACHMENT_ID,
            SECOND_ATTACHMENT_ID,
            ATTACHMENT_ID,
        ]
        assert trigger.continuation_resources.previous_outcome_refs == [
            WAIT_OUTCOME_ID
        ]
        return _plan(
            target_status=CaseStatus.WAITING_INPUT,
            delta=_empty_delta(
                fulfill_requirements=[
                    RequirementFulfillment(
                        requirement_id=SECOND_REQUIREMENT_ID,
                        fulfilled_by_refs=submitted_ids,
                    )
                ]
            ),
        )

    selected = waiting.cases[CASE_ID].case.selected_skill_ref
    assert selected is not None
    catalog = FakeAssetCatalog(
        diagnose={
            (selected.id, selected.version, selected.content_hash): _bindings(source)
        }
    )
    coordinator = ScriptedCoordinator([supplement_plan])
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        coordinator,
        ids=DeterministicIdGenerator(
            scripted_ids={"trigger": [TRIGGER_ID], "job": [NEW_JOB_ID]}
        ),
        catalog=catalog,
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-ready-attachments",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={},
        attachment_ids=submitted_ids,
        wait_seconds=0,
    )

    response = handler.execute(command)

    stored = repository.read_snapshot().cases[CASE_ID]
    attachment_requirement = stored.case.diagnosis_state.pending_requirements[0]
    assert response.business_receipt.job_id is None
    assert response.business_receipt.status == CaseStatus.WAITING_INPUT.value
    assert attachment_requirement.status is RequirementStatus.FULFILLED
    assert attachment_requirement.fulfilled_by_refs == submitted_ids
    assert len(coordinator.calls) == 1
    captured_trigger = coordinator.calls[0][1]
    assert captured_trigger.payload.ready_attachment_ids == submitted_ids
    assert captured_trigger.continuation_resources.attachment_refs == [
        SOURCE_ATTACHMENT_ID,
        SECOND_ATTACHMENT_ID,
        ATTACHMENT_ID,
    ]
    assert len(catalog.diagnose_calls) == 1
    assert len(repository.commit_calls) == 1
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == [(CASE_ID, 3)]


def test_submit_supplement_rejects_nonready_attachment_before_any_dependency_call() -> None:
    uploading = _attachment_record(
        ATTACHMENT_ID,
        status=AttachmentStatus.UPLOADING,
    )
    waiting = _waiting_attachment_state([uploading])
    coordinator = ScriptedCoordinator()
    catalog = FakeAssetCatalog()
    execution_records = InMemoryExecutionRecordStore()
    ids = DeterministicIdGenerator()
    clock = FakeClock(NOW)
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        coordinator,
        ids=ids,
        clock=clock,
        catalog=catalog,
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-nonready-attachment",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={},
        attachment_ids=[ATTACHMENT_ID],
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.ATTACHMENT_NOT_READY,
    )

    assert coordinator.calls == []
    assert catalog.diagnose_calls == []
    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []
    assert clock.calls == 0
    assert ids.new_calls == []


def test_submit_supplement_rejects_coordinator_binding_substitution() -> None:
    waiting = _waiting_state()
    aggregate = waiting.cases[CASE_ID]
    source = aggregate.jobs[SOURCE_JOB_ID]
    selected = aggregate.case.selected_skill_ref
    assert selected is not None
    replacement_profile = source.agent_profile_ref.model_copy(
        update={"version": "9.9.9", "content_hash": "f" * 64}
    )

    def substituted_plan(snapshot, trigger):
        fact = trigger.payload.user_facts[0]
        return _plan(
            target_status=CaseStatus.RUNNING,
            delta=_empty_delta(
                add_user_facts=[fact],
                fulfill_requirements=[
                    RequirementFulfillment(
                        requirement_id=REQUIREMENT_ID,
                        fulfilled_by_refs=[fact.item_id],
                    )
                ],
            ),
            next_job_spec=_job_spec(
                source,
                target_revision=3,
                previous_outcome_refs=[WAIT_OUTCOME_ID],
            ).model_copy(update={"agent_profile_ref": replacement_profile}),
        )

    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        ScriptedCoordinator([substituted_plan]),
        catalog=FakeAssetCatalog(
            diagnose={
                (selected.id, selected.version, selected.content_hash): _bindings(
                    source
                )
            }
        ),
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-substituted-binding",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"rpc_method": "ReserveStock"},
        attachment_ids=[],
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.VALIDATION_ERROR,
    )

    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_submit_supplement_rejects_swapped_input_fulfillments_without_commit() -> None:
    waiting = _waiting_state()
    aggregate = waiting.cases[CASE_ID]
    source = aggregate.jobs[SOURCE_JOB_ID]
    first_requirement = aggregate.case.diagnosis_state.pending_requirements[0]
    second_requirement = first_requirement.model_copy(
        deep=True,
        update={
            "requirement_id": SECOND_REQUIREMENT_ID,
            "name": "scope",
            "prompt": "Provide the affected scope.",
        },
    )
    diagnosis_state = aggregate.case.diagnosis_state.model_copy(
        deep=True,
        update={
            "pending_requirements": [first_requirement, second_requirement]
        },
    )
    waiting_case = aggregate.case.model_copy(
        deep=True,
        update={"diagnosis_state": diagnosis_state},
    )
    waiting = waiting.model_copy(
        deep=True,
        update={
            "cases": {
                CASE_ID: aggregate.model_copy(
                    deep=True,
                    update={"case": waiting_case},
                )
            }
        },
    )

    def swapped_plan(snapshot, trigger):
        facts_by_name = {
            fact.provenance.input_name: fact
            for fact in trigger.payload.user_facts
        }
        return _plan(
            target_status=CaseStatus.RUNNING,
            delta=_empty_delta(
                add_user_facts=trigger.payload.user_facts,
                fulfill_requirements=[
                    RequirementFulfillment(
                        requirement_id=REQUIREMENT_ID,
                        fulfilled_by_refs=[facts_by_name["scope"].item_id],
                    ),
                    RequirementFulfillment(
                        requirement_id=SECOND_REQUIREMENT_ID,
                        fulfilled_by_refs=[
                            facts_by_name["rpc_method"].item_id
                        ],
                    ),
                ],
            ),
            next_job_spec=_job_spec(
                source,
                target_revision=3,
                previous_outcome_refs=[WAIT_OUTCOME_ID],
            ),
        )

    selected = waiting.cases[CASE_ID].case.selected_skill_ref
    assert selected is not None
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        ScriptedCoordinator([swapped_plan]),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "trigger": [TRIGGER_ID],
                "job": [NEW_JOB_ID],
                "diagnosis_item": [FACT_ID, SECOND_FACT_ID],
            }
        ),
        catalog=FakeAssetCatalog(
            diagnose={
                (selected.id, selected.version, selected.content_hash): _bindings(
                    source
                )
            }
        ),
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-swapped-inputs",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={
            "rpc_method": "ReserveStock",
            "scope": "inventory service",
        },
        attachment_ids=[],
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.VALIDATION_ERROR,
    )

    assert repository.commit_calls == []
    assert guard.acquire_calls == guard.release_calls == 0
    assert execution_records.publish_job_calls == []
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_submit_supplement_accepts_canonical_fact_order_for_multiple_inputs() -> None:
    waiting = _waiting_state()
    aggregate = waiting.cases[CASE_ID]
    source = aggregate.jobs[SOURCE_JOB_ID]
    first_requirement = aggregate.case.diagnosis_state.pending_requirements[0]
    second_requirement = first_requirement.model_copy(
        deep=True,
        update={
            "requirement_id": SECOND_REQUIREMENT_ID,
            "name": "caller_service",
            "prompt": "Provide the calling service.",
        },
    )
    diagnosis_state = aggregate.case.diagnosis_state.model_copy(
        deep=True,
        update={
            "pending_requirements": [first_requirement, second_requirement]
        },
    )
    waiting_case = aggregate.case.model_copy(
        deep=True,
        update={"diagnosis_state": diagnosis_state},
    )
    waiting = waiting.model_copy(
        deep=True,
        update={
            "cases": {
                CASE_ID: aggregate.model_copy(
                    deep=True,
                    update={"case": waiting_case},
                )
            }
        },
    )

    def canonical_plan(snapshot, trigger):
        assert trigger.payload.stable_target_changed is False
        facts = sorted(trigger.payload.user_facts, key=lambda item: item.item_id)
        facts_by_name = {
            fact.provenance.input_name: fact for fact in trigger.payload.user_facts
        }
        return _plan(
            target_status=CaseStatus.RUNNING,
            delta=_empty_delta(
                add_user_facts=facts,
                fulfill_requirements=[
                    RequirementFulfillment(
                        requirement_id=REQUIREMENT_ID,
                        fulfilled_by_refs=[facts_by_name["rpc_method"].item_id],
                    ),
                    RequirementFulfillment(
                        requirement_id=SECOND_REQUIREMENT_ID,
                        fulfilled_by_refs=[facts_by_name["caller_service"].item_id],
                    ),
                ],
            ),
            next_job_spec=_job_spec(
                source,
                target_revision=3,
                previous_outcome_refs=[WAIT_OUTCOME_ID],
            ),
        )

    selected = aggregate.case.selected_skill_ref
    assert selected is not None
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        ScriptedCoordinator([canonical_plan]),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "trigger": [TRIGGER_ID],
                "job": [NEW_JOB_ID],
                # Input names sort as caller_service, rpc_method. Reverse their IDs so
                # the Coordinator's canonical item_id ordering is observably different.
                "diagnosis_item": [SECOND_FACT_ID, FACT_ID],
            }
        ),
        catalog=FakeAssetCatalog(
            diagnose={
                (selected.id, selected.version, selected.content_hash): _bindings(source)
            }
        ),
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="supplement-canonical-multiple-inputs",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={
            "caller_service": "checkout-synthetic",
            "rpc_method": "ReserveStock",
        },
        attachment_ids=[],
        wait_seconds=0,
    )

    response = handler.execute(command)

    stored = repository.read_snapshot().cases[CASE_ID]
    assert response.business_receipt.job_id == NEW_JOB_ID
    assert stored.case.status is CaseStatus.RUNNING
    assert [
        fact.item_id for fact in stored.case.diagnosis_state.user_facts[-2:]
    ] == [FACT_ID, SECOND_FACT_ID]
    assert guard.acquire_calls == guard.release_calls == 1
    assert dispatcher.submit_calls == [NEW_JOB_ID]
    assert notifier.notify_calls == [(CASE_ID, 3)]


def test_submit_supplement_reuses_first_bindings_across_three_commit_attempts() -> None:
    waiting = _waiting_state()
    source = waiting.cases[CASE_ID].jobs[SOURCE_JOB_ID]
    first_bindings = _bindings(source)
    second_bindings = first_bindings.model_copy(
        deep=True,
        update={
            "agent_profile_ref": first_bindings.agent_profile_ref.model_copy(
                update={
                    "version": "9.9.9",
                    "content_hash": "f" * 64,
                }
            )
        },
    )
    catalog = _RotatingDiagnoseCatalog(
        [first_bindings, second_bindings, second_bindings]
    )

    def supplement_plan(snapshot, trigger):
        fact = trigger.payload.user_facts[0]
        bindings = trigger.runtime_bindings_by_job_type[JobType.DIAGNOSE]
        next_job_spec = _job_spec(
            source,
            target_revision=3,
            previous_outcome_refs=[WAIT_OUTCOME_ID],
        ).model_copy(
            update={
                "agent_profile_ref": bindings.agent_profile_ref,
                "available_skill_refs": bindings.available_skill_refs,
                "skill_ref": bindings.skill_ref,
                "tool_bundle_ref": bindings.tool_bundle_ref,
                "context_policy_ref": bindings.context_policy_ref,
                "output_contract_ref": bindings.output_contract_ref,
                "logparse_tool_ref": bindings.logparse_tool_ref,
                "logparse_product": bindings.logparse_product,
                "resource_limits": bindings.resource_limits,
            }
        )
        return _plan(
            target_status=CaseStatus.RUNNING,
            delta=_empty_delta(
                add_user_facts=[fact],
                fulfill_requirements=[
                    RequirementFulfillment(
                        requirement_id=REQUIREMENT_ID,
                        fulfilled_by_refs=[fact.item_id],
                    )
                ],
            ),
            next_job_spec=next_job_spec,
        )

    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, _, _, clock, ids = _handler(
        waiting,
        ScriptedCoordinator(
            [supplement_plan, supplement_plan, supplement_plan]
        ),
        ids=DeterministicIdGenerator(
            scripted_ids={
                "trigger": [TRIGGER_ID],
                "job": [NEW_JOB_ID],
                "diagnosis_item": [FACT_ID],
            }
        ),
        catalog=catalog,
        execution_records=execution_records,
    )

    def conflict() -> ApplicationPortError:
        return ApplicationPortError(
            ApplicationError(
                code=ErrorCode.REVISION_CONFLICT,
                message="The state generation changed before commit.",
                details=[],
                retryable=True,
            )
        )

    repository.fail_next_commit(conflict())
    repository.fail_next_commit(conflict())
    command = SubmitSupplement(
        idempotency_key="supplement-binding-retry",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"rpc_method": "ReserveStock"},
        attachment_ids=[],
        wait_seconds=0,
    )

    response = handler.execute(command)

    assert response.business_receipt.job_id == NEW_JOB_ID
    assert len(catalog.diagnose_calls) == 1
    assert len(execution_records.publish_job_calls) == 3
    assert all(
        job == execution_records.publish_job_calls[0]
        for job in execution_records.publish_job_calls
    )
    assert (
        execution_records.publish_job_calls[0].agent_profile_ref
        == first_bindings.agent_profile_ref
    )
    assert guard.acquire_calls == guard.release_calls == 3
    assert clock.calls == 1
    assert ids.new_calls == ["trigger", "job", "diagnosis_item"]


def test_stable_target_change_returns_coordinator_error_without_commit() -> None:
    waiting = _waiting_state(requirement_name="scope")
    selected = waiting.cases[CASE_ID].case.selected_skill_ref
    source = waiting.cases[CASE_ID].jobs[SOURCE_JOB_ID]
    assert selected is not None

    def reject_new_target(snapshot, trigger):
        assert trigger.payload.stable_target_changed is True
        return _command_error(
            ErrorCode.NEW_CASE_REQUIRED,
            "The supplement changes the stable diagnosis target.",
        )

    coordinator = ScriptedCoordinator([reject_new_target])
    execution_records = InMemoryExecutionRecordStore()
    catalog = FakeAssetCatalog(
        diagnose={
            (selected.id, selected.version, selected.content_hash): _bindings(source)
        }
    )
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        waiting,
        coordinator,
        catalog=catalog,
        execution_records=execution_records,
    )
    command = SubmitSupplement(
        idempotency_key="new-target-1",
        case_id=CASE_ID,
        expected_case_revision=2,
        inputs={"scope": "a different service boundary"},
        attachment_ids=[],
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.NEW_CASE_REQUIRED,
    )
    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []
    assert execution_records.publish_job_calls == []


def test_pending_resume_persists_receipt_without_revision_change_and_resubmits() -> None:
    notifier = _ImmediateTimeoutNotifier()
    handler, repository, guard, _, dispatcher, notifier, clock, _ = _handler(
        _state(),
        ScriptedCoordinator(),
        notifier=notifier,
    )
    command = ResumeCase(
        idempotency_key="resume-1",
        case_id=CASE_ID,
        expected_case_revision=1,
        wait_seconds=5,
    )
    dispatcher.reject_next_submit = True

    first = handler.execute(command)
    second = handler.execute(command.model_copy(update={"wait_seconds": 0}))

    stored = repository.read_snapshot()
    route_job_id = stored.cases[CASE_ID].case.active_job_id
    assert first.business_receipt == second.business_receipt
    assert first.dispatch_pending is True
    assert second.dispatch_pending is False
    assert first.wait_timed_out is True
    assert second.wait_timed_out is False
    assert first.business_receipt.case_revision == 1
    assert stored.cases[CASE_ID].case.case_revision == 1
    assert stored.generation == 2
    assert len(repository.commit_calls) == 1
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert notifier.wait_calls == [(CASE_ID, 2, 5.0)]
    assert dispatcher.submit_calls == [route_job_id, route_job_id]
    assert clock.calls == 1


def _interrupted_route_state() -> tuple[StateFile, Job]:
    base = _state()
    aggregate = base.cases[CASE_ID]
    source = next(iter(aggregate.jobs.values()))
    source_payload = source.model_dump(mode="python")
    source_payload.update(
        status=JobStatus.INTERRUPTED,
        finished_at="2026-07-31T00:01:00.000Z",
    )
    interrupted_job = Job.model_validate(source_payload)
    case_payload = aggregate.case.model_dump(mode="python")
    case_payload.update(
        status=CaseStatus.INTERRUPTED,
        case_revision=2,
        active_job_id=None,
        updated_at="2026-07-31T00:01:00.000Z",
    )
    interrupted_case = Case.model_validate(case_payload)
    interrupted_aggregate = CaseAggregate(
        case=interrupted_case,
        jobs={interrupted_job.job_id: interrupted_job},
        outcomes={},
        outcome_processing_records={},
        execution_failure_records={},
        attachments={},
        evidence={},
        artifacts={},
    )
    return base.model_copy(
        update={
            "generation": 2,
            "cases": {CASE_ID: interrupted_aggregate},
        }
    ), interrupted_job


def test_interrupted_resume_reuses_pinned_bindings_and_creates_one_replacement() -> None:
    state, interrupted_job = _interrupted_route_state()

    def resume_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.RESUME_INTERRUPTED
        assert trigger.payload.source_job_id == interrupted_job.job_id
        assert trigger.runtime_bindings_by_job_type[JobType.ROUTE] == _bindings(
            interrupted_job
        )
        return _plan(
            target_status=CaseStatus.RUNNING,
            next_job_spec=_job_spec(
                interrupted_job,
                target_revision=1,
                replacement_for_job_id=interrupted_job.job_id,
            ),
        )

    bindings = _bindings(interrupted_job)
    catalog = FakeAssetCatalog(
        assets=assets_for_bindings(bindings),
        route=bindings,
    )
    ids = DeterministicIdGenerator(
        scripted_ids={
            "trigger": [TRIGGER_ID],
            "job": [NEW_JOB_ID],
        }
    )
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        state,
        ScriptedCoordinator([resume_plan]),
        ids=ids,
        catalog=catalog,
        execution_records=execution_records,
    )
    command = ResumeCase(
        idempotency_key="resume-interrupted-1",
        case_id=CASE_ID,
        expected_case_revision=2,
        wait_seconds=0,
    )

    response = handler.execute(command)

    stored = repository.read_snapshot().cases[CASE_ID]
    replacement = stored.jobs[NEW_JOB_ID]
    assert response.business_receipt.job_id == NEW_JOB_ID
    assert stored.case.status is CaseStatus.RUNNING
    assert stored.case.case_revision == 3
    assert stored.case.diagnosis_state.revision == 1
    assert stored.case.active_job_id == NEW_JOB_ID
    assert replacement.replacement_for_job_id == interrupted_job.job_id
    assert _bindings(replacement) == bindings
    assert stored.jobs[interrupted_job.job_id].status is JobStatus.INTERRUPTED
    assert len(catalog.check_calls) == 1
    assert catalog.resolve_calls == []
    assert catalog.route_calls == 0
    assert catalog.diagnose_calls == []
    assert catalog.review_calls == []
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 3)]
    assert dispatcher.submit_calls == [NEW_JOB_ID]
    assert [job.job_id for job in execution_records.publish_job_calls] == [
        NEW_JOB_ID
    ]


def test_interrupted_resume_rejects_coordinator_binding_substitution() -> None:
    state, interrupted_job = _interrupted_route_state()
    bindings = _bindings(interrupted_job)
    replacement_profile = bindings.agent_profile_ref.model_copy(
        update={"version": "9.9.9", "content_hash": "f" * 64}
    )

    def substituted_plan(snapshot, trigger):
        assert trigger.runtime_bindings_by_job_type[JobType.ROUTE] == bindings
        return _plan(
            target_status=CaseStatus.RUNNING,
            next_job_spec=_job_spec(
                interrupted_job,
                target_revision=1,
                replacement_for_job_id=interrupted_job.job_id,
            ).model_copy(update={"agent_profile_ref": replacement_profile}),
        )

    catalog = FakeAssetCatalog(
        assets=assets_for_bindings(bindings),
        route=bindings,
    )
    execution_records = InMemoryExecutionRecordStore()
    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        state,
        ScriptedCoordinator([substituted_plan]),
        catalog=catalog,
        execution_records=execution_records,
    )
    command = ResumeCase(
        idempotency_key="resume-substituted-binding",
        case_id=CASE_ID,
        expected_case_revision=2,
        wait_seconds=0,
    )

    _expect_port_error(
        lambda: handler.execute(command),
        ErrorCode.VALIDATION_ERROR,
    )

    assert len(catalog.check_calls) == 1
    assert repository.commit_calls == []
    assert execution_records.publish_job_calls == []
    assert guard.acquire_calls == 0
    assert dispatcher.submit_calls == []
    assert notifier.notify_calls == []


def test_cancel_uses_plan_and_signals_only_after_guarded_commit() -> None:
    state = _state()
    active_job = next(iter(state.cases[CASE_ID].jobs.values()))

    def cancel_plan(snapshot, trigger):
        assert trigger.trigger_type is TriggerType.CANCEL_CASE
        return _plan(
            target_status=CaseStatus.CANCELLED,
            job_updates=[
                {
                    "job_id": active_job.job_id,
                    "expected_status": JobStatus.PENDING,
                    "target_status": JobStatus.CANCELLED,
                    "started_at": None,
                    "finished_at": trigger.occurred_at,
                    "runtime_epoch": None,
                }
            ],
            clear_active_job=True,
        )

    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        state,
        ScriptedCoordinator([cancel_plan]),
    )
    command = CancelCase(
        idempotency_key="cancel-1",
        case_id=CASE_ID,
        expected_case_revision=1,
    )

    response = handler.execute(command)

    aggregate = repository.read_snapshot().cases[CASE_ID]
    assert response.business_receipt.status == CaseStatus.CANCELLED.value
    assert aggregate.case.status is CaseStatus.CANCELLED
    assert aggregate.case.case_revision == 2
    assert aggregate.case.diagnosis_state.revision == 1
    assert aggregate.case.active_job_id is None
    assert aggregate.jobs[active_job.job_id].status is JobStatus.CANCELLED
    assert guard.acquire_calls == guard.release_calls == 1
    assert notifier.notify_calls == [(CASE_ID, 2)]
    assert dispatcher.cancel_calls == [active_job.job_id]


def test_cancel_rejects_plan_that_does_not_cancel_the_active_job() -> None:
    state = _state()

    def incomplete_cancel_plan(snapshot, trigger):
        return _plan(
            target_status=CaseStatus.CANCELLED,
            job_updates=[],
            clear_active_job=True,
        )

    handler, repository, guard, _, dispatcher, notifier, _, _ = _handler(
        state,
        ScriptedCoordinator([incomplete_cancel_plan]),
    )
    command = CancelCase(
        idempotency_key="cancel-invalid-plan",
        case_id=CASE_ID,
        expected_case_revision=1,
    )

    _expect_port_error(lambda: handler.execute(command), ErrorCode.VALIDATION_ERROR)

    assert repository.commit_calls == []
    assert guard.acquire_calls == 0
    assert notifier.notify_calls == []
    assert dispatcher.cancel_calls == []
