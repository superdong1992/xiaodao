from __future__ import annotations

import copy

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from problem_locator.contracts import (
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    ArtifactKind,
    JobOutcome,
    UserResultArchiveMetadataV2,
    UserResultCitationV2,
    UserResultFindingV2,
    UserResultMetadataV2,
    UserResultPayloadV2,
    UserResultTimeRelevanceV2,
    UserResultVerificationRuleV2,
    finalize_unresolved_result,
)
from problem_locator.contracts.models import UnresolvedResultDraft
from tests.contracts._support import FIXTURE_ROOT, load_json, schema_validator


EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
JOB_ID = "00000000-0000-0000-0000-000000000011"
OUTCOME_ID = "00000000-0000-0000-0000-000000000021"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000081"
AUDIT_ID = "00000000-0000-0000-0000-000000000082"


def _positive(name: str) -> dict:
    return load_json(FIXTURE_ROOT / "positive" / name)


def test_user_result_v2_exposes_the_complete_public_shape_and_rejects_v1() -> None:
    payload = UserResultPayloadV2.model_validate(_positive("user-result.json"))
    assert set(type(payload).model_fields) == {
        "schema_version",
        "format_id",
        "status",
        "source_job_type",
        "problem_statement",
        "root_cause",
        "findings",
        "supporting_evidence_bindings",
        "completion_criteria_mapping",
        "verification_rules",
        "time_relevance",
        "evidence_gaps",
        "limitations",
        "recommendations",
    }
    with pytest.raises(ValidationError):
        UserResultPayloadV2.model_validate(
            {
                "schema_version": 1,
                "format_id": "problem-locator-diagnosis-v1",
            }
        )
    with pytest.raises(ValidationError):
        UserResultMetadataV2.model_validate(
            {
                "schema_version": 1,
                "format_id": "problem-locator-diagnosis-v1",
                "description": "legacy",
            }
        )
    with pytest.raises(ValidationError):
        UserResultArchiveMetadataV2.model_validate(
            {
                "schema_version": 1,
                "format_id": "problem-locator-result-archive-v1",
                "description": "legacy",
                "user_result_proposal_key": "result",
                "target_log_count": 0,
            }
        )


def test_inconclusive_user_result_requires_an_explicit_evidence_gap() -> None:
    payload = _positive("user-result.json")
    payload["status"] = "INCONCLUSIVE"
    payload["root_cause"] = None
    payload["evidence_gaps"] = []
    payload["limitations"] = [
        "The available log timestamp cannot be aligned to the reported event."
    ]

    with pytest.raises(ValidationError, match="requires at least one evidence gap"):
        UserResultPayloadV2.model_validate(payload)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator("user-result.schema.json").validate(payload)


def test_public_citation_location_is_generic_or_fully_populated() -> None:
    binding = {
        "existing_evidence_id": EVIDENCE_ID,
        "evidence_proposal_key": None,
    }
    with pytest.raises(ValidationError, match="either all null or all non-null"):
        UserResultCitationV2.model_validate(
            {
                "evidence_binding": binding,
                "archive_name": "payment.log",
                "line_start": 1,
                "line_end": 2,
                "raw_bytes_sha256": None,
                "excerpt": "timeout",
            }
        )
    with pytest.raises(ValidationError, match="safe filename"):
        UserResultCitationV2.model_validate(
            {
                "evidence_binding": binding,
                "archive_name": "internal/events/payment.log",
                "line_start": None,
                "line_end": None,
                "raw_bytes_sha256": None,
                "excerpt": None,
            }
        )
    citation = UserResultCitationV2.model_validate(
        {
            "evidence_binding": binding,
            "archive_name": "payment.log",
            "line_start": 1,
            "line_end": 2,
            "raw_bytes_sha256": "a" * 64,
            "excerpt": "timeout",
        }
    )
    assert citation.archive_name == "payment.log"


def test_time_relevance_structures_and_checks_relative_offsets() -> None:
    value = {
        "assessment": "RELEVANT",
        "problem_time": "2026-07-31T00:00:00.000Z",
        "derived_anchor_time": None,
        "observations": [
            {
                "rule_id": "event_present",
                "event_time": "2026-07-31T00:00:00.250Z",
                "offset_ms": 250,
            }
        ],
        "explanation": "The event occurs 250 ms after the reported problem time.",
        "citations": [],
    }
    assert UserResultTimeRelevanceV2.model_validate(value).observations[0].offset_ms == 250
    value["observations"][0]["offset_ms"] = 249
    with pytest.raises(ValidationError, match="relative to problem_time"):
        UserResultTimeRelevanceV2.model_validate(value)


@pytest.mark.parametrize(
    "model_type",
    [UserResultFindingV2, UserResultVerificationRuleV2],
)
def test_finding_and_rule_bindings_exactly_follow_citation_order(model_type: type) -> None:
    first = {
        "existing_evidence_id": EVIDENCE_ID,
        "evidence_proposal_key": None,
    }
    second = {
        "existing_evidence_id": "00000000-0000-0000-0000-000000000041",
        "evidence_proposal_key": None,
    }
    citations = [
        {
            "evidence_binding": binding,
            "archive_name": None,
            "line_start": None,
            "line_end": None,
            "raw_bytes_sha256": None,
            "excerpt": None,
        }
        for binding in (first, second)
    ]
    value = {
        "evidence_bindings": [second, first],
        "citations": citations,
    }
    if model_type is UserResultFindingV2:
        value.update(statement="A verified finding.", confidence=0.9)
        message = "finding citations must cover evidence_bindings"
    else:
        value.update(
            rule_id="event_present",
            rule_kind="EVENT_PRESENT",
            status="SEMANTIC_ONLY",
            explanation="The rule was assessed semantically.",
            observed_times=[],
            issues=[],
        )
        message = "verification rule citations must cover evidence_bindings"
    with pytest.raises(ValidationError, match=message):
        model_type.model_validate(value)


def test_agent_draft_forbids_server_generated_user_result_artifacts() -> None:
    draft = _positive("agent-job-outcome-draft-diagnosis.json")
    final = _positive("agent-job-outcome-diagnosis.json")
    draft["proposed_artifact_drafts"] = final["proposed_artifact_drafts"]
    with pytest.raises(ValidationError, match="forbids server-generated USER_RESULT"):
        AgentJobOutcomeDraftV2.model_validate(draft)


def test_completed_candidate_server_final_requires_json_and_archive() -> None:
    payload = _positive("job-outcome-diagnosis.json")
    assert JobOutcome.model_validate(payload)
    for forbidden_kind in (ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE):
        drifted = copy.deepcopy(payload)
        drifted["proposed_artifacts"] = [
            item
            for item in drifted["proposed_artifacts"]
            if item["artifact_kind"] != forbidden_kind.value
        ]
        with pytest.raises(ValidationError, match="exactly one USER_RESULT"):
            JobOutcome.model_validate(drifted)


def test_inconclusive_server_final_requires_json_and_forbids_archive() -> None:
    payload = _positive("job-outcome-diagnosis.json")
    payload["result_type"] = "INCONCLUSIVE"
    payload["payload"]["candidate_conclusion_draft"] = None
    payload["proposed_artifacts"] = [
        item
        for item in payload["proposed_artifacts"]
        if item["artifact_kind"] == ArtifactKind.USER_RESULT.value
    ]
    assert JobOutcome.model_validate(payload)
    payload["proposed_artifacts"] = []
    with pytest.raises(ValidationError, match="unresolved server-final Outcome"):
        JobOutcome.model_validate(payload)


def test_non_pass_review_carries_json_but_pass_carries_no_new_result() -> None:
    pass_payload = _positive("job-outcome-review.json")
    assert JobOutcome.model_validate(pass_payload).proposed_artifacts == []

    rejected = copy.deepcopy(pass_payload)
    rejected["payload"]["verdict"] = "REJECT"
    rejected["payload"]["unsupported_findings"] = ["The causal claim is unsupported."]
    user_result = copy.deepcopy(
        _positive("job-outcome-diagnosis.json")["proposed_artifacts"][0]
    )
    user_result["proposal_key"] = "review_user_result"
    user_result["staged_resource_ref"]["proposal_key"] = "review_user_result"
    user_result["staged_resource_ref"]["owner_job_id"] = rejected["job_id"]
    rejected["proposed_artifacts"] = [user_result]
    assert JobOutcome.model_validate(rejected)

    rejected["proposed_artifacts"] = []
    with pytest.raises(ValidationError, match="unresolved server-final Outcome"):
        JobOutcome.model_validate(rejected)


def test_unresolved_result_hard_binds_the_user_result_artifact() -> None:
    draft = UnresolvedResultDraft(
        source_job_id=JOB_ID,
        source_outcome_id=OUTCOME_ID,
        reason_code="INSUFFICIENT_EVIDENCE",
        summary="The available evidence cannot establish a root cause.",
        blocking_rule_ids=["event_present"],
        evidence_bindings=[
            {
                "existing_evidence_id": EVIDENCE_ID,
                "evidence_proposal_key": None,
            }
        ],
        user_result_proposal_key="user_result",
        recommended_next_step="Collect the missing target-process logs.",
        occurred_at="2026-07-31T00:01:30.000Z",
    )
    result = finalize_unresolved_result(
        draft,
        audit_artifact_id=AUDIT_ID,
        user_result_artifact_id=ARTIFACT_ID,
        evidence_refs=[EVIDENCE_ID],
    )
    assert result.user_result_artifact_id == ARTIFACT_ID
