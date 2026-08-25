from __future__ import annotations

import json
from pathlib import Path

import pytest

from problem_locator.contracts import JobOutcome, OutcomeResultType


ROOT = Path(__file__).resolve().parents[3]
DIAGNOSIS_OUTCOME = (
    ROOT / "tests/fixtures/contracts/positive/job-outcome-diagnosis.json"
)


def _waiting_value(result_type: OutcomeResultType) -> dict[str, object]:
    value = json.loads(DIAGNOSIS_OUTCOME.read_bytes())
    is_input = result_type is OutcomeResultType.NEED_INPUT
    requirement_id = "00000000-0000-4000-8000-000000000099"
    requirement = {
        "requirement_id": requirement_id,
        "kind": "INPUT" if is_input else "ATTACHMENT",
        "name": "problem_time" if is_input else "log_archive",
        "prompt": (
            "请提供毫秒精度 UTC 问题时间。"
            if is_input
            else "请上传 Logparse 支持的日志归档。"
        ),
        "required": True,
        "constraints": (
            {
                "value_type": "STRING",
                "min_utf8_bytes": 24,
                "max_utf8_bytes": 24,
                "pattern": (
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
                ),
                "allowed_values": [],
            }
            if is_input
            else {
                "allowed_content_types": [
                    "application/gzip",
                    "application/zip",
                    "application/x-tar",
                ],
                "min_count": 1,
                "max_count": 1,
            }
        ),
        "status": "OPEN",
        "requested_by_job_id": value["job_id"],
        "fulfilled_by_refs": [],
        "supplement_policy": "MISSING_ONLY",
    }
    value.update(
        result_type=result_type.value,
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        error=None,
        decision_audit=None,
    )
    value["payload"] = {
        "findings": [],
        "state_delta": {
            "problem_spec_patch": None,
            "add_user_facts": [],
            "proposed_facts": [],
            "add_active_hypotheses": [],
            "update_hypotheses": [],
            "reject_hypotheses": [],
            "add_open_questions": [],
            "resolve_questions": [],
            "add_pending_requirements": [requirement],
            "fulfill_requirements": [],
            "add_evidence_bindings": [],
        },
        "requested_input": [requirement_id] if is_input else [],
        "requested_attachments": [] if is_input else [requirement_id],
        "candidate_conclusion_draft": None,
        "recommended_next_step": "Supply the required server-preflight material.",
    }
    return value


@pytest.mark.parametrize(
    "result_type",
    (OutcomeResultType.NEED_INPUT, OutcomeResultType.NEED_ATTACHMENT),
)
def test_server_preflight_waiting_outcome_forbids_decision_audit(
    result_type: OutcomeResultType,
) -> None:
    value = _waiting_value(result_type)

    outcome = JobOutcome.model_validate(value)

    assert outcome.decision_audit is None
    value["decision_audit"] = json.loads(DIAGNOSIS_OUTCOME.read_bytes())[
        "decision_audit"
    ]
    with pytest.raises(ValueError, match="waiting DIAGNOSE outcomes forbid"):
        JobOutcome.model_validate(value)


def test_completed_diagnosis_still_requires_decision_audit() -> None:
    value = json.loads(DIAGNOSIS_OUTCOME.read_bytes())
    value["decision_audit"] = None

    with pytest.raises(ValueError, match="completed or inconclusive"):
        JobOutcome.model_validate(value)
