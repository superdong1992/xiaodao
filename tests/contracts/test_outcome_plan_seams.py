from __future__ import annotations

import hashlib

import pytest

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
)
from problem_locator.contracts.outcomes import (
    validate_outcome_for_job,
    validate_transition_plan_for_outcome,
)
from problem_locator.contracts.serialization import canonical_json_sha256

from tests.contracts._support import FIXTURE_ROOT, load_json


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
    user_result_key = next(
        artifact.proposal_key
        for artifact in outcome.proposed_artifacts
        if artifact.artifact_kind is ArtifactKind.USER_RESULT
    )
    return TransitionPlan(
        accepted_state_delta=outcome.payload.state_delta,
        target_case_status=CaseStatus.REVIEWING,
        job_updates=[],
        outcome_disposition=OutcomeDisposition.APPLIED,
        accepted_evidence_proposal_keys=[],
        accepted_artifact_proposal_keys=[user_result_key],
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
    drifted = JobOutcome.model_validate(payload)
    with pytest.raises(ValueError, match="review_target"):
        validate_outcome_for_job(job, drifted)


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
