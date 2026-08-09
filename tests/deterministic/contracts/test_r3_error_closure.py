from __future__ import annotations

import hashlib
import inspect
from types import NoneType
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

from problem_locator.contracts import errors, outcomes, ports
from problem_locator.contracts.enums import (
    ErrorCode,
    FailureReportDisposition,
    OutcomeDisposition,
    ResourceKind,
    ResourceType,
    TriggerType,
)
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES
from problem_locator.contracts.models import (
    ApplicationError,
    ApplicationErrorDetail,
    ApplicationResponse,
    ArtifactListResponse,
    AssetAvailabilityReport,
    BusinessReceipt,
    CaseQueryResponse,
    ClaimReceipt,
    ExecutionFileRef,
    FailureReceipt,
    Job,
    JobOutcome,
    OutcomeReceipt,
    OpenArtifactResult,
    PlannedResourceTarget,
    ReadinessCheck,
    ReadinessReport,
    RecoveryReceipt,
    SubmitSupplementTriggerPayload,
    ValidatedTrigger,
    VersionedRef,
)
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json
from tests.deterministic.contracts.fakes import (
    FakeAssetCatalog,
    InMemoryCancellationSignal,
    InMemoryPublicationCommitGuard,
    InMemoryResourceStore,
    InMemoryStateRepository,
    RecordingApplicationCommand,
    ScriptedRuntime,
    StubApplicationQuery,
    StubJobControl,
    StubStateAdmin,
)


CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000011"
RESOURCE_ID = "00000000-0000-0000-0000-000000000061"


def _error(code: ErrorCode) -> ApplicationError:
    return ApplicationError(
        code=code,
        message=f"Frozen r3 error: {code.value}.",
        details=[],
        retryable=code in errors.APPLICATION_ERROR_RETRYABLE_CODES,
    )


def _port_error(code: ErrorCode) -> Exception:
    return errors.ApplicationPortError(_error(code))


def _job() -> Job:
    return Job.model_validate(load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json"))


def _outcome() -> JobOutcome:
    return JobOutcome.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "job-outcome-diagnosis.json")
    )


def test_r3_exact_port_error_closure_is_not_overwide() -> None:
    state_failures = {ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED}
    mapping = errors.PORT_ERROR_CODES

    expected = {
        "ApplicationCommandPort.execute": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.CASE_NOT_FOUND,
            ErrorCode.ATTACHMENT_NOT_FOUND,
            ErrorCode.INVALID_CASE_STATE,
            ErrorCode.ACTIVE_JOB_EXISTS,
            ErrorCode.NEW_CASE_REQUIRED,
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            ErrorCode.RESOURCE_CASE_MISMATCH,
            ErrorCode.ATTACHMENT_NOT_READY,
            ErrorCode.UPLOAD_INCOMPLETE,
            ErrorCode.RESOURCE_HASH_MISMATCH,
            ErrorCode.RESOURCE_SIZE_MISMATCH,
            ErrorCode.RESOURCE_LIMIT_EXCEEDED,
            ErrorCode.EXECUTION_RECORD_FAILED,
            ErrorCode.STATE_WRITE_FAILED,
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            ErrorCode.CONFIG_INVALID,
            *state_failures,
        },
        "ApplicationQueryPort.get_case": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.CASE_NOT_FOUND,
            ErrorCode.JOB_NOT_FOUND,
            ErrorCode.JOB_CASE_MISMATCH,
            *state_failures,
        },
        "ApplicationQueryPort.list_artifacts": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.CASE_NOT_FOUND,
            *state_failures,
        },
        "ApplicationQueryPort.open_artifact": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.CASE_NOT_FOUND,
            ErrorCode.ARTIFACT_NOT_FOUND,
            ErrorCode.RESOURCE_NOT_FOUND,
            ErrorCode.RESOURCE_HASH_MISMATCH,
            ErrorCode.RESOURCE_SIZE_MISMATCH,
            *state_failures,
        },
        "StateRepository.read_case": {ErrorCode.CASE_NOT_FOUND, *state_failures},
        "StateRepository.read_job": {ErrorCode.JOB_NOT_FOUND, *state_failures},
        "StateRepository.read_artifact": {
            ErrorCode.ARTIFACT_NOT_FOUND,
            *state_failures,
        },
        "StateRepository.read_snapshot": state_failures,
        "AssetCatalogPort.check": set(),
        "AssetCatalogPort.resolve": {ErrorCode.ASSET_VERSION_UNAVAILABLE},
        "AssetCatalogPort.route_bindings": {
            ErrorCode.CONFIG_INVALID,
        },
        "AssetCatalogPort.diagnose_bindings": {
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            ErrorCode.CONFIG_INVALID,
        },
        "AssetCatalogPort.review_bindings": {
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            ErrorCode.CONFIG_INVALID,
        },
        "JobControlPort.claim_job": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.JOB_NOT_FOUND,
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STATE_WRITE_FAILED,
            *state_failures,
        },
        "Runtime.execute": state_failures,
        "JobControlPort.submit_outcome": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.JOB_NOT_FOUND,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            ErrorCode.STATE_WRITE_FAILED,
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.EXECUTION_RECORD_FAILED,
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            ErrorCode.CONFIG_INVALID,
            *state_failures,
        },
        "JobControlPort.report_execution_infrastructure_failure": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.JOB_NOT_FOUND,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STATE_WRITE_FAILED,
            *state_failures,
        },
        "JobControlPort.interrupt_previous_epoch": {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STATE_WRITE_FAILED,
            *state_failures,
        },
    }
    for key, codes in expected.items():
        assert mapping[key] == frozenset(codes), key

    assert ErrorCode.CLAIM_REJECTED not in mapping["JobControlPort.claim_job"]
    assert mapping["StateRepository.validate_all"] == frozenset()
    assert mapping["StateAdminPort.readiness"] == frozenset()
    assert mapping["StateAdminPort.validate_state"] == frozenset()


def test_asset_catalog_is_part_of_typed_port_conformance() -> None:
    expected_returns = {
        "check": AssetAvailabilityReport,
        "resolve": getattr(__import__("problem_locator.contracts.models", fromlist=["ResolvedAsset"]), "ResolvedAsset"),
        "route_bindings": getattr(__import__("problem_locator.contracts.models", fromlist=["RuntimeBindings"]), "RuntimeBindings"),
        "diagnose_bindings": getattr(__import__("problem_locator.contracts.models", fromlist=["RuntimeBindings"]), "RuntimeBindings"),
        "review_bindings": getattr(__import__("problem_locator.contracts.models", fromlist=["RuntimeBindings"]), "RuntimeBindings"),
    }
    for method_name, expected in expected_returns.items():
        method = getattr(ports.AssetCatalogPort, method_name)
        assert get_type_hints(method)["return"] is expected
        assert f"AssetCatalogPort.{method_name}" in errors.PORT_ERROR_CODES


@pytest.mark.parametrize("code", [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED])
@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("read_case", (CASE_ID,)),
        ("read_job", (JOB_ID,)),
        ("read_artifact", (RESOURCE_ID,)),
        ("read_snapshot", ()),
    ],
)
def test_state_repository_read_fakes_propagate_the_exact_typed_fault(
    method_name: str,
    args: tuple[Any, ...],
    code: ErrorCode,
) -> None:
    from problem_locator.contracts.models import StateFile

    state = StateFile.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "state.json")
    )
    repository = InMemoryStateRepository(state)
    failure = _port_error(code)
    repository.inject_read_failure(method_name, failure)

    with pytest.raises(errors.ApplicationPortError) as raised:
        getattr(repository, method_name)(*args)
    assert raised.value is failure


def test_application_and_job_control_fakes_propagate_state_faults_precommit() -> None:
    command_port = RecordingApplicationCommand()
    query_port = StubApplicationQuery()
    job_control = StubJobControl()
    command = getattr(__import__("problem_locator.contracts.models", fromlist=["CreateCase"]), "CreateCase").model_construct()
    job_outcome = _outcome()
    outcome_ref = ExecutionFileRef(
        relative_key=f"jobs/{job_outcome.job_id}/job_outcome.json",
        size=len(canonical_json_bytes(job_outcome)),
        sha256=hashlib.sha256(canonical_json_bytes(job_outcome)).hexdigest(),
    )

    command_failure = _port_error(ErrorCode.STATE_CORRUPT)
    command_port.queue(command_failure)
    with pytest.raises(errors.ApplicationPortError) as command_raised:
        command_port.execute(command)
    assert command_raised.value is command_failure

    for method, args in (
        ("get_case", (CASE_ID, None, 0)),
        ("list_artifacts", (CASE_ID, False)),
        ("open_artifact", (CASE_ID, RESOURCE_ID)),
    ):
        failure = _port_error(ErrorCode.STATE_SCHEMA_UNSUPPORTED)
        query_port.queue(method, failure)
        with pytest.raises(errors.ApplicationPortError) as raised:
            getattr(query_port, method)(*args)
        assert raised.value is failure

    job_calls = (
        ("claim_job", (JOB_ID, RESOURCE_ID)),
        ("submit_outcome", (job_outcome, outcome_ref)),
        (
            "report_execution_infrastructure_failure",
            (JOB_ID, RESOURCE_ID, CASE_ID, getattr(__import__("problem_locator.contracts.models", fromlist=["ExecutionFailure"]), "ExecutionFailure").model_construct()),
        ),
        ("interrupt_previous_epoch", (RESOURCE_ID, CASE_ID)),
    )
    for method, args in job_calls:
        failure = _port_error(ErrorCode.STATE_CORRUPT)
        job_control.queue(method, failure)
        with pytest.raises(errors.ApplicationPortError) as raised:
            getattr(job_control, method)(*args)
        assert raised.value is failure


@pytest.mark.parametrize("invalid", [-1, 31, True, 1.5, "1"])
def test_get_case_rejects_invalid_raw_wait_before_any_query_work(
    invalid: object,
) -> None:
    query = StubApplicationQuery()
    response = CaseQueryResponse.model_construct()
    query.queue("get_case", response)

    with pytest.raises(errors.ApplicationPortError) as raised:
        query.get_case(CASE_ID, None, invalid)  # type: ignore[arg-type]
    assert raised.value.error.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.error.retryable is False
    assert query.calls == []

    assert query.get_case(CASE_ID, None, 0) == response
    assert query.calls == [("get_case", (CASE_ID, None, 0))]


@pytest.mark.parametrize("valid", [0, 30])
def test_get_case_keeps_the_closed_valid_wait_range(valid: int) -> None:
    query = StubApplicationQuery()
    response = CaseQueryResponse.model_construct()
    query.queue("get_case", response)
    assert query.get_case(CASE_ID, None, valid) == response


@pytest.mark.parametrize(
    ("method_name", "invalid_args", "valid_args", "response"),
    [
        (
            "get_case",
            ("not-a-case-id", None, 0),
            (CASE_ID, None, 0),
            CaseQueryResponse.model_construct(),
        ),
        (
            "get_case",
            (CASE_ID, "not-a-job-id", 0),
            (CASE_ID, JOB_ID, 0),
            CaseQueryResponse.model_construct(),
        ),
        (
            "list_artifacts",
            ("not-a-case-id", False),
            (CASE_ID, False),
            ArtifactListResponse.model_construct(),
        ),
        (
            "list_artifacts",
            (CASE_ID, 1),
            (CASE_ID, True),
            ArtifactListResponse.model_construct(),
        ),
        (
            "list_artifacts",
            (CASE_ID, "true"),
            (CASE_ID, False),
            ArtifactListResponse.model_construct(),
        ),
        (
            "open_artifact",
            ("not-a-case-id", RESOURCE_ID),
            (CASE_ID, RESOURCE_ID),
            OpenArtifactResult.model_construct(),
        ),
        (
            "open_artifact",
            (CASE_ID, "not-an-artifact-id"),
            (CASE_ID, RESOURCE_ID),
            OpenArtifactResult.model_construct(),
        ),
    ],
)
def test_query_raw_dto_validation_precedes_recording_and_script_consumption(
    method_name: str,
    invalid_args: tuple[object, ...],
    valid_args: tuple[object, ...],
    response: object,
) -> None:
    query = StubApplicationQuery()
    query.queue(method_name, response)

    with pytest.raises(errors.ApplicationPortError) as raised:
        getattr(query, method_name)(*invalid_args)
    assert raised.value.error.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.error.retryable is False
    assert query.calls == []

    assert getattr(query, method_name)(*valid_args) == response
    assert len(query.calls) == 1


@pytest.mark.parametrize("invalid", [0, 1, "true"])
def test_list_artifacts_dto_itself_keeps_include_internal_strict(
    invalid: object,
) -> None:
    from problem_locator.contracts.models import ListArtifacts

    assert ListArtifacts(case_id=CASE_ID, include_internal=False).include_internal is False
    assert ListArtifacts(case_id=CASE_ID, include_internal=True).include_internal is True
    with pytest.raises(ValidationError):
        ListArtifacts(case_id=CASE_ID, include_internal=invalid)


def _outcome_ref(job_outcome: JobOutcome) -> ExecutionFileRef:
    encoded = canonical_json_bytes(job_outcome)
    return ExecutionFileRef(
        relative_key=f"jobs/{job_outcome.job_id}/job_outcome.json",
        size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


@pytest.mark.parametrize(
    ("invalid_job_id", "invalid_runtime_epoch"),
    [
        ("not-a-job-id", RESOURCE_ID),
        (JOB_ID, "not-a-runtime-epoch"),
        (True, RESOURCE_ID),
    ],
)
def test_claim_raw_validation_precedes_recording_and_script_consumption(
    invalid_job_id: object,
    invalid_runtime_epoch: object,
) -> None:
    control = StubJobControl()
    receipt = ClaimReceipt(
        claimed=False,
        job=None,
        failure_applied=False,
        failure_code=None,
    )
    control.queue("claim_job", receipt)

    with pytest.raises(errors.ApplicationPortError) as raised:
        control.claim_job(invalid_job_id, invalid_runtime_epoch)  # type: ignore[arg-type]
    assert raised.value.error.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.error.retryable is False
    assert control.calls == []

    assert control.claim_job(JOB_ID, RESOURCE_ID) == receipt
    assert len(control.calls) == 1


@pytest.mark.parametrize("invalid_field", ["outcome", "file_ref", "binding"])
def test_submit_outcome_raw_validation_rebuilds_the_existing_command_dto_first(
    invalid_field: str,
) -> None:
    control = StubJobControl()
    outcome = _outcome()
    outcome_ref = _outcome_ref(outcome)
    receipt = OutcomeReceipt(
        disposition=OutcomeDisposition.APPLIED,
        case_view=None,
    )
    control.queue("submit_outcome", receipt)

    invalid_outcome: object = outcome
    invalid_ref: object = outcome_ref
    if invalid_field == "outcome":
        invalid_outcome = "not-an-outcome"
    elif invalid_field == "file_ref":
        invalid_ref = "not-an-execution-file-ref"
    else:
        invalid_ref = outcome_ref.model_copy(update={"sha256": "f" * 64})

    with pytest.raises(errors.ApplicationPortError) as raised:
        control.submit_outcome(invalid_outcome, invalid_ref)  # type: ignore[arg-type]
    assert raised.value.error.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.error.retryable is False
    assert control.calls == []

    assert control.submit_outcome(outcome, outcome_ref) == receipt
    assert len(control.calls) == 1


@pytest.mark.parametrize(
    ("invalid_runtime_epoch", "invalid_recovery_id"),
    [
        ("not-a-runtime-epoch", RESOURCE_ID),
        (RESOURCE_ID, "not-a-recovery-id"),
        (RESOURCE_ID, 1.5),
    ],
)
def test_interrupt_raw_validation_precedes_recording_and_script_consumption(
    invalid_runtime_epoch: object,
    invalid_recovery_id: object,
) -> None:
    control = StubJobControl()
    receipt = RecoveryReceipt(
        recovery_id=RESOURCE_ID,
        interrupted_job_ids=[],
        pending_job_ids=[],
    )
    control.queue("interrupt_previous_epoch", receipt)

    with pytest.raises(errors.ApplicationPortError) as raised:
        control.interrupt_previous_epoch(  # type: ignore[arg-type]
            invalid_runtime_epoch,
            invalid_recovery_id,
        )
    assert raised.value.error.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.error.retryable is False
    assert control.calls == []

    assert control.interrupt_previous_epoch(RESOURCE_ID, RESOURCE_ID) == receipt
    assert len(control.calls) == 1


@pytest.mark.parametrize(
    "code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_runtime_state_read_fault_is_typed_and_does_not_become_an_outcome(
    code: ErrorCode,
) -> None:
    failure = _port_error(code)
    runtime = ScriptedRuntime([failure])
    with pytest.raises(errors.ApplicationPortError) as raised:
        runtime.execute(_job(), InMemoryCancellationSignal())
    assert raised.value is failure
    assert runtime.calls[0][0].job_id == _job().job_id

    runtime.queue(_port_error(ErrorCode.EXECUTION_RECORD_FAILED))
    with pytest.raises(ValueError, match="does not allow"):
        runtime.execute(_job(), InMemoryCancellationSignal())


def test_postcommit_receipts_keep_saved_success_when_projection_reread_fails() -> None:
    business = BusinessReceipt.model_construct(
        operation="CreateCase",
        primary_resource_id=CASE_ID,
        case_id=CASE_ID,
        case_revision=1,
        job_id=JOB_ID,
        status="accepted",
    )
    response = ApplicationResponse(
        business_receipt=business,
        case_view=None,
        wait_timed_out=False,
        dispatch_pending=False,
    )
    assert response.case_view is None

    job_control = StubJobControl()
    outcome = _outcome()
    outcome_ref = ExecutionFileRef(
        relative_key=f"jobs/{outcome.job_id}/job_outcome.json",
        size=len(canonical_json_bytes(outcome)),
        sha256=hashlib.sha256(canonical_json_bytes(outcome)).hexdigest(),
    )
    outcome_receipt = OutcomeReceipt(
        disposition=OutcomeDisposition.APPLIED,
        case_view=None,
    )
    failure_receipt = FailureReceipt(
        failure_id=RESOURCE_ID,
        disposition=FailureReportDisposition.APPLIED,
        case_view=None,
    )
    job_control.queue("submit_outcome", outcome_receipt)
    job_control.queue("report_execution_infrastructure_failure", failure_receipt)
    assert job_control.submit_outcome(outcome, outcome_ref) == outcome_receipt
    assert (
        job_control.report_execution_infrastructure_failure(
            JOB_ID,
            RESOURCE_ID,
            RESOURCE_ID,
            getattr(__import__("problem_locator.contracts.models", fromlist=["ExecutionFailure"]), "ExecutionFailure").model_construct(),
        )
        == failure_receipt
    )

    with pytest.raises(ValidationError):
        CaseQueryResponse(case_view=None, wait_timed_out=False)


def test_readiness_and_validation_keep_their_nonexception_report_channels() -> None:
    state_error = _error(ErrorCode.STATE_CORRUPT)
    report = ReadinessReport(
        ready=False,
        checks=[
            ReadinessCheck(name="CONFIG", passed=True, message=None),
            ReadinessCheck(name="INSTANCE_LOCK", passed=True, message=None),
            ReadinessCheck(name="STATE", passed=False, message="State validation failed."),
            ReadinessCheck(name="DATA_DIRECTORIES", passed=True, message=None),
            ReadinessCheck(name="RECOVERY", passed=True, message=None),
        ],
        error=state_error,
    )
    admin = StubStateAdmin()
    admin.queue("readiness", report)
    assert admin.readiness() == report

    admin.queue("readiness", _port_error(ErrorCode.STATE_CORRUPT))
    with pytest.raises(ValueError, match="does not allow"):
        admin.readiness()


def test_claim_receipt_has_one_normal_rejection_and_one_asset_failure_branch() -> None:
    normal = ClaimReceipt(
        claimed=False,
        job=None,
        failure_applied=False,
        failure_code=None,
    )
    unavailable = ClaimReceipt(
        claimed=False,
        job=None,
        failure_applied=True,
        failure_code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )
    assert normal.failure_applied is False
    assert unavailable.failure_applied is True

    with pytest.raises(ValidationError, match="ASSET_VERSION_UNAVAILABLE"):
        ClaimReceipt(
            claimed=False,
            job=None,
            failure_applied=True,
            failure_code=ErrorCode.CONFIG_INVALID,
        )

    control = StubJobControl()
    control.queue("claim_job", normal)
    assert control.claim_job(JOB_ID, RESOURCE_ID) == normal
    control.queue("claim_job", _port_error(ErrorCode.CLAIM_REJECTED))
    with pytest.raises(ValueError, match="does not allow"):
        control.claim_job(JOB_ID, RESOURCE_ID)


def test_coordinator_error_codes_are_trigger_specific() -> None:
    mapping = getattr(errors, "COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER")
    invalid = ErrorCode.INVALID_CASE_STATE
    active = ErrorCode.ACTIVE_JOB_EXISTS
    new_case = ErrorCode.NEW_CASE_REQUIRED
    validation = ErrorCode.VALIDATION_ERROR
    expected = {
        TriggerType.CREATE_CASE: {invalid, active, validation},
        TriggerType.ROUTE_OUTCOME: {invalid, validation},
        TriggerType.DIAGNOSIS_OUTCOME: {invalid, new_case, validation},
        TriggerType.REVIEW_OUTCOME: {invalid, validation},
        TriggerType.SUBMIT_SUPPLEMENT: {invalid, active, new_case, validation},
        TriggerType.CANCEL_CASE: {invalid, validation},
        TriggerType.RESUME_INTERRUPTED: {invalid, active, validation},
        TriggerType.EXECUTION_FAILED: {invalid, validation},
        TriggerType.ASSET_VERSION_UNAVAILABLE: {invalid, validation},
        TriggerType.MARK_OLD_EPOCH_INTERRUPTED: {invalid, validation},
        TriggerType.STALE_ACTIVE_OUTCOME: {invalid, validation},
    }
    assert mapping == {key: frozenset(value) for key, value in expected.items()}
    assert errors.COORDINATOR_PLAN_ERROR_CODES == frozenset().union(*expected.values())

    for trigger_type, allowed in expected.items():
        payload: object | None = None
        if trigger_type is TriggerType.SUBMIT_SUPPLEMENT:
            payload = SubmitSupplementTriggerPayload.model_construct(
                user_facts=[],
                ready_attachment_ids=[RESOURCE_ID],
                stable_target_changed=False,
            )
        trigger = ValidatedTrigger.model_construct(
            trigger_type=trigger_type,
            payload=payload,
        )
        for code in (invalid, active, new_case, validation):
            if code in allowed:
                assert outcomes.validate_coordinator_plan_result(trigger, _error(code)).code is code
            else:
                with pytest.raises(ValueError, match="Trigger"):
                    outcomes.validate_coordinator_plan_result(trigger, _error(code))


def test_finalized_outcome_semantic_rejection_has_one_deterministic_terminal_path() -> None:
    normalize = getattr(outcomes, "coordinator_outcome_error_failure")
    route = ValidatedTrigger.model_construct(trigger_type=TriggerType.ROUTE_OUTCOME, payload=None)
    diagnosis = ValidatedTrigger.model_construct(
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=None,
    )
    review = ValidatedTrigger.model_construct(trigger_type=TriggerType.REVIEW_OUTCOME, payload=None)

    for trigger, code in (
        (route, ErrorCode.VALIDATION_ERROR),
        (diagnosis, ErrorCode.VALIDATION_ERROR),
        (diagnosis, ErrorCode.NEW_CASE_REQUIRED),
        (review, ErrorCode.VALIDATION_ERROR),
    ):
        failure = normalize(trigger, _error(code))
        assert failure.code is ErrorCode.OUTCOME_INVALID
        assert failure.retryable is False
        assert failure == errors.deterministic_outcome_failure(
            ErrorCode.OUTCOME_INVALID,
            _error(code).details,
        )

    with pytest.raises(ValueError, match="STALE"):
        normalize(diagnosis, _error(ErrorCode.INVALID_CASE_STATE))
    supplement = ValidatedTrigger.model_construct(
        trigger_type=TriggerType.SUBMIT_SUPPLEMENT,
        payload=SubmitSupplementTriggerPayload.model_construct(
            user_facts=[],
            ready_attachment_ids=[RESOURCE_ID],
            stable_target_changed=False,
        ),
    )
    with pytest.raises(ValueError, match="finalized Outcome"):
        normalize(supplement, _error(ErrorCode.VALIDATION_ERROR))


def test_application_and_execution_retryability_are_separate() -> None:
    assert ErrorCode.EXECUTION_RECORD_FAILED not in errors.APPLICATION_ERROR_RETRYABLE_CODES
    assert ErrorCode.EXECUTION_RECORD_FAILED in errors.EXECUTION_FAILURE_RETRYABLE_CODES
    with pytest.raises(ValidationError, match="ApplicationError code is not retryable"):
        ApplicationError(
            code=ErrorCode.EXECUTION_RECORD_FAILED,
            message="Execution record persistence failed.",
            details=[],
            retryable=True,
        )

    method_retry = getattr(errors, "JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES")
    assert method_retry == frozenset(
        {
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STATE_WRITE_FAILED,
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            ErrorCode.EXECUTION_RECORD_FAILED,
        }
    )
    assert ErrorCode.ASSET_VERSION_UNAVAILABLE not in method_retry
    assert ErrorCode.CONFIG_INVALID not in method_retry
    assert method_retry <= errors.PORT_ERROR_CODES["JobControlPort.submit_outcome"]

    parked = getattr(errors, "JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES")
    assert parked == frozenset(
        {
            ErrorCode.ASSET_VERSION_UNAVAILABLE,
            ErrorCode.CONFIG_INVALID,
        }
    )
    assert method_retry.isdisjoint(parked)
    assert parked <= errors.PORT_ERROR_CODES["JobControlPort.submit_outcome"]


def test_deterministic_failure_detail_sort_is_a_complete_order() -> None:
    first = ApplicationErrorDetail(
        field="same",
        resource_type="JOB",
        resource_id=JOB_ID,
        resource_ref=None,
        expected="a",
        actual="z",
        limit=None,
        observed=None,
    )
    second = first.model_copy(update={"expected": "z", "actual": "a"})
    forward = errors.deterministic_outcome_failure(
        ErrorCode.OUTCOME_INVALID,
        [first, second],
    )
    reverse = errors.deterministic_outcome_failure(
        ErrorCode.OUTCOME_INVALID,
        [second, first],
    )
    assert canonical_json_bytes(forward) == canonical_json_bytes(reverse)
    assert forward.details == reverse.details


def _target(name: str, size: int, digest: str) -> PlannedResourceTarget:
    return PlannedResourceTarget(
        case_id=CASE_ID,
        resource_type=ResourceType.ARTIFACT,
        resource_id=name,
        resource_kind=ResourceKind.FILE,
        size=size,
        sha256=digest,
        final_storage_key=f"resources/cases/{CASE_ID}/artifacts/{name}/payload",
    )


def test_capacity_error_observed_includes_outbox_orphan_and_atomic_new_bytes() -> None:
    guard = InMemoryPublicationCommitGuard()
    store = InMemoryResourceStore(publication_guard=guard)
    outbox = _target(
        "00000000-0000-0000-0000-000000000071",
        MAX_CASE_RESOURCE_BYTES - 20,
        "a" * 64,
    )
    orphan = _target(
        "00000000-0000-0000-0000-000000000072",
        12,
        "b" * 64,
    )
    planned = _target(
        "00000000-0000-0000-0000-000000000073",
        9,
        "c" * 64,
    )
    store.seed_formal_resource(outbox, outbox_reference_count=1)
    store.seed_formal_resource(orphan, ordinary_orphan=True)

    lease = guard.acquire()
    try:
        with pytest.raises(errors.ApplicationPortError) as raised:
            store.validate_case_capacity(CASE_ID, [planned])
    finally:
        lease.release()

    application_error = raised.value.error
    assert application_error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert application_error.retryable is False
    assert application_error.details == [
        ApplicationErrorDetail(
            field="case_resource_bytes",
            resource_type="CASE",
            resource_id=CASE_ID,
            resource_ref=None,
            expected=None,
            actual=None,
            limit=MAX_CASE_RESOURCE_BYTES,
            observed=MAX_CASE_RESOURCE_BYTES + 1,
        )
    ]
    failure = errors.deterministic_outcome_failure(
        application_error.code,
        application_error.details,
    )
    assert failure.details == application_error.details
    assert store.publish_calls == []


def test_asset_catalog_negative_and_exception_channels_are_disjoint() -> None:
    old_ref = VersionedRef(id="diagnosis-skill/rpc-timeout", version="2.0.0", content_hash="a" * 64)
    catalog = FakeAssetCatalog()
    report = catalog.check([old_ref])
    assert report == AssetAvailabilityReport(available=False, missing_refs=[old_ref])

    with pytest.raises(errors.ApplicationPortError) as missing:
        catalog.resolve(old_ref)
    assert missing.value.error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE

    for method, args, natural_code in (
        ("route_bindings", (), ErrorCode.CONFIG_INVALID),
        ("diagnose_bindings", (old_ref,), ErrorCode.ASSET_VERSION_UNAVAILABLE),
        ("review_bindings", (old_ref,), ErrorCode.ASSET_VERSION_UNAVAILABLE),
    ):
        with pytest.raises(errors.ApplicationPortError) as raised:
            getattr(catalog, method)(*args)
        assert raised.value.error.code is natural_code

        scripted = _port_error(ErrorCode.CONFIG_INVALID)
        catalog.inject_failure(method, scripted)
        with pytest.raises(errors.ApplicationPortError) as injected:
            getattr(catalog, method)(*args)
        assert injected.value is scripted

    with pytest.raises(ValueError, match="report channel"):
        catalog.inject_failure("check", _port_error(ErrorCode.CONFIG_INVALID))


def test_port_protocol_inventory_includes_asset_catalog_without_changing_success_types() -> None:
    protocols = (
        ports.ApplicationCommandPort,
        ports.ApplicationQueryPort,
        ports.StateAdminPort,
        ports.StateRepository,
        ports.ResourceStore,
        ports.ExecutionRecordStore,
        ports.AssetCatalogPort,
        ports.Runtime,
        ports.JobControlPort,
    )
    expected_keys = {
        f"{protocol.__name__}.{name}"
        for protocol in protocols
        for name, value in protocol.__dict__.items()
        if inspect.isfunction(value) and name != "__init__"
    }
    assert set(errors.PORT_ERROR_CODES) == expected_keys
    assert get_type_hints(ports.AssetCatalogPort.check)["return"] is AssetAvailabilityReport
    assert get_type_hints(ports.StateRepository.validate_all)["return"].__name__ == "ValidationReport"
    assert get_type_hints(ports.ResourceStore.validate_staged)["return"] is NoneType
