from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from problem_locator.application.outcome_processing import (
    OutcomeActivity,
    classify_outcome_activity,
    validate_published_outcome,
)
from problem_locator.application.projection import (
    build_case_snapshot,
    empty_continuation_resources,
)
from problem_locator.contracts import (
    DETERMINISTIC_OUTCOME_FAILURE_SPECS,
    ERROR_SPECS,
    JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES,
    JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES,
    ApplicationError,
    ErrorCode,
    ExecutionFailedTriggerPayload,
    ExecutionFailure,
    ExecutionFileRef,
    ExecutionStage,
    FixtureManifest,
    Job,
    JobOutcome,
    JobStatus,
    OldEpochTriggerPayload,
    StateFile,
    TriggerType,
    ValidatedTrigger,
    canonical_json_bytes,
    deterministic_outcome_failure,
    parse_canonical_json_bytes,
)
from problem_locator.domain import DomainCoordinator
from tests.deterministic.contracts._support import schema_validator


ROOT = Path(__file__).resolve().parents[3]
FAILURE_FIXTURES = ROOT / "tests/fixtures/failures"
MATRIX_PATH = FAILURE_FIXTURES / "failure-matrix.json"
CONTRACT_FIXTURES = ROOT / "tests/fixtures/contracts/positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
TRIGGER_ID = "00000000-0000-0000-0000-000000000090"
FIXED_TIME = "2026-07-31T00:03:00.000Z"
STARTED_AT = "2026-07-31T00:00:10.000Z"
DEFAULT_RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000091"


ModelT = TypeVar("ModelT", bound=BaseModel)


_SCENARIO_KEYS = {
    "application_submission_retry": {
        "category",
        "error_code",
        "expected_application_retryable",
        "failure_points",
        "id",
        "mapping",
    },
    "deterministic_outcome_failure": {
        "category",
        "error_code",
        "expected_case_status",
        "expected_job_status",
        "expected_message",
        "expected_retryable",
        "expected_stage",
        "id",
        "mapping",
    },
    "execution_failure_transition": {
        "category",
        "error_code",
        "expected_case_status",
        "expected_job_status",
        "id",
        "mapping",
        "message",
        "retryable",
        "stage",
    },
    "invalid_outcome_activity": {
        "category",
        "expected_activity",
        "expected_case_status",
        "expected_error_code",
        "expected_job_status",
        "expected_message",
        "expected_retryable",
        "expected_stage",
        "id",
        "mapping",
    },
    "late_outcome_activity": {
        "category",
        "expected_activity",
        "id",
        "mapping",
    },
    "missing_published_outcome": {
        "category",
        "expected_case_status",
        "expected_error_code",
        "expected_job_status",
        "expected_message",
        "expected_retryable",
        "expected_stage",
        "id",
        "mapping",
    },
    "old_epoch_transition": {
        "category",
        "current_runtime_epoch",
        "expected_case_status",
        "expected_job_status",
        "id",
        "mapping",
        "previous_runtime_epoch",
    },
}
_REQUIRED_CATEGORIES = {
    "cancel_race",
    "context_limit",
    "hash_mismatch",
    "invalid_job_binding",
    "old_epoch",
    "output_missing",
    "replace_after",
    "replace_before",
}


def _fixture(model_type: type[ModelT], name: str) -> ModelT:
    return parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / name).read_bytes(),
        model_type=model_type,
    )


def _rebuild(model: ModelT, **updates: object) -> ModelT:
    value = model.model_dump(mode="python")
    value.update(updates)
    return type(model).model_validate(value)


def _running_state(runtime_epoch: str = DEFAULT_RUNTIME_EPOCH) -> StateFile:
    state = _fixture(StateFile, "state.json")
    aggregate = state.cases[CASE_ID]
    running_job = _rebuild(
        aggregate.jobs[ROUTE_JOB_ID],
        status=JobStatus.RUNNING,
        started_at=STARTED_AT,
        finished_at=None,
        runtime_epoch=runtime_epoch,
    )
    rebuilt_aggregate = _rebuild(
        aggregate,
        jobs={ROUTE_JOB_ID: running_job},
    )
    return _rebuild(
        state,
        cases={CASE_ID: rebuilt_aggregate},
        updated_at=STARTED_AT,
    )


def _cancelled_state() -> StateFile:
    state = _running_state()
    aggregate = state.cases[CASE_ID]
    cancelled_job = _rebuild(
        aggregate.jobs[ROUTE_JOB_ID],
        status=JobStatus.CANCELLED,
        finished_at=FIXED_TIME,
    )
    cancelled_case = _rebuild(
        aggregate.case,
        status="CANCELLED",
        case_revision=aggregate.case.case_revision + 1,
        active_job_id=None,
        updated_at=FIXED_TIME,
    )
    rebuilt_aggregate = _rebuild(
        aggregate,
        case=cancelled_case,
        jobs={ROUTE_JOB_ID: cancelled_job},
    )
    return _rebuild(
        state,
        cases={CASE_ID: rebuilt_aggregate},
        generation=state.generation + 1,
        updated_at=FIXED_TIME,
    )


def _trigger(
    state: StateFile,
    trigger_type: TriggerType,
    payload: object,
) -> ValidatedTrigger:
    return ValidatedTrigger(
        trigger_id=TRIGGER_ID,
        trigger_type=trigger_type,
        case_id=CASE_ID,
        expected_case_revision=state.cases[CASE_ID].case.case_revision,
        idempotency_key=f"s08-failure-matrix-{trigger_type.value.lower()}",
        payload=payload,
        continuation_resources=empty_continuation_resources(),
        runtime_bindings_by_job_type={},
        occurred_at=FIXED_TIME,
    )


def _failure_plan(state: StateFile, failure: ExecutionFailure):
    trigger = _trigger(
        state,
        TriggerType.EXECUTION_FAILED,
        ExecutionFailedTriggerPayload(
            source_job_id=ROUTE_JOB_ID,
            source_outcome_id=None,
            execution_failure=failure,
        ),
    )
    return DomainCoordinator().plan(build_case_snapshot(state, CASE_ID), trigger)


def _assert_failure_plan(scenario: dict[str, Any], failure: ExecutionFailure) -> None:
    plan = _failure_plan(_running_state(), failure)
    assert not isinstance(plan, ApplicationError)
    assert plan.target_case_status.value == scenario["expected_case_status"]
    assert len(plan.job_updates) == 1
    assert plan.job_updates[0].target_status.value == scenario["expected_job_status"]


def _matrix() -> dict[str, Any]:
    raw = MATRIX_PATH.read_bytes()
    payload = json.loads(raw)
    assert raw == canonical_json_bytes(payload)
    return payload


def test_failures_fixture_manifest_is_strict_canonical_and_exhaustive() -> None:
    manifest_path = FAILURE_FIXTURES / "fixture-manifest.json"
    raw = manifest_path.read_bytes()
    payload = json.loads(raw)

    schema_validator("fixture-manifest.schema.json").validate(payload)
    manifest = FixtureManifest.model_validate(payload)
    assert raw == canonical_json_bytes(manifest)
    assert manifest.owner_spec == "S08"
    assert manifest.root == "tests/fixtures/failures"

    actual: list[str] = []
    for path in FAILURE_FIXTURES.rglob("*"):
        assert path.is_symlink() is False
        if path.is_file() and path != manifest_path:
            actual.append(path.relative_to(FAILURE_FIXTURES).as_posix())
    actual.sort()
    assert [entry.path for entry in manifest.files] == actual

    for entry in manifest.files:
        fixture_path = FAILURE_FIXTURES / entry.path
        fixture_bytes = fixture_path.read_bytes()
        assert entry.size == len(fixture_bytes)
        assert entry.sha256 == hashlib.sha256(fixture_bytes).hexdigest()
        if entry.schema_ref is not None:
            assert (ROOT / entry.schema_ref).is_file()
        if fixture_path.suffix == ".json":
            assert fixture_bytes == canonical_json_bytes(json.loads(fixture_bytes))


def test_failure_matrix_has_the_complete_s08_negative_category_partition() -> None:
    payload = _matrix()
    assert set(payload) == {"contract_revision", "scenarios", "schema_version"}
    assert payload["contract_revision"] == "v4-contract-r1"
    assert payload["schema_version"] == 1

    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list) and scenarios
    ids = [scenario["id"] for scenario in scenarios]
    categories = [scenario["category"] for scenario in scenarios]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert set(categories) == _REQUIRED_CATEGORIES
    assert len(categories) == len(set(categories))

    for scenario in scenarios:
        mapping = scenario["mapping"]
        assert mapping in _SCENARIO_KEYS
        assert set(scenario) == _SCENARIO_KEYS[mapping]
        if "failure_points" in scenario:
            points = scenario["failure_points"]
            assert points == sorted(set(points)) and points


def test_failure_matrix_drives_real_failure_and_recovery_mappings() -> None:
    route_outcome = _fixture(JobOutcome, "job-outcome-route.json")

    for scenario in _matrix()["scenarios"]:
        mapping = scenario["mapping"]
        if mapping == "application_submission_retry":
            code = ErrorCode(scenario["error_code"])
            assert ERROR_SPECS[code].application_retryable is (
                scenario["expected_application_retryable"]
            )
            assert code in JOB_OUTCOME_SUBMISSION_RETRY_ERROR_CODES
            assert code not in JOB_OUTCOME_SUBMISSION_PARK_ERROR_CODES
            assert code not in DETERMINISTIC_OUTCOME_FAILURE_SPECS
            continue

        if mapping == "deterministic_outcome_failure":
            failure = deterministic_outcome_failure(ErrorCode(scenario["error_code"]))
            assert failure.stage.value == scenario["expected_stage"]
            assert failure.message == scenario["expected_message"]
            assert failure.retryable is scenario["expected_retryable"]
            _assert_failure_plan(scenario, failure)
            continue

        if mapping == "execution_failure_transition":
            failure = ExecutionFailure(
                stage=ExecutionStage(scenario["stage"]),
                code=ErrorCode(scenario["error_code"]),
                message=scenario["message"],
                retryable=scenario["retryable"],
                details=[],
            )
            _assert_failure_plan(scenario, failure)
            continue

        if mapping == "invalid_outcome_activity":
            invalid_outcome = _rebuild(
                route_outcome,
                base_state_revision=route_outcome.base_state_revision + 1,
            )
            state = _running_state()
            decision = classify_outcome_activity(
                state.cases[CASE_ID],
                invalid_outcome,
            )
            assert decision.activity.value == scenario["expected_activity"]
            assert decision.error_code is ErrorCode(scenario["expected_error_code"])
            failure = deterministic_outcome_failure(decision.error_code)
            assert failure.stage.value == scenario["expected_stage"]
            assert failure.message == scenario["expected_message"]
            assert failure.retryable is scenario["expected_retryable"]
            _assert_failure_plan(scenario, failure)
            continue

        if mapping == "late_outcome_activity":
            decision = classify_outcome_activity(
                _cancelled_state().cases[CASE_ID],
                route_outcome,
            )
            assert decision.activity is OutcomeActivity(
                scenario["expected_activity"]
            )
            assert decision.error_code is None
            continue

        if mapping == "missing_published_outcome":
            outcome_bytes = canonical_json_bytes(route_outcome)
            expected_ref = ExecutionFileRef(
                relative_key=f"jobs/{ROUTE_JOB_ID}/job_outcome.json",
                size=len(outcome_bytes),
                sha256=hashlib.sha256(outcome_bytes).hexdigest(),
            )
            validation = validate_published_outcome(
                route_outcome,
                expected_ref,
                None,
            )
            assert validation.outcome is None
            assert validation.error_code is ErrorCode(
                scenario["expected_error_code"]
            )
            failure = deterministic_outcome_failure(validation.error_code)
            assert failure.stage.value == scenario["expected_stage"]
            assert failure.message == scenario["expected_message"]
            assert failure.retryable is scenario["expected_retryable"]
            _assert_failure_plan(scenario, failure)
            continue

        if mapping == "old_epoch_transition":
            state = _running_state(scenario["previous_runtime_epoch"])
            trigger = _trigger(
                state,
                TriggerType.MARK_OLD_EPOCH_INTERRUPTED,
                OldEpochTriggerPayload(
                    source_job_id=ROUTE_JOB_ID,
                    previous_runtime_epoch=scenario["previous_runtime_epoch"],
                    current_runtime_epoch=scenario["current_runtime_epoch"],
                ),
            )
            plan = DomainCoordinator().plan(build_case_snapshot(state, CASE_ID), trigger)
            assert not isinstance(plan, ApplicationError)
            assert plan.target_case_status.value == scenario["expected_case_status"]
            assert len(plan.job_updates) == 1
            assert plan.job_updates[0].target_status.value == (
                scenario["expected_job_status"]
            )
            assert plan.outcome_disposition is None
            continue

        raise AssertionError(f"unhandled failure mapping: {mapping}")
