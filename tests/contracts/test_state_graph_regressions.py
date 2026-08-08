from __future__ import annotations

import copy
import hashlib

import pytest
from pydantic import ValidationError

from problem_locator.contracts.enums import (
    ArtifactKind,
    AttachmentStatus,
    CandidateStatus,
    CaseStatus,
    DiagnosisItemStatus,
    DiagnosisProvenanceType,
    EvidenceSourceType,
    JobStatus,
    JobType,
    OutcomeDisposition,
    ResourceKind,
)
from problem_locator.contracts.limits import CONTRACT_REVISION, SCHEMA_VERSION
from problem_locator.contracts.models import (
    CaseAggregate,
    CaseView,
    JobOutcome,
    StateExport,
    StateFile,
    StateMutation,
    ValidationReport,
)
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.contracts._support import FIXTURE_ROOT, load_json


CASE_ID = "00000000-0000-0000-0000-000000000001"
SECOND_CASE_ID = "00000000-0000-0000-0000-000000000002"
ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000010"
SECOND_JOB_ID = "00000000-0000-0000-0000-000000000019"
ROUTE_OUTCOME_ID = "00000000-0000-0000-0000-000000000020"
FACT_ID = "00000000-0000-0000-0000-000000000031"
EVIDENCE_ID = "00000000-0000-0000-0000-000000000040"
NOW = "2026-07-31T00:00:00.000Z"
LATER = "2026-07-31T00:00:30.000Z"


def _state_payload() -> dict:
    return load_json(FIXTURE_ROOT / "positive" / "state.json")


def _aggregate_payload() -> dict:
    return copy.deepcopy(_state_payload()["cases"][CASE_ID])


def _user_fact() -> dict:
    return {
        "item_id": FACT_ID,
        "statement": "The caller service is payment-service.",
        "status": DiagnosisItemStatus.ACTIVE,
        "provenance": {
            "source_type": DiagnosisProvenanceType.USER_INPUT,
            "source_ref": "00000000-0000-0000-0000-000000000090",
            "input_name": "caller_service",
        },
        "evidence_refs": [],
        "created_revision": 1,
        "supersedes": [],
    }


def _user_fact_evidence() -> dict:
    return {
        "evidence_id": EVIDENCE_ID,
        "case_id": CASE_ID,
        "source_type": EvidenceSourceType.USER_FACT,
        "source_ref": FACT_ID,
        "locator": {"kind": "USER_FACT", "input_name": "caller_service"},
        "summary": "Caller service supplied by the user.",
        "collected_at": NOW,
        "content_hash": None,
        "resource_ref": None,
    }


def _aggregate_with_user_fact_evidence() -> dict:
    aggregate = _aggregate_payload()
    aggregate["case"]["diagnosis_state"]["user_facts"] = [_user_fact()]
    aggregate["case"]["diagnosis_state"]["evidence_refs"] = [EVIDENCE_ID]
    aggregate["evidence"] = {EVIDENCE_ID: _user_fact_evidence()}
    return aggregate


def _aggregate_with_route_processing() -> dict:
    aggregate = _aggregate_payload()
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
            "processed_at": LATER,
            "error_code": None,
            "accepted_evidence_ids": [],
            "accepted_artifact_ids": [],
            "created_job_id": None,
            "reason": "The route result was committed.",
        }
    }
    return aggregate


def _empty_mutation_payload() -> dict:
    return {
        "upsert_case": None,
        "upsert_runtime_epoch_records": [],
        "upsert_recovery_processing_records": [],
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


def _job_summary(job: dict) -> dict:
    return {
        name: job[name]
        for name in (
            "job_id",
            "job_type",
            "status",
            "goal",
            "base_state_revision",
            "created_at",
            "started_at",
            "finished_at",
        )
    }


def _running_case_view_payload() -> dict:
    aggregate = _aggregate_payload()
    case = aggregate["case"]
    diagnosis = case["diagnosis_state"]
    active_job = aggregate["jobs"][ROUTE_JOB_ID]
    return {
        "case_id": case["case_id"],
        "status": case["status"],
        "case_revision": case["case_revision"],
        "diagnosis_state_revision": diagnosis["revision"],
        "problem_spec": diagnosis["problem_spec"],
        "user_facts": diagnosis["user_facts"],
        "confirmed_facts": diagnosis["confirmed_facts"],
        "open_questions": diagnosis["open_questions"],
        "pending_requirements": diagnosis["pending_requirements"],
        "active_job": _job_summary(active_job),
        "selected_skill_ref": case["selected_skill_ref"],
        "final_result": None,
        "failure": None,
        "artifacts": [],
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
    }


def _resolved_case_view_payload() -> dict:
    review_job = load_json(FIXTURE_ROOT / "positive" / "job-review.json")
    candidate = copy.deepcopy(review_job["context_snapshot"]["candidate_conclusion"])
    candidate["status"] = CandidateStatus.ACCEPTED
    return {
        "case_id": CASE_ID,
        "status": CaseStatus.RESOLVED,
        "case_revision": 4,
        "diagnosis_state_revision": review_job["base_state_revision"],
        "problem_spec": review_job["context_snapshot"]["problem_spec"],
        "user_facts": review_job["context_snapshot"]["user_facts"],
        "confirmed_facts": review_job["context_snapshot"]["confirmed_facts"],
        "open_questions": review_job["context_snapshot"]["open_questions"],
        "pending_requirements": review_job["context_snapshot"][
            "pending_requirements"
        ],
        "active_job": None,
        "selected_skill_ref": review_job["skill_ref"],
        "final_result": candidate,
        "failure": None,
        "artifacts": [
            {
                "artifact_id": "00000000-0000-0000-0000-000000000070",
                "kind": ArtifactKind.USER_RESULT,
                "name": "diagnosis.json",
                "content_type": "application/json",
                "resource_kind": ResourceKind.FILE,
                "size": 512,
                "sha256": "7" * 64,
                "created_by_job_id": candidate["proposed_by_job_id"],
                "created_at": LATER,
                "downloadable": True,
            }
        ],
        "created_at": NOW,
        "updated_at": LATER,
    }


def _attachment(attachment_id: str, storage_key: str, fill: str) -> dict:
    digest = fill * 64
    return {
        "attachment_id": attachment_id,
        "case_id": CASE_ID,
        "status": AttachmentStatus.READY,
        "name": f"{attachment_id}.log",
        "content_type": "text/plain",
        "declared_size": 16,
        "declared_sha256": digest,
        "size": 16,
        "sha256": digest,
        "storage_key": storage_key,
        "created_at": NOW,
        "updated_at": LATER,
    }


def _state_export_payload() -> dict:
    state_payload = _state_payload()
    attachments = [
        _attachment(
            "00000000-0000-0000-0000-000000000050",
            f"cases/{CASE_ID}/attachments/00000000-0000-0000-0000-000000000050/payload",
            "5",
        ),
        _attachment(
            "00000000-0000-0000-0000-000000000051",
            f"cases/{CASE_ID}/attachments/00000000-0000-0000-0000-000000000051/payload",
            "6",
        ),
    ]
    state_payload["cases"][CASE_ID]["attachments"] = {
        attachment["attachment_id"]: attachment for attachment in attachments
    }
    resources = [
        {
            "resource_kind": ResourceKind.FILE,
            "storage_key": attachment["storage_key"],
            "size": attachment["size"],
            "sha256": attachment["sha256"],
        }
        for attachment in attachments
    ]
    return {
        "export_schema_version": 2,
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "source_generation": state_payload["generation"],
        "installation_id": state_payload["installation_id"],
        "object_counts": _object_counts(attachments=2),
        "state": state_payload,
        "resources": resources,
    }


def _object_counts(**updates: int) -> dict:
    counts = {
        "cases": 1,
        "jobs": 1,
        "outcomes": 0,
        "outcome_processing_records": 0,
        "execution_failure_records": 0,
        "attachments": 0,
        "evidence": 0,
        "artifacts": 0,
        "idempotency_records": 0,
        "runtime_epochs": 0,
        "recovery_processing_records": 0,
    }
    counts.update(updates)
    return counts


def test_case_aggregate_accepts_a_fully_resolved_typed_evidence_source() -> None:
    aggregate = CaseAggregate.model_validate(_aggregate_with_user_fact_evidence())

    assert aggregate.evidence[EVIDENCE_ID].source_ref == FACT_ID


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("diagnosis", "dangling Evidence"),
        ("job_artifact", "dangling Artifact"),
        ("previous_outcome", "dangling previous Outcome"),
    ],
)
def test_case_aggregate_rejects_dangling_graph_edges(
    target: str,
    message: str,
) -> None:
    aggregate = _aggregate_payload()
    missing = "00000000-0000-0000-0000-000000000098"
    if target == "diagnosis":
        aggregate["case"]["diagnosis_state"]["evidence_refs"] = [missing]
    elif target == "job_artifact":
        aggregate["jobs"][ROUTE_JOB_ID]["artifact_refs"] = [missing]
    else:
        aggregate["jobs"][ROUTE_JOB_ID]["previous_outcome_refs"] = [missing]

    with pytest.raises(ValidationError, match=message):
        CaseAggregate.model_validate(aggregate)


@pytest.mark.parametrize("drift", ["wrong_source_object", "wrong_input_name"])
def test_case_aggregate_rejects_semantically_wrong_typed_sources(drift: str) -> None:
    aggregate = _aggregate_with_user_fact_evidence()
    evidence = aggregate["evidence"][EVIDENCE_ID]
    if drift == "wrong_source_object":
        evidence["source_ref"] = ROUTE_JOB_ID
    else:
        evidence["locator"]["input_name"] = "wrong_input"

    with pytest.raises(ValidationError, match="USER_FACT"):
        CaseAggregate.model_validate(aggregate)


def test_case_aggregate_active_job_must_resolve_and_be_live() -> None:
    missing = _aggregate_payload()
    missing["jobs"] = {}
    with pytest.raises(ValidationError, match="active_job_id must resolve"):
        CaseAggregate.model_validate(missing)

    terminal = _aggregate_payload()
    job = terminal["jobs"][ROUTE_JOB_ID]
    job["status"] = JobStatus.SUCCEEDED
    job["finished_at"] = LATER
    with pytest.raises(ValidationError, match="PENDING or RUNNING"):
        CaseAggregate.model_validate(terminal)


def test_case_aggregate_accepts_a_canonical_processing_record() -> None:
    aggregate = CaseAggregate.model_validate(_aggregate_with_route_processing())

    assert (
        aggregate.outcome_processing_records[ROUTE_OUTCOME_ID].disposition
        is OutcomeDisposition.APPLIED
    )


def test_case_aggregate_rejects_completed_outcome_without_processing_record() -> None:
    aggregate = _aggregate_with_route_processing()
    aggregate["outcome_processing_records"] = {}
    aggregate["case"]["status"] = CaseStatus.WAITING_INPUT
    aggregate["case"]["active_job_id"] = None
    aggregate["jobs"][ROUTE_JOB_ID]["status"] = JobStatus.SUCCEEDED
    aggregate["jobs"][ROUTE_JOB_ID]["finished_at"] = LATER

    with pytest.raises(ValidationError):
        CaseAggregate.model_validate(aggregate)


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("missing_outcome", "technical REJECTED processing"),
        ("wrong_saved_hash", "canonical saved Outcome"),
        ("header_mismatch", "headers must match"),
    ],
)
def test_case_aggregate_rejects_incoherent_processing_records(
    drift: str,
    message: str,
) -> None:
    aggregate = _aggregate_with_route_processing()
    if drift == "missing_outcome":
        aggregate["outcomes"] = {}
    elif drift == "wrong_saved_hash":
        record = aggregate["outcome_processing_records"][ROUTE_OUTCOME_ID]
        record["outcome_hash"] = "9" * 64
        record["outcome_file_ref"]["sha256"] = "9" * 64
    else:
        job = aggregate["jobs"][ROUTE_JOB_ID]
        job["base_state_revision"] = 2
        job["context_snapshot"]["diagnosis_state_revision"] = 2

    with pytest.raises(ValidationError, match=message):
        CaseAggregate.model_validate(aggregate)


def test_empty_state_mutation_is_valid() -> None:
    assert StateMutation.model_validate(_empty_mutation_payload()).insert_jobs == []


@pytest.mark.parametrize("duplicate_kind", ["runtime_epoch", "job"])
def test_state_mutation_rejects_duplicate_natural_ids(duplicate_kind: str) -> None:
    mutation = _empty_mutation_payload()
    if duplicate_kind == "runtime_epoch":
        record = {
            "runtime_epoch": "00000000-0000-0000-0000-000000000080",
            "started_at": NOW,
            "recovery_id": "00000000-0000-0000-0000-000000000081",
            "recovery_completed_at": None,
        }
        mutation["upsert_runtime_epoch_records"] = [record, copy.deepcopy(record)]
    else:
        job = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
        mutation["insert_jobs"] = [job, copy.deepcopy(job)]

    with pytest.raises(ValidationError, match="must not repeat natural IDs"):
        StateMutation.model_validate(mutation)


def test_state_mutation_cannot_insert_and_update_the_same_job() -> None:
    mutation = _empty_mutation_payload()
    mutation["insert_jobs"] = [
        load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    ]
    mutation["job_lifecycle_updates"] = [
        {
            "job_id": ROUTE_JOB_ID,
            "expected_status": JobStatus.PENDING,
            "target_status": JobStatus.RUNNING,
            "started_at": NOW,
            "finished_at": None,
            "runtime_epoch": "00000000-0000-0000-0000-000000000080",
        }
    ]

    with pytest.raises(ValidationError, match="insert and lifecycle-update"):
        StateMutation.model_validate(mutation)


def test_state_file_accepts_distinct_ids_across_cases() -> None:
    state = _state_payload()
    second = copy.deepcopy(state["cases"][CASE_ID])
    second["case"]["case_id"] = SECOND_CASE_ID
    second["case"]["active_job_id"] = SECOND_JOB_ID
    second_job = second["jobs"].pop(ROUTE_JOB_ID)
    second_job["job_id"] = SECOND_JOB_ID
    second_job["case_id"] = SECOND_CASE_ID
    second["jobs"][SECOND_JOB_ID] = second_job
    state["cases"][SECOND_CASE_ID] = second

    assert len(StateFile.model_validate(state).cases) == 2


def test_state_file_rejects_cross_case_object_id_reuse() -> None:
    state = _state_payload()
    second = copy.deepcopy(state["cases"][CASE_ID])
    second["case"]["case_id"] = SECOND_CASE_ID
    second["jobs"][ROUTE_JOB_ID]["case_id"] = SECOND_CASE_ID
    state["cases"][SECOND_CASE_ID] = second

    with pytest.raises(ValidationError, match="globally unique jobs IDs"):
        StateFile.model_validate(state)


def test_running_case_view_accepts_a_live_non_review_job() -> None:
    view = CaseView.model_validate(_running_case_view_payload())

    assert view.active_job is not None
    assert view.active_job.job_type is JobType.ROUTE


@pytest.mark.parametrize("drift", ["missing_active", "active_while_waiting", "terminal_active"])
def test_case_view_active_job_matches_the_case_status(drift: str) -> None:
    view = _running_case_view_payload()
    if drift == "missing_active":
        view["active_job"] = None
    elif drift == "active_while_waiting":
        view["status"] = CaseStatus.WAITING_INPUT
    else:
        view["active_job"]["status"] = JobStatus.SUCCEEDED
        view["active_job"]["finished_at"] = LATER

    with pytest.raises(ValidationError, match="active_job|PENDING or RUNNING"):
        CaseView.model_validate(view)


@pytest.mark.parametrize(
    ("collection", "source_type", "evidence_refs", "message"),
    [
        ("user_facts", DiagnosisProvenanceType.AGENT_OUTCOME, [], "USER_INPUT"),
        (
            "confirmed_facts",
            DiagnosisProvenanceType.USER_INPUT,
            [EVIDENCE_ID],
            "AGENT_OUTCOME",
        ),
        (
            "confirmed_facts",
            DiagnosisProvenanceType.AGENT_OUTCOME,
            [],
            "must cite Evidence",
        ),
    ],
)
def test_case_view_rejects_invalid_item_provenance_or_evidence(
    collection: str,
    source_type: DiagnosisProvenanceType,
    evidence_refs: list[str],
    message: str,
) -> None:
    view = _running_case_view_payload()
    item = _user_fact()
    item["provenance"]["source_type"] = source_type
    if source_type is DiagnosisProvenanceType.AGENT_OUTCOME:
        item["provenance"]["input_name"] = None
    item["evidence_refs"] = evidence_refs
    view[collection] = [item]

    with pytest.raises(ValidationError, match=message):
        CaseView.model_validate(view)


def test_resolved_case_view_accepts_exact_final_mapping_and_user_result() -> None:
    view = CaseView.model_validate(_resolved_case_view_payload())

    assert view.final_result is not None
    assert view.final_result.status is CandidateStatus.ACCEPTED
    assert view.artifacts[0].created_by_job_id == view.final_result.proposed_by_job_id


@pytest.mark.parametrize("drift", ["criterion", "user_result_owner"])
def test_resolved_case_view_rejects_final_projection_drift(drift: str) -> None:
    view = _resolved_case_view_payload()
    if drift == "criterion":
        view["problem_spec"]["completion_criteria"] = ["A different criterion."]
    else:
        view["artifacts"][0]["created_by_job_id"] = ROUTE_JOB_ID

    with pytest.raises(ValidationError, match="final result|USER_RESULT"):
        CaseView.model_validate(view)


def test_state_export_accepts_exact_counts_and_sorted_formal_resources() -> None:
    export = StateExport.model_validate(_state_export_payload())

    assert export.object_counts.attachments == 2
    assert [item.storage_key for item in export.resources] == sorted(
        item.storage_key for item in export.resources
    )


@pytest.mark.parametrize("drift", ["counts", "missing_resource", "unsorted_resources"])
def test_state_export_rejects_count_or_resource_projection_drift(drift: str) -> None:
    export = _state_export_payload()
    if drift == "counts":
        export["object_counts"]["attachments"] = 1
    elif drift == "missing_resource":
        export["resources"].pop()
    else:
        export["resources"].reverse()

    with pytest.raises(ValidationError, match="object_counts|resources"):
        StateExport.model_validate(export)


def test_valid_validation_report_requires_the_frozen_state_envelope() -> None:
    report = ValidationReport.model_validate(
        {
            "valid": True,
            "schema_version": SCHEMA_VERSION,
            "contract_revision": CONTRACT_REVISION,
            "generation": 1,
            "object_counts": _object_counts(),
            "errors": [],
        }
    )

    assert report.valid is True
    assert report.errors == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 0),
        ("contract_revision", "v1-contract-drift"),
        ("generation", None),
    ],
)
def test_valid_validation_report_rejects_envelope_drift(
    field: str,
    value: object,
) -> None:
    payload = {
        "valid": True,
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generation": 1,
        "object_counts": _object_counts(),
        "errors": [],
    }
    payload[field] = value

    with pytest.raises(ValidationError, match="frozen StateFile envelope"):
        ValidationReport.model_validate(payload)


def test_invalid_validation_report_requires_errors_in_deterministic_order() -> None:
    issue = {
        "code": "DANGLING_REFERENCE",
        "object_type": "Case",
        "object_id": CASE_ID,
        "field_path": "/diagnosis_state/evidence_refs/0",
        "message": "The Evidence reference does not resolve.",
    }
    report = ValidationReport.model_validate(
        {
            "valid": False,
            "schema_version": None,
            "contract_revision": None,
            "generation": None,
            "object_counts": _object_counts(cases=0, jobs=0),
            "errors": [issue],
        }
    )
    assert report.errors[0].code == "DANGLING_REFERENCE"

    no_errors = report.model_dump(mode="python")
    no_errors["errors"] = []
    with pytest.raises(ValidationError, match="valid must be true"):
        ValidationReport.model_validate(no_errors)

    unsorted = report.model_dump(mode="python")
    unsorted["errors"] = [
        {
            "code": "JOB_ERROR",
            "object_type": "Job",
            "object_id": ROUTE_JOB_ID,
            "field_path": "/status",
            "message": "Invalid status.",
        },
        issue,
    ]
    with pytest.raises(ValidationError, match="deterministic order"):
        ValidationReport.model_validate(unsorted)
