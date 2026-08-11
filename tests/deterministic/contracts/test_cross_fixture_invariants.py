from __future__ import annotations

import copy
import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import SCHEMA_MODELS
from problem_locator.contracts.serialization import canonical_json_sha256

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json


def test_state_aggregate_keys_and_active_job_match_the_job_snapshot() -> None:
    state = load_json(FIXTURE_ROOT / "positive" / "state.json")
    route_job = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    case_id = route_job["case_id"]
    job_id = route_job["job_id"]
    aggregate = state["cases"][case_id]

    assert aggregate["case"]["case_id"] == case_id
    assert aggregate["case"]["active_job_id"] == job_id
    assert aggregate["jobs"][job_id] == route_job
    assert route_job["base_state_revision"] == (
        route_job["context_snapshot"]["diagnosis_state_revision"]
    )
    assert route_job["context_snapshot"] == {
        key: value
        for key, value in aggregate["case"]["diagnosis_state"].items()
        if key != "revision"
    } | {"diagnosis_state_revision": 1}


def test_review_job_target_candidate_and_supporting_evidence_are_fixed() -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-review.json")
    candidate = job["context_snapshot"]["candidate_conclusion"]
    assert job["review_target"] == {
        "candidate_conclusion_id": candidate["conclusion_id"],
        "candidate_content_hash": candidate["content_hash"],
        "candidate_revision": candidate["revision"],
    }
    assert candidate["status"] == "REVIEWING"
    assert candidate["supporting_evidence_refs"] == job["evidence_refs"]

    preimage = {
        "resolution_status": candidate["resolution_status"],
        "terminal_path_id": candidate["terminal_path_id"],
        "statement": candidate["statement"],
        "causal_factors": candidate["causal_factors"],
        "candidate_factors": candidate["candidate_factors"],
        "excluded_factors": candidate["excluded_factors"],
        "supporting_evidence_refs": candidate["supporting_evidence_refs"],
        "completion_criteria_mapping": candidate["completion_criteria_mapping"],
    }
    assert canonical_json_sha256(preimage) == candidate["content_hash"]


def _assert_user_result_matches_candidate(
    job: dict[str, object],
    agent_outcome: dict[str, object],
    user_result: dict[str, object],
    user_result_bytes: bytes,
) -> None:
    payload = agent_outcome["payload"]
    assert isinstance(payload, dict)
    candidate = payload["candidate_conclusion_draft"]
    assert isinstance(candidate, dict)
    assert user_result["problem_statement"] == job["context_snapshot"]["problem_spec"][
        "statement"
    ]
    assert user_result["status"] == "COMPLETED"
    assert user_result["source_job_type"] == agent_outcome["job_type"]
    assert user_result["root_cause"] == candidate["statement"]
    assert user_result["supporting_evidence_bindings"] == candidate[
        "supporting_evidence_bindings"
    ]
    assert user_result["completion_criteria_mapping"] == candidate[
        "completion_criteria_mapping"
    ]

    user_result_drafts = [
        proposal
        for proposal in agent_outcome["proposed_artifact_drafts"]
        if proposal["artifact_kind"] == "USER_RESULT"
    ]
    assert len(user_result_drafts) == 1
    proposal = user_result_drafts[0]
    assert proposal["resource_kind"] == "FILE"
    assert proposal["content_type"] == "application/json"
    assert proposal["declared_size"] == len(user_result_bytes)
    assert proposal["declared_sha256"] == hashlib.sha256(user_result_bytes).hexdigest()
    archives = [
        proposal
        for proposal in agent_outcome["proposed_artifact_drafts"]
        if proposal["artifact_kind"] == "USER_RESULT_ARCHIVE"
    ]
    assert len(archives) == 1
    assert archives[0]["metadata"]["user_result_proposal_key"] == proposal["proposal_key"]


def test_user_result_is_the_exact_candidate_representation() -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    outcome = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    result_path = FIXTURE_ROOT / "positive" / "user-result.json"
    _assert_user_result_matches_candidate(
        job, outcome, load_json(result_path), result_path.read_bytes()
    )


@pytest.mark.parametrize(
    "field",
    [
        "problem_statement",
        "root_cause",
        "supporting_evidence_bindings",
        "completion_criteria_mapping",
    ],
)
def test_user_result_semantic_drift_is_detected(field: str) -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    outcome = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    result_path = FIXTURE_ROOT / "positive" / "user-result.json"
    result = load_json(result_path)
    result[field] = "drift" if isinstance(result[field], str) else []
    with pytest.raises(AssertionError):
        _assert_user_result_matches_candidate(
            job, outcome, result, result_path.read_bytes()
        )


def test_normalized_user_result_proposal_preserves_size_hash_and_owner() -> None:
    agent = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    normalized = load_json(
        FIXTURE_ROOT / "positive" / "job-outcome-diagnosis.json"
    )
    draft = agent["proposed_artifact_drafts"][0]
    proposal = normalized["proposed_artifacts"][0]
    staged = proposal["staged_resource_ref"]
    assert proposal["proposal_key"] == draft["proposal_key"]
    assert proposal["size"] == draft["declared_size"] == staged["size"]
    assert proposal["sha256"] == draft["declared_sha256"] == staged["sha256"]
    assert staged["owner_job_id"] == normalized["job_id"]
    assert staged["proposal_key"] == proposal["proposal_key"]
    assert "workspace_relative_path" not in proposal
    assert "workspace_relative_path" not in staged


def test_workspace_manifest_is_grouped_and_fixed_to_logparse_job() -> None:
    manifest = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    diagnose_job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    claim = load_json(FIXTURE_ROOT / "positive" / "logparse-parse-claim.json")

    assert [entry["input_kind"] for entry in manifest["entries"]] == [
        "ATTACHMENT",
        "EVIDENCE",
        "ARTIFACT",
        "PREVIOUS_OUTCOME",
    ]
    assert manifest["job_id"] == diagnose_job["job_id"] == claim["job_id"]
    assert manifest["case_id"] == diagnose_job["case_id"]
    assert manifest["logparse_tool_ref"] == diagnose_job["logparse_tool_ref"]
    assert claim["logparse_tool_ref"] == diagnose_job["logparse_tool_ref"]
    assert manifest["logparse_product"] == diagnose_job["logparse_product"]
    assert claim["attachment_id"] == diagnose_job["attachment_refs"][0]
    grouped_ids = {
        kind: [
            entry["resource_id"]
            for entry in manifest["entries"]
            if entry["input_kind"] == kind
        ]
        for kind in ("ATTACHMENT", "EVIDENCE", "ARTIFACT", "PREVIOUS_OUTCOME")
    }
    assert grouped_ids == {
        "ATTACHMENT": diagnose_job["attachment_refs"],
        "EVIDENCE": diagnose_job["evidence_refs"],
        "ARTIFACT": diagnose_job["artifact_refs"],
        "PREVIOUS_OUTCOME": diagnose_job["previous_outcome_refs"],
    }


def test_workspace_manifest_rejects_duplicate_ids_and_wrong_group_order() -> None:
    base = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    model_type = SCHEMA_MODELS["workspace-input-manifest.schema.json"]

    duplicate = copy.deepcopy(base)
    duplicate["entries"].insert(1, copy.deepcopy(duplicate["entries"][0]))
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(model_type).validate_python(duplicate)

    wrong_order = copy.deepcopy(base)
    wrong_order["entries"][0], wrong_order["entries"][1] = (
        wrong_order["entries"][1],
        wrong_order["entries"][0],
    )
    with pytest.raises((TypeError, ValueError, ValidationError)):
        TypeAdapter(model_type).validate_python(wrong_order)
