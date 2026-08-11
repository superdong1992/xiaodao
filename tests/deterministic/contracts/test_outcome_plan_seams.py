from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from problem_locator.contracts.enums import (
    ArtifactKind,
    CandidateMutationAction,
    CandidateStatus,
    CaseStatus,
    EvidenceSourceType,
    FieldUpdateAction,
    OutcomeDisposition,
    ResourceKind,
)
from problem_locator.contracts.models import (
    ArtifactProposal,
    CandidateMutation,
    DiagnosisStateDelta,
    EvidenceProposal,
    EvidenceSourceBinding,
    Job,
    JobOutcome,
    LogparseEvidenceLocator,
    LogparseParseParameters,
    LogparseRunMetadata,
    ReviewTargetBinding,
    SelectedSkillUpdate,
    StagedResourceRef,
    TransitionPlan,
    TreeManifest,
    TreeManifestEntry,
    VersionedRef,
    WorkspaceInputManifest,
    review_required_evidence_refs,
)
from problem_locator.contracts.outcomes import (
    validate_outcome_for_job,
    validate_transition_plan_for_outcome,
)
from problem_locator.contracts.serialization import canonical_json_sha256

from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json


def _model(name: str, model_type: type):
    return model_type.model_validate(load_json(FIXTURE_ROOT / "positive" / name))


def _empty_delta() -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=[],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def _route_plan(outcome: JobOutcome) -> TransitionPlan:
    assert outcome.payload is not None
    return TransitionPlan(
        accepted_state_delta=_empty_delta(),
        target_case_status=CaseStatus.RUNNING,
        job_updates=[],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[],
        accepted_candidate_proposal_key=None,
        selected_skill_update=SelectedSkillUpdate(
            action=FieldUpdateAction.SET,
            value=outcome.payload.skill_ref,
        ),
        case_failure_update=None,
        candidate_mutation=None,
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Apply the fixed route decision.",
    )


def _candidate_plan(outcome: JobOutcome) -> TransitionPlan:
    assert outcome.payload is not None
    candidate = outcome.payload.candidate_conclusion_draft
    assert candidate is not None
    user_result_keys = [
        artifact.proposal_key
        for artifact in outcome.proposed_artifacts
        if artifact.artifact_kind
        in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
    ]
    return TransitionPlan(
        accepted_state_delta=outcome.payload.state_delta,
        target_case_status=CaseStatus.REVIEWING,
        job_updates=[],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=user_result_keys,
        accepted_candidate_proposal_key=candidate.proposal_key,
        selected_skill_update=None,
        case_failure_update=None,
        candidate_mutation=CandidateMutation(
            action=CandidateMutationAction.INSTALL,
            candidate_binding=ReviewTargetBinding(
                existing_candidate_target=None,
                accepted_candidate_proposal_key=candidate.proposal_key,
            ),
            expected_status=None,
            target_status=CandidateStatus.REVIEWING,
            reason=None,
        ),
        next_job_spec=None,
        final_result_target=None,
        clear_active_job=True,
        reason="Install the evidence-backed candidate for review.",
    )


def test_route_outcome_skill_must_come_from_the_job_candidate_set() -> None:
    job = _model("job-route.json", Job)
    outcome = _model("job-outcome-route.json", JobOutcome)
    assert validate_outcome_for_job(job, outcome) is outcome
    assert validate_transition_plan_for_outcome(_route_plan(outcome), outcome)

    payload = outcome.model_dump(mode="python")
    payload["payload"]["skill_ref"] = VersionedRef(
        id="unavailable-skill",
        version="1.0.0",
        content_hash="9" * 64,
    )
    drifted = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="available_skill_refs"):
        validate_outcome_for_job(job, drifted)


def test_review_assessment_echoes_target_and_only_reviews_fixed_evidence() -> None:
    job = _model("job-review.json", Job)
    outcome = _model("job-outcome-review.json", JobOutcome)
    assert validate_outcome_for_job(job, outcome) is outcome

    payload = outcome.model_dump(mode="python")
    payload["payload"]["candidate_revision"] += 1
    payload["decision_audit"]["candidate_target"]["candidate_revision"] += 1
    drifted = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="review_target"):
        validate_outcome_for_job(job, drifted)


def _candidate_semantic_preimage(candidate: dict) -> dict:
    return {
        "resolution_status": candidate["resolution_status"],
        "terminal_path_id": candidate["terminal_path_id"],
        "statement": candidate["statement"],
        "causal_factors": candidate["causal_factors"],
        "candidate_factors": candidate["candidate_factors"],
        "excluded_factors": candidate["excluded_factors"],
        "supporting_evidence_refs": candidate["supporting_evidence_refs"],
        "completion_criteria_mapping": candidate["completion_criteria_mapping"],
    }


def _review_job_with_completion_mapping_only_evidence() -> Job:
    job = _model("job-review.json", Job)
    payload = job.model_dump(mode="json")
    completion_only_ref = "00000000-0000-0000-0000-000000000041"
    candidate = payload["context_snapshot"]["candidate_conclusion"]
    assert candidate is not None
    candidate["completion_criteria_mapping"][0]["evidence_refs"] = [
        completion_only_ref
    ]
    candidate["content_hash"] = canonical_json_sha256(
        _candidate_semantic_preimage(candidate)
    )
    payload["context_snapshot"]["evidence_refs"].append(completion_only_ref)
    payload["evidence_refs"].append(completion_only_ref)
    payload["review_target"]["candidate_content_hash"] = candidate["content_hash"]
    return Job.model_validate(payload)


def _review_job_with_reordered_candidate(*, include_extra: bool = False) -> Job:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-review.json")
    first_ref = "00000000-0000-0000-0000-000000000040"
    second_ref = "00000000-0000-0000-0000-000000000041"
    candidate = payload["context_snapshot"]["candidate_conclusion"]
    assert candidate is not None
    candidate["supporting_evidence_refs"] = [second_ref, first_ref]
    candidate["completion_criteria_mapping"][0]["evidence_refs"] = [
        first_ref,
        second_ref,
    ]
    candidate["content_hash"] = canonical_json_sha256(
        _candidate_semantic_preimage(candidate)
    )
    payload["context_snapshot"]["evidence_refs"] = [first_ref, second_ref]
    payload["evidence_refs"] = [first_ref, second_ref]
    if include_extra:
        extra_ref = "00000000-0000-0000-0000-000000000042"
        payload["context_snapshot"]["evidence_refs"].append(extra_ref)
        payload["evidence_refs"].append(extra_ref)
    payload["review_target"]["candidate_content_hash"] = candidate["content_hash"]
    return Job.model_validate(payload)


def _review_outcome_for_job(
    job: Job,
    *,
    audit_evidence_refs: list[str],
) -> JobOutcome:
    payload = load_json(FIXTURE_ROOT / "positive" / "job-outcome-review.json")
    assert job.review_target is not None
    payload["payload"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["candidate_target"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["required_evidence_bindings"] = [
        {"existing_evidence_id": ref, "evidence_proposal_key": None}
        for ref in audit_evidence_refs
    ]
    payload["consumed_evidence_refs"] = list(job.evidence_refs)
    payload["payload"]["reviewed_evidence_refs"] = list(job.evidence_refs)
    return JobOutcome.model_validate(payload)


def test_review_audit_uses_job_order_for_reordered_candidate_evidence() -> None:
    job = _review_job_with_reordered_candidate()
    candidate = job.context_snapshot.candidate_conclusion
    assert candidate is not None
    assert review_required_evidence_refs(candidate) == (
        "00000000-0000-0000-0000-000000000041",
        "00000000-0000-0000-0000-000000000040",
    )
    outcome = _review_outcome_for_job(
        job,
        audit_evidence_refs=list(job.evidence_refs),
    )
    assert validate_outcome_for_job(job, outcome) is outcome


@pytest.mark.parametrize("coverage", ["missing", "extra"])
def test_review_audit_rejects_inexact_candidate_evidence_coverage(
    coverage: str,
) -> None:
    job = _review_job_with_reordered_candidate(include_extra=coverage == "extra")
    audit_refs = list(job.evidence_refs)
    if coverage == "missing":
        audit_refs.pop()
    outcome = _review_outcome_for_job(job, audit_evidence_refs=audit_refs)
    with pytest.raises(ValueError, match="cover required Candidate Evidence"):
        validate_outcome_for_job(job, outcome)


def test_review_audit_rejects_duplicate_evidence_bindings() -> None:
    job = _review_job_with_reordered_candidate()
    payload = load_json(FIXTURE_ROOT / "positive" / "job-outcome-review.json")
    assert job.review_target is not None
    payload["payload"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["candidate_target"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    binding = {
        "existing_evidence_id": job.evidence_refs[0],
        "evidence_proposal_key": None,
    }
    payload["decision_audit"]["required_evidence_bindings"] = [binding, binding]
    with pytest.raises(ValidationError):
        JobOutcome.model_validate(payload)


def test_review_pass_covers_supporting_and_completion_mapping_evidence() -> None:
    job = _review_job_with_completion_mapping_only_evidence()
    outcome = _model("job-outcome-review.json", JobOutcome)
    payload = outcome.model_dump(mode="json")
    payload["payload"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["candidate_target"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["required_evidence_bindings"] = [
        {"existing_evidence_id": ref, "evidence_proposal_key": None}
        for ref in review_required_evidence_refs(
            job.context_snapshot.candidate_conclusion
        )
    ]

    missing_mapping = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="every required candidate Evidence"):
        validate_outcome_for_job(job, missing_mapping)

    required_refs = list(job.evidence_refs)
    payload["consumed_evidence_refs"] = required_refs
    payload["payload"]["reviewed_evidence_refs"] = required_refs
    complete = JobOutcome.model_validate(payload)
    assert validate_outcome_for_job(job, complete) is complete


def test_reviewed_evidence_must_equal_consumed_evidence_in_job_order() -> None:
    job = _review_job_with_completion_mapping_only_evidence()
    outcome = _model("job-outcome-review.json", JobOutcome)
    payload = outcome.model_dump(mode="json")
    payload["payload"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["candidate_target"]["candidate_content_hash"] = (
        job.review_target.candidate_content_hash
    )
    payload["decision_audit"]["required_evidence_bindings"] = [
        {"existing_evidence_id": ref, "evidence_proposal_key": None}
        for ref in review_required_evidence_refs(
            job.context_snapshot.candidate_conclusion
        )
    ]
    payload["payload"]["reviewed_evidence_refs"] = list(job.evidence_refs)

    mismatched = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="exactly equal consumed_evidence_refs"):
        validate_outcome_for_job(job, mismatched)

    reversed_refs = list(reversed(job.evidence_refs))
    payload["consumed_evidence_refs"] = reversed_refs
    payload["payload"]["reviewed_evidence_refs"] = reversed_refs
    reordered = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="fixed Job order"):
        validate_outcome_for_job(job, reordered)


def test_candidate_plan_must_accept_the_unique_user_result() -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    validate_outcome_for_job(job, outcome)
    plan = _candidate_plan(outcome)
    assert validate_transition_plan_for_outcome(plan, outcome) is plan

    invalid = plan.model_copy(update={"accepted_artifact_proposal_keys": []})
    with pytest.raises(ValueError, match="USER_RESULT"):
        validate_transition_plan_for_outcome(invalid, outcome)


def _logparse_pair(job: Job) -> tuple[EvidenceProposal, ArtifactProposal]:
    manifest_bytes = b'{"files":[]}\n'
    manifest = TreeManifest(
        version=1,
        entries=[
            TreeManifestEntry(
                path="parse_manifest.json",
                size=len(manifest_bytes),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        ],
    )
    tree_hash = canonical_json_sha256(manifest)
    staged = StagedResourceRef(
        staging_id="00000000-0000-0000-0000-000000000094",
        owner_job_id=job.job_id,
        proposal_key="logparse_run",
        resource_kind=ResourceKind.DIRECTORY,
        size=len(manifest_bytes),
        sha256=tree_hash,
        tree_manifest=manifest,
    )
    assert job.logparse_tool_ref is not None
    artifact = ArtifactProposal(
        proposal_key="logparse_run",
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="parsed-log-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        size=staged.size,
        sha256=staged.sha256,
        staged_resource_ref=staged,
        metadata=LogparseRunMetadata(
            tree_manifest_sha256=tree_hash,
            logparse_version_ref=job.logparse_tool_ref,
            parse_manifest_relative_path="parse_manifest.json",
            source_attachment_id=job.attachment_refs[0],
            source_attachment_sha256="2" * 64,
            parse_parameters=LogparseParseParameters(product=job.logparse_product),
        ),
    )
    evidence = EvidenceProposal(
        proposal_key="parsed_timeout_evidence",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=artifact.proposal_key,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="events/timeout.json",
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary="The parsed run identifies the timed-out request.",
        content_hash=None,
        staged_resource_ref=None,
    )
    return evidence, artifact


def test_bound_logparse_evidence_and_artifact_are_accepted_atomically() -> None:
    job = _model("job-diagnose.json", Job)
    outcome = _model("job-outcome-diagnosis.json", JobOutcome)
    workspace_manifest = _model(
        "workspace-input-manifest.json", WorkspaceInputManifest
    )
    evidence, artifact = _logparse_pair(job)
    payload = outcome.model_dump(mode="python")
    payload["proposed_evidence"].append(evidence)
    payload["proposed_artifacts"].append(artifact)
    expanded = JobOutcome.model_validate(payload)
    validate_outcome_for_job(job, expanded, workspace_manifest)

    plan = _candidate_plan(expanded).model_copy(
        update={"accepted_evidence_proposal_keys": [evidence.proposal_key]}
    )
    with pytest.raises(ValueError, match="accepted together"):
        validate_transition_plan_for_outcome(plan, expanded)

    accepted = plan.model_copy(
        update={
            "accepted_artifact_proposal_keys": [
                *plan.accepted_artifact_proposal_keys,
                artifact.proposal_key,
            ]
        }
    )
    assert validate_transition_plan_for_outcome(accepted, expanded) is accepted
