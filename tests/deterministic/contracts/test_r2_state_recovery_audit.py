from __future__ import annotations

import copy
import hashlib
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from problem_locator.contracts import models
from problem_locator.contracts.enums import ErrorCode, OutcomeDisposition
from problem_locator.contracts.limits import (
    CONTRACT_REVISION,
    MAX_ATTACHMENT_BYTES,
    SCHEMA_VERSION,
)
from problem_locator.contracts.models import (
    CaseAggregate,
    JobOutcome,
    PrepareAttachment,
    StateExport,
    StateFile,
    StateMutation,
)
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json
from tests.deterministic.contracts.fakes import InMemoryStateRepository


CASE_ID = "00000000-0000-0000-0000-000000000001"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
ROUTE_OUTCOME_ID = "00000000-0000-0000-0000-000000000020"
RECOVERY_ID = "00000000-0000-0000-0000-000000000080"
CURRENT_RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000081"
SECOND_RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000082"
PENDING_JOB_ID = "00000000-0000-0000-0000-000000000011"
SECOND_PENDING_JOB_ID = "00000000-0000-0000-0000-000000000012"
COMPLETED_AT = "2026-07-31T00:05:00.000Z"
LATER_COMPLETED_AT = "2026-07-31T00:06:00.000Z"
RUNTIME_STARTED_AT = "2026-07-31T00:00:00.000Z"


def _required_symbol(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"R2 requires {module.__name__}.{name}"
    return value


def _recovery_record_payload(
    *,
    completed_at: str | None = None,
    interrupted_job_ids: list[str] | None = None,
    pending_job_ids: list[str] | None = None,
    current_runtime_epoch: str = CURRENT_RUNTIME_EPOCH,
) -> dict[str, object]:
    return {
        "recovery_id": RECOVERY_ID,
        "current_runtime_epoch": current_runtime_epoch,
        "interrupted_job_ids": (
            [ROUTE_JOB_ID]
            if interrupted_job_ids is None
            else interrupted_job_ids
        ),
        "pending_job_ids": (
            [PENDING_JOB_ID, SECOND_PENDING_JOB_ID]
            if pending_job_ids is None
            else pending_job_ids
        ),
        "completed_at": completed_at,
    }


def _state_payload(
    recovery_processing_records: dict[str, object] | None = None,
) -> dict:
    payload = load_json(FIXTURE_ROOT / "positive" / "state.json")
    records = {} if recovery_processing_records is None else recovery_processing_records
    payload["recovery_processing_records"] = records
    payload["runtime_epochs"] = [
        {
            "runtime_epoch": (
                record.current_runtime_epoch
                if hasattr(record, "current_runtime_epoch")
                else record["current_runtime_epoch"]
            ),
            "started_at": RUNTIME_STARTED_AT,
            "recovery_id": (
                record.recovery_id
                if hasattr(record, "recovery_id")
                else record["recovery_id"]
            ),
            "recovery_completed_at": (
                record.completed_at
                if hasattr(record, "completed_at")
                else record["completed_at"]
            ),
        }
        for record in records.values()
    ]
    return payload


def _mutation_payload(
    recovery_records: list[object] | None = None,
) -> dict[str, object]:
    records = [] if recovery_records is None else recovery_records
    return {
        "upsert_case": None,
        "upsert_runtime_epoch_records": [
            {
                "runtime_epoch": record.current_runtime_epoch,
                "started_at": RUNTIME_STARTED_AT,
                "recovery_id": record.recovery_id,
                "recovery_completed_at": record.completed_at,
            }
            for record in records
        ],
        "upsert_recovery_processing_records": records,
        "insert_jobs": [],
        "job_lifecycle_updates": [],
        "insert_outcomes": [],
        "insert_outcome_processing_records": [],
        "insert_execution_failure_records": [],
        "upsert_attachments": [],
        "insert_evidence": [],
        "insert_artifacts": [],
        "insert_idempotency_records": [],
    }


def _object_counts(recovery_processing_records: int) -> dict[str, int]:
    return {
        "cases": 1,
        "jobs": 1,
        "outcomes": 0,
        "outcome_processing_records": 0,
        "execution_failure_records": 0,
        "attachments": 0,
        "evidence": 0,
        "artifacts": 0,
        "idempotency_records": 0,
        "runtime_epochs": 1 if recovery_processing_records else 0,
        "recovery_processing_records": recovery_processing_records,
    }


def _aggregate_with_route_processing() -> dict:
    aggregate = copy.deepcopy(
        load_json(FIXTURE_ROOT / "positive" / "state.json")["cases"][CASE_ID]
    )
    outcome_payload = load_json(
        FIXTURE_ROOT / "positive" / "job-outcome-route.json"
    )
    outcome = JobOutcome.model_validate(outcome_payload)
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_hash = hashlib.sha256(outcome_bytes).hexdigest()
    aggregate["outcomes"] = {ROUTE_OUTCOME_ID: outcome_payload}
    aggregate["outcome_processing_records"] = {
        ROUTE_OUTCOME_ID: {
            "outcome_id": ROUTE_OUTCOME_ID,
            "job_id": ROUTE_JOB_ID,
            "outcome_hash": outcome_hash,
            "outcome_file_ref": {
                "relative_key": f"jobs/{ROUTE_JOB_ID}/job_outcome.json",
                "size": len(outcome_bytes),
                "sha256": outcome_hash,
            },
            "disposition": OutcomeDisposition.APPLIED,
            "processed_at": "2026-07-31T00:00:30.000Z",
            "error_code": None,
            "accepted_evidence_ids": [],
            "accepted_artifact_ids": [],
            "created_job_id": None,
            "reason": "The route result was committed.",
        }
    }
    return aggregate


def _processing_without_trusted_outcome(
    disposition: str,
    error_code: str | None,
) -> dict:
    aggregate = _aggregate_with_route_processing()
    aggregate["outcomes"] = {}
    record = aggregate["outcome_processing_records"][ROUTE_OUTCOME_ID]
    record["disposition"] = disposition
    record["error_code"] = error_code
    return aggregate


def _prepare_payload(declared_size: object) -> dict[str, object]:
    return {
        "idempotency_key": "prepare-r2-size",
        "case_id": CASE_ID,
        "expected_case_revision": 1,
        "name": "large-diagnostic.log",
        "content_type": "text/plain",
        "declared_size": declared_size,
        "declared_sha256": None,
    }


def test_recovery_processing_record_has_only_the_five_frozen_fields() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    assert set(record_type.model_fields) == {
        "recovery_id",
        "current_runtime_epoch",
        "interrupted_job_ids",
        "pending_job_ids",
        "completed_at",
    }
    assert record_type.model_fields["completed_at"].is_required()

    record = record_type.model_validate(_recovery_record_payload())
    assert record.recovery_id == RECOVERY_ID
    assert record.completed_at is None


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("interrupted_job_ids", [ROUTE_JOB_ID, ROUTE_JOB_ID]),
        (
            "pending_job_ids",
            [SECOND_PENDING_JOB_ID, PENDING_JOB_ID],
        ),
    ],
)
def test_recovery_processing_lists_are_unique_and_stably_sorted(
    field: str,
    values: list[str],
) -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    payload = _recovery_record_payload()
    payload[field] = values

    with pytest.raises(ValidationError):
        record_type.model_validate(payload)


def test_recovery_processing_job_sets_are_disjoint() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    payload = _recovery_record_payload(
        interrupted_job_ids=[ROUTE_JOB_ID],
        pending_job_ids=[ROUTE_JOB_ID],
    )
    with pytest.raises(ValidationError, match="must be disjoint"):
        record_type.model_validate(payload)


def test_state_file_keys_recovery_records_by_recovery_id() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    record = record_type.model_validate(_recovery_record_payload())
    state = StateFile.model_validate(
        _state_payload({RECOVERY_ID: record.model_dump(mode="python")})
    )
    assert state.recovery_processing_records[RECOVERY_ID] == record

    wrong_key = _state_payload(
        {CURRENT_RUNTIME_EPOCH: record.model_dump(mode="python")}
    )
    with pytest.raises(ValidationError):
        StateFile.model_validate(wrong_key)


def test_state_file_pairs_recovery_audit_with_the_runtime_epoch() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    record = record_type.model_validate(_recovery_record_payload())
    valid = _state_payload({RECOVERY_ID: record.model_dump(mode="python")})
    StateFile.model_validate(valid)

    missing_runtime = copy.deepcopy(valid)
    missing_runtime["runtime_epochs"] = []
    with pytest.raises(ValidationError, match="exact recovery_id pair set"):
        StateFile.model_validate(missing_runtime)

    completion_drift = copy.deepcopy(valid)
    completion_drift["runtime_epochs"][0]["recovery_completed_at"] = COMPLETED_AT
    with pytest.raises(ValidationError, match="completion timestamps"):
        StateFile.model_validate(completion_drift)


def test_state_file_allows_at_most_one_incomplete_recovery() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    first = record_type.model_validate(_recovery_record_payload())
    second = record_type.model_validate(
        {
            **_recovery_record_payload(),
            "recovery_id": SECOND_RUNTIME_EPOCH,
            "current_runtime_epoch": SECOND_RUNTIME_EPOCH,
        }
    )
    payload = _state_payload(
        {
            first.recovery_id: first.model_dump(mode="python"),
            second.recovery_id: second.model_dump(mode="python"),
        }
    )
    with pytest.raises(ValidationError, match="at most one recovery"):
        StateFile.model_validate(payload)


def test_state_mutation_upserts_one_record_per_recovery_id() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    record = record_type.model_validate(_recovery_record_payload())
    mutation = StateMutation.model_validate(_mutation_payload([record]))
    assert mutation.upsert_recovery_processing_records == [record]

    with pytest.raises(ValidationError):
        StateMutation.model_validate(_mutation_payload([record, record]))


def test_state_export_counts_recovery_processing_records() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    record = record_type.model_validate(_recovery_record_payload())
    state = StateFile.model_validate(
        _state_payload({RECOVERY_ID: record.model_dump(mode="python")})
    )
    payload = {
        "export_schema_version": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "source_generation": state.generation,
        "installation_id": state.installation_id,
        "object_counts": _object_counts(1),
        "state": state,
        "resources": [],
    }
    export = StateExport.model_validate(payload)
    assert export.object_counts.recovery_processing_records == 1

    drifted = copy.deepcopy(payload)
    drifted["object_counts"]["recovery_processing_records"] = 0
    with pytest.raises(ValidationError):
        StateExport.model_validate(drifted)


def test_recovery_id_exact_replay_preserves_the_first_job_lists() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    initial = record_type.model_validate(_recovery_record_payload())
    repository = InMemoryStateRepository(StateFile.model_validate(_state_payload()))

    snapshot = repository.read_snapshot()
    repository.commit(
        snapshot.generation,
        None,
        StateMutation.model_validate(_mutation_payload([initial])),
    )
    replay_snapshot = repository.read_snapshot()
    repository.commit(
        replay_snapshot.generation,
        None,
        StateMutation.model_validate(_mutation_payload([initial])),
    )
    assert (
        repository.read_snapshot().recovery_processing_records[RECOVERY_ID]
        == initial
    )

    changed_list = record_type.model_validate(
        _recovery_record_payload(pending_job_ids=[PENDING_JOB_ID])
    )
    with pytest.raises(ValueError):
        current = repository.read_snapshot()
        repository.commit(
            current.generation,
            None,
            StateMutation.model_validate(_mutation_payload([changed_list])),
        )


def test_completed_recovery_record_is_immutable_but_exactly_replayable() -> None:
    record_type = _required_symbol(models, "RecoveryProcessingRecord")
    initial = record_type.model_validate(_recovery_record_payload())
    completed = record_type.model_validate(
        _recovery_record_payload(completed_at=COMPLETED_AT)
    )
    repository = InMemoryStateRepository(StateFile.model_validate(_state_payload()))

    for record in (initial, completed, completed):
        snapshot = repository.read_snapshot()
        repository.commit(
            snapshot.generation,
            None,
            StateMutation.model_validate(_mutation_payload([record])),
        )
    assert (
        repository.read_snapshot()
        .recovery_processing_records[RECOVERY_ID]
        .completed_at
        == COMPLETED_AT
    )

    drifts = [
        record_type.model_validate(_recovery_record_payload(completed_at=None)),
        record_type.model_validate(
            _recovery_record_payload(completed_at=LATER_COMPLETED_AT)
        ),
        record_type.model_validate(
            _recovery_record_payload(
                completed_at=COMPLETED_AT,
                current_runtime_epoch=SECOND_RUNTIME_EPOCH,
            )
        ),
    ]
    for drift in drifts:
        with pytest.raises(ValueError):
            snapshot = repository.read_snapshot()
            repository.commit(
                snapshot.generation,
                None,
                StateMutation.model_validate(_mutation_payload([drift])),
            )


@pytest.mark.parametrize(
    "technical_error",
    ["OUTCOME_MISSING", "EXECUTION_RECORD_FAILED", "OUTCOME_INVALID"],
)
def test_explicit_technical_rejection_may_lack_a_trusted_outcome(
    technical_error: str,
) -> None:
    assert technical_error in ErrorCode.__members__
    aggregate = CaseAggregate.model_validate(
        _processing_without_trusted_outcome("REJECTED", technical_error)
    )
    assert aggregate.outcomes == {}
    assert (
        aggregate.outcome_processing_records[ROUTE_OUTCOME_ID].error_code.value
        == technical_error
    )


@pytest.mark.parametrize(
    ("disposition", "error_code"),
    [
        ("APPLIED", None),
        ("STALE", None),
        ("REJECTED", "RESOURCE_LIMIT_EXCEEDED"),
    ],
)
def test_normal_processing_still_requires_a_trusted_outcome_pair(
    disposition: str,
    error_code: str | None,
) -> None:
    with pytest.raises(ValidationError):
        CaseAggregate.model_validate(
            _processing_without_trusted_outcome(disposition, error_code)
        )


def test_prepare_attachment_accepts_exact_limit_and_oversize_for_s03() -> None:
    exact = PrepareAttachment.model_validate(_prepare_payload(MAX_ATTACHMENT_BYTES))
    oversize = PrepareAttachment.model_validate(
        _prepare_payload(MAX_ATTACHMENT_BYTES + 1)
    )
    assert exact.declared_size == MAX_ATTACHMENT_BYTES
    assert oversize.declared_size == MAX_ATTACHMENT_BYTES + 1

    schema = PrepareAttachment.model_json_schema()["properties"]["declared_size"]
    integer_branch = next(
        branch for branch in schema["anyOf"] if branch.get("type") == "integer"
    )
    assert integer_branch["minimum"] == 0
    assert "maximum" not in integer_branch


@pytest.mark.parametrize("invalid", [-1, 1.0, "1", True])
def test_prepare_attachment_declared_size_remains_strict_non_negative_int(
    invalid: object,
) -> None:
    with pytest.raises(ValidationError):
        PrepareAttachment.model_validate(_prepare_payload(invalid))
