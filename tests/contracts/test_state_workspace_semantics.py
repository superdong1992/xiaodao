from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from problem_locator.contracts.enums import (
    CandidateStatus,
    CaseStatus,
    DiagnosisItemStatus,
    DiagnosisProvenanceType,
    TriggerType,
)
from problem_locator.contracts.models import (
    Case,
    ContinuationResourceView,
    CreateCaseTriggerPayload,
    DiagnosisItem,
    DiagnosisProvenance,
    Job,
    ValidatedTrigger,
    WorkspaceInputManifest,
    validate_workspace_manifest_for_job,
)
from problem_locator.contracts.serialization import canonical_json_sha256

from tests.contracts._support import FIXTURE_ROOT, load_json


TRIGGER_ID = "00000000-0000-0000-0000-000000000090"
CASE_ID = "00000000-0000-0000-0000-000000000001"


def _review_job_payload() -> dict:
    return load_json(FIXTURE_ROOT / "positive" / "job-review.json")


def _rehash_candidate(candidate: dict) -> None:
    candidate["content_hash"] = canonical_json_sha256(
        {
            "statement": candidate["statement"],
            "supporting_evidence_refs": candidate["supporting_evidence_refs"],
            "completion_criteria_mapping": candidate["completion_criteria_mapping"],
        }
    )


def test_snapshot_candidate_mapping_exactly_covers_problem_spec() -> None:
    payload = _review_job_payload()
    candidate = payload["context_snapshot"]["candidate_conclusion"]
    candidate["completion_criteria_mapping"][0]["criterion"] = "A different criterion."
    _rehash_candidate(candidate)
    payload["review_target"]["candidate_content_hash"] = candidate["content_hash"]

    with pytest.raises(ValidationError, match="exactly cover"):
        Job.model_validate(payload)


def test_snapshot_candidate_can_only_cite_current_state_evidence() -> None:
    payload = _review_job_payload()
    payload["context_snapshot"]["evidence_refs"] = []
    payload["evidence_refs"] = []
    with pytest.raises(ValidationError, match="belong to DiagnosisState"):
        Job.model_validate(payload)


def test_review_job_must_include_completion_mapping_only_evidence() -> None:
    payload = _review_job_payload()
    completion_only_ref = "00000000-0000-0000-0000-000000000041"
    candidate = payload["context_snapshot"]["candidate_conclusion"]
    candidate["completion_criteria_mapping"][0]["evidence_refs"] = [
        completion_only_ref
    ]
    _rehash_candidate(candidate)
    payload["review_target"]["candidate_content_hash"] = candidate["content_hash"]
    payload["context_snapshot"]["evidence_refs"].append(completion_only_ref)

    with pytest.raises(ValidationError, match="required candidate Evidence"):
        Job.model_validate(payload)


def test_resolved_case_final_result_is_the_complete_current_candidate() -> None:
    review_job = Job.model_validate(_review_job_payload())
    candidate = review_job.context_snapshot.candidate_conclusion
    assert candidate is not None
    accepted = candidate.model_copy(update={"status": CandidateStatus.ACCEPTED})
    diagnosis_state = {
        **review_job.context_snapshot.model_dump(mode="python"),
        "revision": review_job.context_snapshot.diagnosis_state_revision,
    }
    diagnosis_state.pop("diagnosis_state_revision")
    diagnosis_state["candidate_conclusion"] = accepted
    mismatched_final = accepted.model_copy(update={"revision": accepted.revision + 1})

    with pytest.raises(ValidationError, match="must equal"):
        Case.model_validate(
            {
                "case_id": CASE_ID,
                "status": CaseStatus.RESOLVED,
                "case_revision": 9,
                "diagnosis_state": diagnosis_state,
                "active_job_id": None,
                "selected_skill_ref": review_job.skill_ref,
                "final_result": mismatched_final,
                "failure": None,
                "created_at": "2026-07-31T00:00:00.000Z",
                "updated_at": "2026-07-31T00:02:00.000Z",
            }
        )


@pytest.mark.parametrize(
    ("entry_index", "wrong_path"),
    [
        (0, "inputs/attachments/wrong/payload"),
        (2, "inputs/artifacts/wrong/tree"),
        (3, "inputs/outcomes/wrong/job_outcome.json"),
    ],
)
def test_workspace_entries_use_only_fixed_materialization_paths(
    entry_index: int,
    wrong_path: str,
) -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    payload["entries"][entry_index]["relative_path"] = wrong_path
    with pytest.raises(ValidationError, match="fixed materialization path"):
        WorkspaceInputManifest.model_validate(payload)


def test_workspace_artifact_shape_and_tree_hash_are_revalidated() -> None:
    payload = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    artifact = payload["entries"][2]
    artifact["metadata"]["tree_manifest_sha256"] = "9" * 64
    with pytest.raises(ValidationError, match="tree hash"):
        WorkspaceInputManifest.model_validate(payload)


def test_public_workspace_job_seam_requires_exact_arrays_and_headers() -> None:
    job = Job.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    )
    manifest = WorkspaceInputManifest.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "workspace-input-manifest.json")
    )
    assert validate_workspace_manifest_for_job(manifest, job) is manifest

    drifted = manifest.model_copy(update={"case_id": TRIGGER_ID})
    with pytest.raises(ValueError, match="case_id"):
        validate_workspace_manifest_for_job(drifted, job)


def test_create_trigger_user_fact_provenance_references_the_trigger() -> None:
    problem_spec = Job.model_validate(_review_job_payload()).context_snapshot.problem_spec
    fact = DiagnosisItem(
        item_id="00000000-0000-0000-0000-000000000091",
        statement="The caller is payment-service.",
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.USER_INPUT,
            source_ref="00000000-0000-0000-0000-000000000099",
            input_name="caller_service",
        ),
        evidence_refs=[],
        created_revision=1,
        supersedes=[],
    )
    payload = CreateCaseTriggerPayload(
        problem_spec=problem_spec.model_copy(update={"revision": 1}),
        initial_user_facts=[fact],
    )
    trigger = {
        "trigger_id": TRIGGER_ID,
        "trigger_type": TriggerType.CREATE_CASE,
        "case_id": CASE_ID,
        "expected_case_revision": 0,
        "idempotency_key": "create-case-1",
        "payload": payload,
        "continuation_resources": ContinuationResourceView(
            evidence_refs=[],
            attachment_refs=[],
            artifact_refs=[],
            previous_outcome_refs=[],
        ),
        "runtime_bindings_by_job_type": {},
        "occurred_at": "2026-07-31T00:00:00.000Z",
    }
    with pytest.raises(ValidationError, match="trigger_id"):
        ValidatedTrigger.model_validate(trigger)

    corrected = copy.deepcopy(trigger)
    corrected["payload"] = payload.model_copy(
        update={
            "initial_user_facts": [
                fact.model_copy(
                    update={
                        "provenance": fact.provenance.model_copy(
                            update={"source_ref": TRIGGER_ID}
                        )
                    }
                )
            ]
        }
    )
    assert ValidatedTrigger.model_validate(corrected).trigger_id == TRIGGER_ID
