from __future__ import annotations

import hashlib
import json
from pathlib import Path

from problem_locator.contracts import (
    COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER,
    CaseStatus,
    ErrorCode,
    FixtureManifest,
    TriggerType,
    canonical_json_bytes,
)
from problem_locator.contracts.serialization import parse_canonical_json_bytes

from ._builders import CONTRACT_FIXTURES, diagnose_job
from .test_failure_matrix import CONDITIONAL_CODES, FATAL_CODES
from .test_state_trigger_matrix import LEGAL_STATUS_TRIGGER_PAIRS


DOMAIN_FIXTURES = (
    Path(__file__).resolve().parents[3] / "fixtures" / "components" / "domain"
)


def _load(name: str) -> dict[str, object]:
    raw = (DOMAIN_FIXTURES / name).read_bytes()
    payload = json.loads(raw)
    assert canonical_json_bytes(payload) == raw
    assert payload["schema_version"] == 1
    assert payload["contract_revision"] == "v5-contract-r1"
    return payload


def test_domain_fixture_manifest_is_complete_and_content_addressed() -> None:
    manifest = parse_canonical_json_bytes(
        (DOMAIN_FIXTURES / "fixture-manifest.json").read_bytes(),
        model_type=FixtureManifest,
    )

    assert manifest.owner_spec == "S01"
    assert manifest.root == "tests/fixtures/components/domain"
    actual_paths = sorted(
        path.name
        for path in DOMAIN_FIXTURES.iterdir()
        if path.is_file() and path.name != "fixture-manifest.json"
    )
    assert [entry.path for entry in manifest.files] == actual_paths
    for entry in manifest.files:
        raw = (DOMAIN_FIXTURES / entry.path).read_bytes()
        assert entry.size == len(raw)
        assert entry.sha256 == hashlib.sha256(raw).hexdigest()


def test_routing_supplement_logparse_fixture_consumes_the_s00_seed() -> None:
    scenario = _load("routing-supplement-logparse-sequence.json")
    seed = json.loads(
        (CONTRACT_FIXTURES / "rpc-timeout-continuation.json").read_bytes()
    )
    source = diagnose_job()

    assert scenario["parameter_group_a"] == seed["parameter_group_a"]
    assert scenario["parameter_group_b"] == seed["parameter_group_b"]
    assert scenario["unique_log_requirement"] == {
        "kind": "ATTACHMENT",
        "name": seed["log_attachment_requirement"],
    }
    assert scenario["r10_to_r11_resources"] == {
        "artifact_refs": source.artifact_refs,
        "attachment_refs": source.attachment_refs,
        "evidence_refs": source.evidence_refs,
        "previous_outcome_refs": source.previous_outcome_refs,
    }


def test_candidate_and_review_fixtures_freeze_the_resolution_gate() -> None:
    candidate = _load("candidate-review-sequence.json")
    rework = _load("review-rework-sequence.json")

    candidate_steps = candidate["steps"]
    assert [step["step"] for step in candidate_steps] == [
        "DIAGNOSIS_CANDIDATE",
        "REVIEW_PASS",
    ]
    assert candidate_steps[0]["user_result_is_reviewer_input"] is False
    assert candidate_steps[0]["accepted_artifact_proposal_keys"] == [
        "user_result",
        "user_result_archive",
    ]
    assert candidate_steps[1]["to"] == "RESOLVED"
    assert {item["step"] for item in rework["transitions"]} == {
        "NEED_MORE_EVIDENCE",
        "NEED_MORE_EVIDENCE_MISSING_ONLY",
        "REJECT",
    }
    assert all(
        item["previous_outcome_mode"] == "PRIVATE_NOT_MATERIALIZED"
        for item in rework["transitions"]
    )
    by_step = {item["step"]: item for item in rework["transitions"]}
    assert by_step["NEED_MORE_EVIDENCE"]["to"] == "UNRESOLVED"
    assert by_step["NEED_MORE_EVIDENCE_MISSING_ONLY"]["to"] == "WAITING_INPUT"
    assert by_step["REJECT"]["to"] == "UNRESOLVED"
    assert all(item["next_job"] is None for item in rework["transitions"])


def test_control_recovery_fixture_covers_non_outcome_recovery_paths() -> None:
    scenario = _load("control-recovery-sequence.json")
    by_step = {item["step"]: item for item in scenario["scenarios"]}

    assert by_step["PENDING_REDISPATCH"] == {
        "coordinator_invoked": False,
        "owner": "S05",
        "same_job_id": True,
        "step": "PENDING_REDISPATCH",
    }
    assert by_step["OLD_EPOCH_INTERRUPT"]["expected_job_status"] == "INTERRUPTED"
    assert by_step["REVIEW_REPLACEMENT"]["next_job"] == "REVIEW"
    assert by_step["STABLE_TARGET_CHANGE"] == {
        "decision_error": "NEW_CASE_REQUIRED",
        "plan_present": False,
        "stable_target_changed": True,
        "step": "STABLE_TARGET_CHANGE",
    }


def test_failure_status_fixture_equals_the_executable_closed_matrices() -> None:
    scenario = _load("failure-status-matrix.json")

    assert tuple(ErrorCode(value) for value in scenario["fatal_failure_codes"]) == (
        FATAL_CODES
    )
    assert tuple(
        ErrorCode(value) for value in scenario["conditional_failure_codes"]
    ) == CONDITIONAL_CODES
    fixture_pairs = {
        (CaseStatus(status), TriggerType(trigger))
        for status, triggers in scenario["allowed_triggers_by_status"].items()
        for trigger in triggers
    }
    assert fixture_pairs == LEGAL_STATUS_TRIGGER_PAIRS
    assert scenario["idempotent_cancel_replay_owner"] == "S03_PRE_COORDINATOR"


def test_finalized_semantic_rejection_fixture_freezes_the_two_stage_chain() -> None:
    scenario = _load("finalized-semantic-rejection-sequence.json")

    expected_errors = {
        trigger.value: sorted(code.value for code in codes)
        for trigger, codes in COORDINATOR_PLAN_ERROR_CODES_BY_TRIGGER.items()
    }
    assert scenario["allowed_errors_by_trigger"] == expected_errors
    assert scenario["cases"] == [
        {"decision_error": "VALIDATION_ERROR", "source_trigger": "ROUTE_OUTCOME"},
        {
            "decision_error": "VALIDATION_ERROR",
            "source_trigger": "DIAGNOSIS_OUTCOME",
        },
        {
            "decision_error": "NEW_CASE_REQUIRED",
            "source_trigger": "DIAGNOSIS_OUTCOME",
        },
        {
            "decision_error": "VALIDATION_ERROR",
            "source_trigger": "REVIEW_OUTCOME",
        },
    ]
    assert scenario["normalization"] == {
        "code": "OUTCOME_INVALID",
        "message": "Job outcome validation failed.",
        "retryable": False,
        "stage": "OUTCOME_VALIDATE",
    }
    assert scenario["stale_ownership"] == {
        "application_owner": "S03",
        "coordinator_decision_error": "INVALID_CASE_STATE",
        "outcome_disposition": "STALE",
        "terminal_failure_created": False,
    }
    assert scenario["termination_plan"] == {
        "accepted_artifact_proposal_keys": [],
        "accepted_candidate_proposal_key": None,
        "accepted_evidence_proposal_keys": [],
        "case_status": "FAILED",
        "job_status": "FAILED",
        "next_job": None,
        "outcome_disposition": None,
        "rejected_processing_owner": "S03",
        "zero_state_delta": True,
    }
