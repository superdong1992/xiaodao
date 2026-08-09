from __future__ import annotations

import json
from pathlib import Path

import pytest

from problem_locator.application.idempotency import (
    IdempotencyDisposition,
    decide_idempotency,
    make_idempotency_record,
)
from problem_locator.contracts.models import (
    BusinessReceipt,
    CreateCase,
    IdempotencyRecord,
    StateFile,
)
from problem_locator.contracts.serialization import business_request_sha256


ROOT = Path(__file__).resolve().parents[4]


def _state() -> StateFile:
    payload = json.loads(
        (ROOT / "tests/fixtures/contracts/positive/state.json").read_text(
            encoding="utf-8"
        )
    )
    return StateFile.model_validate(payload)


def _command(*, wait_seconds: int = 0, statement: str = "RPC timeout") -> CreateCase:
    return CreateCase(
        idempotency_key="create-1",
        problem_spec={
            "statement": statement,
            "expected_behavior": "The RPC succeeds.",
            "actual_behavior": "The RPC times out.",
            "scope": "payment to inventory",
            "goals": ["Locate the cause."],
            "non_goals": [],
            "constraints": [],
            "completion_criteria": ["The timeout cause is evidenced."],
        },
        initial_user_facts=[],
        wait_seconds=wait_seconds,
    )


def _receipt() -> BusinessReceipt:
    return BusinessReceipt(
        operation="CreateCase",
        primary_resource_id="00000000-0000-0000-0000-000000000001",
        case_id="00000000-0000-0000-0000-000000000001",
        case_revision=1,
        job_id="00000000-0000-0000-0000-000000000010",
        status="RUNNING",
    )


def test_wait_seconds_is_excluded_and_replays_saved_business_receipt() -> None:
    initial = _state()
    command = _command(wait_seconds=0)
    record = make_idempotency_record(
        command,
        business_request_sha256(command),
        _receipt(),
        case_id=_receipt().case_id,
        created_at="2026-07-31T00:00:00.000Z",
    )
    state = initial.model_copy(
        update={"idempotency_records": {"CreateCase:create-1": record}}
    )

    decision = decide_idempotency(state, _command(wait_seconds=30))

    assert decision.disposition is IdempotencyDisposition.REPLAY
    assert decision.record == record


def test_same_key_with_changed_business_payload_is_a_conflict() -> None:
    initial = _state()
    command = _command()
    record = IdempotencyRecord(
        operation="CreateCase",
        idempotency_key="create-1",
        request_hash=business_request_sha256(command),
        business_receipt=_receipt(),
        case_id=_receipt().case_id,
        created_at="2026-07-31T00:00:00.000Z",
    )
    state = initial.model_copy(
        update={"idempotency_records": {"CreateCase:create-1": record}}
    )

    decision = decide_idempotency(
        state,
        _command(statement="A different stable problem"),
    )

    assert decision.disposition is IdempotencyDisposition.CONFLICT
    assert decision.record == record


def test_unseen_operation_and_key_is_new() -> None:
    decision = decide_idempotency(_state(), _command())

    assert decision.disposition is IdempotencyDisposition.NEW
    assert decision.record is None


def test_record_builder_rejects_cross_object_receipt_mismatches() -> None:
    command = _command()
    wrong_operation = _receipt().model_copy(update={"operation": "CancelCase"})
    with pytest.raises(ValueError, match="operation"):
        make_idempotency_record(
            command,
            business_request_sha256(command),
            wrong_operation,
            case_id=_receipt().case_id,
            created_at="2026-07-31T00:00:00.000Z",
        )

    wrong_case = _receipt().model_copy(
        update={"case_id": "00000000-0000-0000-0000-000000000002"}
    )
    with pytest.raises(ValueError, match="case_id"):
        make_idempotency_record(
            command,
            business_request_sha256(command),
            wrong_case,
            case_id=_receipt().case_id,
            created_at="2026-07-31T00:00:00.000Z",
        )
