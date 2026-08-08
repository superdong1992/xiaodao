from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from problem_locator.application.projection import is_artifact_downloadable
from problem_locator.contracts import (
    Artifact,
    ArtifactKind,
    AuditBundleMetadata,
    CandidateStatus,
    Case,
    CaseStatus,
    DiagnosisOutcomeTriggerPayload,
    DiagnosisItem,
    DiagnosisItemStatus,
    DiagnosisProvenance,
    DiagnosisProvenanceType,
    DiagnosisState,
    Job,
    JobOutcome,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ReviewOutcomeTriggerPayload,
    ReviewVerdict,
    SupplementPolicy,
    StateFile,
    TriggerType,
    UnresolvedReasonCode,
    UnresolvedResult,
    UserResultMetadata,
)
from problem_locator.domain import DomainCoordinator

from ._builders import (
    continuation,
    rebuild,
    snapshot_with_active,
    trigger,
    unresolved_user_result_proposal,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "contracts" / "positive"


def _json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _job(name: str) -> Job:
    return Job.model_validate(_json(name))


def _state(job: Job) -> DiagnosisState:
    snapshot = job.context_snapshot
    return DiagnosisState(
        revision=snapshot.diagnosis_state_revision,
        problem_spec=snapshot.problem_spec,
        user_facts=snapshot.user_facts,
        confirmed_facts=snapshot.confirmed_facts,
        active_hypotheses=snapshot.active_hypotheses,
        rejected_hypotheses=snapshot.rejected_hypotheses,
        open_questions=snapshot.open_questions,
        pending_requirements=snapshot.pending_requirements,
        evidence_refs=snapshot.evidence_refs,
        candidate_conclusion=snapshot.candidate_conclusion,
    )


def _binding(evidence_id: str) -> dict[str, object]:
    return {
        "existing_evidence_id": evidence_id,
        "evidence_proposal_key": None,
    }


def _decision_audit(
    job: Job,
    *,
    claim: str,
    server_status: str,
) -> dict[str, object]:
    evidence_binding = _binding(job.evidence_refs[0])
    is_review = job.job_type.value == "REVIEW"
    return {
        "schema_version": 2,
        "job_id": job.job_id,
        "case_id": job.case_id,
        "job_type": job.job_type.value,
        "skill_ref": job.skill_ref.model_dump(mode="json"),
        "source_draft_sha256": "1" * 64,
        "subject_hash": "2" * 64,
        "candidate_target": (
            job.review_target.model_dump(mode="json") if is_review else None
        ),
        "diagnosis_audit_hash": "3" * 64 if is_review else None,
        "required_rule_ids": ["causal_chain"],
        "required_evidence_bindings": [evidence_binding],
        "rules": [
            {
                "rule_id": "causal_chain",
                "agent_claim": {
                    "rule_id": "causal_chain",
                    "claimed_result": claim,
                    "fact_refs": [],
                    "citations": [],
                    "explanation": "Explicit independent rule assessment.",
                },
                "server_evaluation": {
                    "rule_id": "causal_chain",
                    "rule_kind": "SEMANTIC_CAUSALITY",
                    "status": server_status,
                    "fact_refs": [],
                    "evidence_bindings": [evidence_binding],
                    "anchor_id": None,
                    "derived_anchor_time": None,
                    "observed_times": [],
                    "line_ranges": [],
                    "issues": (
                        ["The required event is outside the declared window."]
                        if server_status in {"VERIFIED_FAIL", "UNVERIFIABLE"}
                        else []
                    ),
                },
            }
        ],
    }


def _review_outcome(job: Job, verdict: ReviewVerdict) -> JobOutcome:
    payload = _json("job-outcome-review.json")
    assessment = payload["payload"]
    assert isinstance(assessment, dict)
    assessment["verdict"] = verdict.value
    assessment["recommendation"] = "Create a new Case after checking the inputs."
    if verdict is ReviewVerdict.REJECT:
        assessment["unsupported_findings"] = ["Causality is not established."]
    payload["decision_audit"] = _decision_audit(
        job,
        claim="FAIL" if verdict is not ReviewVerdict.PASS else "PASS",
        server_status="SEMANTIC_ONLY",
    )
    if verdict is not ReviewVerdict.PASS:
        payload["proposed_artifacts"] = [
            unresolved_user_result_proposal(job).model_dump(mode="python")
        ]
    return JobOutcome.model_validate(payload)


def test_diagnosis_inconclusive_terminates_unresolved_without_candidate() -> None:
    job = _job("job-diagnose.json")
    payload = _json("job-outcome-diagnosis.json")
    diagnosis = payload["payload"]
    assert isinstance(diagnosis, dict)
    diagnosis["candidate_conclusion_draft"] = None
    diagnosis["recommended_next_step"] = "Check the supplied time and create a new Case."
    payload["proposed_artifacts"] = [
        unresolved_user_result_proposal(job).model_dump(mode="python")
    ]
    payload["result_type"] = OutcomeResultType.INCONCLUSIVE.value
    payload["decision_audit"] = _decision_audit(
        job,
        claim="PASS",
        server_status="VERIFIED_FAIL",
    )
    outcome = JobOutcome.model_validate(payload)
    snapshot = snapshot_with_active(job)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.DIAGNOSIS_OUTCOME,
        payload=DiagnosisOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert plan.target_case_status is CaseStatus.UNRESOLVED
    assert plan.next_job_spec is None
    assert plan.accepted_candidate_proposal_key is None
    assert plan.unresolved_result_draft is not None
    assert plan.unresolved_result_draft.user_result_proposal_key == "user_result"
    assert plan.accepted_artifact_proposal_keys == ["user_result"]
    assert (
        plan.unresolved_result_draft.reason_code
        is UnresolvedReasonCode.MECHANICAL_VERIFICATION_FAILED
    )


def test_review_rejects_candidate_and_does_not_start_diagnosis() -> None:
    job = _job("job-review.json")
    outcome = _review_outcome(job, ReviewVerdict.REJECT)
    snapshot = snapshot_with_active(job)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert plan.target_case_status is CaseStatus.UNRESOLVED
    assert plan.next_job_spec is None
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.target_status is CandidateStatus.REJECTED
    assert plan.unresolved_result_draft is not None
    assert (
        plan.unresolved_result_draft.reason_code
        is UnresolvedReasonCode.SEMANTIC_REVIEW_REJECTED
    )


def test_review_need_more_waits_only_for_one_missing_only_requirement() -> None:
    job = _job("job-review.json")
    requirement = PendingRequirement.model_validate(
        {
            "requirement_id": "00000000-0000-0000-0000-000000000099",
            "kind": RequirementKind.INPUT,
            "name": "region",
            "prompt": "Provide the missing region.",
            "required": True,
            "constraints": {
                "value_type": "STRING",
                "min_utf8_bytes": 1,
                "max_utf8_bytes": 64,
                "pattern": None,
                "allowed_values": [],
            },
            "status": RequirementStatus.OPEN,
            "requested_by_job_id": (
                job.context_snapshot.candidate_conclusion.proposed_by_job_id
            ),
            "fulfilled_by_refs": [],
            "supplement_policy": SupplementPolicy.MISSING_ONLY,
        }
    )
    context = rebuild(job.context_snapshot, pending_requirements=[requirement])
    job = rebuild(job, context_snapshot=context)
    payload = _json("job-outcome-review.json")
    assessment = payload["payload"]
    assert isinstance(assessment, dict)
    assessment.update(
        verdict=ReviewVerdict.NEED_MORE_EVIDENCE.value,
        missing_evidence=["Region is missing."],
        recommendation="Provide the missing region.",
        requested_requirement_ids=[requirement.requirement_id],
    )
    payload["decision_audit"] = _decision_audit(
        job,
        claim="UNKNOWN",
        server_status="SEMANTIC_ONLY",
    )
    payload["proposed_artifacts"] = [
        unresolved_user_result_proposal(job).model_dump(mode="python")
    ]
    outcome = JobOutcome.model_validate(payload)
    snapshot = snapshot_with_active(job)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert plan.target_case_status is CaseStatus.WAITING_INPUT
    assert plan.next_job_spec is None
    assert plan.candidate_mutation is not None
    assert plan.candidate_mutation.target_status is CandidateStatus.REJECTED
    assert plan.accepted_artifact_proposal_keys == []


def test_generic_review_need_more_terminates_unresolved() -> None:
    job = _job("job-review.json")
    payload = _json("job-outcome-review.json")
    assessment = payload["payload"]
    assert isinstance(assessment, dict)
    assessment.update(
        verdict=ReviewVerdict.NEED_MORE_EVIDENCE.value,
        missing_evidence=["More evidence might help."],
        recommendation="Create a new Case after checking the supplied facts.",
        requested_requirement_ids=[],
    )
    payload["decision_audit"] = _decision_audit(
        job,
        claim="UNKNOWN",
        server_status="SEMANTIC_ONLY",
    )
    payload["proposed_artifacts"] = [
        unresolved_user_result_proposal(job).model_dump(mode="python")
    ]
    outcome = JobOutcome.model_validate(payload)
    snapshot = snapshot_with_active(job)
    request = trigger(
        snapshot,
        trigger_type=TriggerType.REVIEW_OUTCOME,
        payload=ReviewOutcomeTriggerPayload(job_outcome=outcome),
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=job,
        ),
        occurred_at=outcome.produced_at,
    )

    plan = DomainCoordinator().plan(snapshot, request)

    assert plan.target_case_status is CaseStatus.UNRESOLVED
    assert plan.next_job_spec is None
    assert plan.unresolved_result_draft is not None
    assert plan.unresolved_result_draft.user_result_proposal_key == "user_result"
    assert plan.accepted_artifact_proposal_keys == ["user_result"]
    assert (
        plan.unresolved_result_draft.reason_code
        is UnresolvedReasonCode.INVALID_NEED_MORE_REQUEST
    )


def test_one_user_fact_per_input_name_is_a_state_invariant() -> None:
    job = _job("job-diagnose.json")
    state = _state(job)
    fact = DiagnosisItem(
        item_id="00000000-0000-0000-0000-000000000095",
        statement="2026-07-31T00:00:00.000Z",
        status=DiagnosisItemStatus.ACTIVE,
        provenance=DiagnosisProvenance(
            source_type=DiagnosisProvenanceType.USER_INPUT,
            source_ref="00000000-0000-0000-0000-000000000094",
            input_name="problem_time",
        ),
        evidence_refs=[],
        created_revision=state.revision,
        supersedes=[],
    )
    duplicate = rebuild(
        fact,
        item_id="00000000-0000-0000-0000-000000000098",
    )

    with pytest.raises(ValidationError, match="active user fact input_name"):
        rebuild(state, user_facts=[fact, duplicate])


def test_only_matching_unresolved_result_artifacts_are_downloadable() -> None:
    job = _job("job-review.json")
    state = _state(job)
    candidate = state.candidate_conclusion
    assert candidate is not None
    rejected = rebuild(candidate, status=CandidateStatus.REJECTED)
    state = rebuild(state, candidate_conclusion=rejected)
    audit_id = "00000000-0000-0000-0000-000000000097"
    user_result_id = "00000000-0000-0000-0000-000000000095"
    case = Case(
        case_id=job.case_id,
        status=CaseStatus.UNRESOLVED,
        case_revision=4,
        diagnosis_state=state,
        active_job_id=None,
        selected_skill_ref=job.skill_ref,
        final_result=None,
        unresolved_result=UnresolvedResult(
            source_job_id=job.job_id,
            source_outcome_id="00000000-0000-0000-0000-000000000022",
            reason_code=UnresolvedReasonCode.SEMANTIC_REVIEW_REJECTED,
            summary="Independent review did not confirm the candidate.",
            blocking_rule_ids=["causal_chain"],
            evidence_refs=job.evidence_refs,
            user_result_artifact_id=user_result_id,
            recommended_next_step="Create a new Case after correcting the inputs.",
            occurred_at="2026-07-31T00:02:30.000Z",
            audit_artifact_id=audit_id,
        ),
        failure=None,
        created_at="2026-07-31T00:00:00.000Z",
        updated_at="2026-07-31T00:02:30.000Z",
    )
    audit = Artifact(
        artifact_id=audit_id,
        case_id=case.case_id,
        kind=ArtifactKind.AUDIT_BUNDLE,
        name="problem-locator-audit.zip",
        content_type="application/zip",
        resource_kind="FILE",
        size=128,
        sha256="4" * 64,
        storage_key=(
            f"resources/cases/{case.case_id}/artifacts/{audit_id}/payload"
        ),
        metadata=AuditBundleMetadata(
            schema_version=1,
            format_id="problem-locator-audit-bundle-v1",
            description="Deterministic unresolved audit bundle.",
            case_id=case.case_id,
            source_job_id=job.job_id,
            source_outcome_id=case.unresolved_result.source_outcome_id,
        ),
        created_by_job_id=job.job_id,
        created_at=case.updated_at,
    )
    user_result = Artifact(
        artifact_id=user_result_id,
        case_id=case.case_id,
        kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind="FILE",
        size=1604,
        sha256="5" * 64,
        storage_key=(
            f"resources/cases/{case.case_id}/artifacts/{user_result_id}/payload"
        ),
        metadata=UserResultMetadata(
            schema_version=2,
            format_id="problem-locator-diagnosis-v2",
            description="Canonical unresolved diagnosis result.",
        ),
        created_by_job_id=job.job_id,
        created_at=case.updated_at,
    )

    assert is_artifact_downloadable(case, audit) is True
    assert is_artifact_downloadable(case, user_result) is True
    assert is_artifact_downloadable(
        case,
        rebuild(
            audit,
            artifact_id="00000000-0000-0000-0000-000000000096",
            storage_key=(
                "resources/cases/00000000-0000-0000-0000-000000000001/"
                "artifacts/00000000-0000-0000-0000-000000000096/payload"
            ),
        ),
    ) is False
    assert is_artifact_downloadable(
        case,
        rebuild(
            user_result,
            artifact_id="00000000-0000-0000-0000-000000000094",
            storage_key=(
                "resources/cases/00000000-0000-0000-0000-000000000001/"
                "artifacts/00000000-0000-0000-0000-000000000094/payload"
            ),
        ),
    ) is False


def test_v1_state_marker_is_rejected_without_migration() -> None:
    with pytest.raises(ValidationError):
        StateFile.model_validate(
            {
                "schema_version": 1,
                "contract_revision": "v1-contract-r4",
                "generation": 1,
                "installation_id": "00000000-0000-0000-0000-000000000001",
                "created_at": "2026-07-31T00:00:00.000Z",
                "updated_at": "2026-07-31T00:00:00.000Z",
                "runtime_epochs": [],
                "recovery_processing_records": {},
                "cases": {},
                "idempotency_records": {},
            }
        )
