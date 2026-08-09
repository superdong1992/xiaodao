from __future__ import annotations

import json

import pytest

from problem_locator.contracts.enums import ErrorCode, OutcomeDisposition
from problem_locator.contracts.models import (
    CreateCase,
    JobOutcome,
    PrepareAttachment,
    ProblemSpec,
    ProblemSpecInput,
    ProblemSpecPatch,
)

from tests.deterministic.contracts._support import FIXTURE_ROOT
from tests.deterministic.contracts.scenario_fakes import (
    CASE_ID,
    IdempotencyScenario,
    OutcomeAuditScenario,
    ProblemSpecScenario,
    ScenarioError,
    normalized_request_hash,
)


def _problem_input() -> ProblemSpecInput:
    return ProblemSpecInput(
        statement="A payment service call to inventory times out.",
        expected_behavior="The payment request completes.",
        actual_behavior="The payment request times out.",
        scope="payment-to-inventory RPC",
        goals=["Locate the timeout cause."],
        non_goals=[],
        constraints=[],
        completion_criteria=["Identify the timed-out request."],
    )


def test_same_normalized_request_reuses_receipt_but_conflicting_request_is_rejected() -> None:
    ledger = IdempotencyScenario()
    first = CreateCase(
        idempotency_key="create-request-1",
        problem_spec=_problem_input(),
        initial_user_facts=[],
        wait_seconds=0,
    )
    replay = first.model_copy(update={"wait_seconds": 30})
    conflict = first.model_copy(
        update={
            "problem_spec": first.problem_spec.model_copy(
                update={"actual_behavior": "A different failure is observed."}
            )
        }
    )
    assert normalized_request_hash(first) == normalized_request_hash(replay)
    assert normalized_request_hash(first) != normalized_request_hash(conflict)

    first_receipt, duplicate = ledger.submit(first)
    assert duplicate is False
    revision_after_first = ledger.case_revision
    replay_receipt, duplicate = ledger.submit(replay)
    assert duplicate is True
    assert replay_receipt == first_receipt
    assert ledger.case_revision == revision_after_first

    with pytest.raises(ScenarioError) as rejected:
        ledger.submit(conflict)
    assert rejected.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert rejected.value.error.retryable is False
    assert ledger.case_revision == revision_after_first


def test_stale_expected_revision_is_rejected_without_record_or_revision_change() -> None:
    ledger = IdempotencyScenario(case_revision=4)
    command = PrepareAttachment(
        idempotency_key="prepare-request-1",
        case_id=CASE_ID,
        expected_case_revision=3,
        name="rpc.log",
        content_type="text/plain",
        declared_size=12,
        declared_sha256="a" * 64,
    )

    with pytest.raises(ScenarioError) as stale:
        ledger.submit(command)
    assert stale.value.error.code is ErrorCode.REVISION_CONFLICT
    assert stale.value.error.retryable is True
    assert ledger.case_revision == 4
    assert ledger.records == {}


def test_late_outcome_is_audited_once_and_exact_replay_is_duplicate() -> None:
    outcome = JobOutcome.model_validate(
        json.loads(
            (FIXTURE_ROOT / "positive/job-outcome-diagnosis.json").read_text(
                encoding="utf-8"
            )
        )
    )
    audit = OutcomeAuditScenario(
        active_job_id="00000000-0000-0000-0000-000000000099",
        case_revision=7,
        diagnosis_state_revision=outcome.base_state_revision,
    )

    assert audit.submit(outcome) is OutcomeDisposition.STALE
    assert audit.case_revision == 8
    assert audit.diagnosis_state_revision == outcome.base_state_revision
    assert audit.submit(outcome) is OutcomeDisposition.DUPLICATE
    assert audit.case_revision == 8
    assert audit.diagnosis_state_revision == outcome.base_state_revision


def test_problem_spec_same_value_target_rejection_and_legal_patch_revisions() -> None:
    problem = ProblemSpec.model_validate(
        {**_problem_input().model_dump(mode="python"), "revision": 1}
    )
    scenario = ProblemSpecScenario(
        problem,
        case_revision=11,
        diagnosis_state_revision=8,
    )

    changed = scenario.apply_patch(
        ProblemSpecPatch(actual_behavior=problem.actual_behavior)
    )
    assert changed is False
    assert scenario.problem_spec.revision == 1
    assert scenario.case_revision == 11
    assert scenario.diagnosis_state_revision == 8

    with pytest.raises(ScenarioError) as target_change:
        scenario.apply_patch(
            ProblemSpecPatch(
                expected_behavior="Inventory may time out without a response."
            )
        )
    assert target_change.value.error.code is ErrorCode.NEW_CASE_REQUIRED
    assert target_change.value.error.retryable is False
    assert scenario.problem_spec == problem
    assert scenario.case_revision == 11
    assert scenario.diagnosis_state_revision == 8

    assert scenario.apply_patch(
        ProblemSpecPatch(
            constraints=["Preserve the original log archive byte-for-byte."]
        )
    )
    assert scenario.problem_spec.revision == 2
    assert scenario.problem_spec.constraints == [
        "Preserve the original log archive byte-for-byte."
    ]
    assert scenario.case_revision == 12
    assert scenario.diagnosis_state_revision == 9

    scenario.apply_semantic_change_without_problem_patch()
    assert scenario.problem_spec.revision == 2
    assert scenario.case_revision == 13
    assert scenario.diagnosis_state_revision == 10
