from __future__ import annotations

from collections.abc import Iterable, Sequence

from problem_locator.contracts import (
    Job,
    ResolvedLogparseAnchor,
    ResolvedLogparsePlanInput,
    ReviewCausalAssertion,
    ReviewSubjectV2,
    canonical_json_sha256,
    review_required_evidence_refs,
)


def resolved_logparse_plan(
    job: Job,
    *,
    problem_time: str,
    anchors: Sequence[ResolvedLogparseAnchor | dict[str, object]],
) -> ResolvedLogparsePlanInput:
    """Build the public V2 projection of a server-resolved test plan."""

    if job.artifact_refs:
        attachment_id = None
        artifact_id = job.artifact_refs[0]
    elif job.attachment_refs:
        attachment_id = job.attachment_refs[0]
        artifact_id = None
    else:
        raise ValueError("a logparse test Job requires an attachment or run artifact")
    return ResolvedLogparsePlanInput(
        schema_version=2,
        attachment_id=attachment_id,
        artifact_id=artifact_id,
        problem_time=problem_time,
        anchors=[
            item
            if isinstance(item, ResolvedLogparseAnchor)
            else ResolvedLogparseAnchor.model_validate(item)
            for item in anchors
        ],
    )


def blind_review_subject(
    job: Job,
    *,
    rule_ids: Iterable[str] = ("causal_chain",),
) -> ReviewSubjectV2:
    """Build a deterministic blind ReviewSubject for cross-object test seams."""

    if job.skill_ref is None or job.context_snapshot.candidate_conclusion is None:
        raise ValueError("a REVIEW test Job requires a pinned Skill and Candidate")
    candidate = job.context_snapshot.candidate_conclusion
    candidate_evidence = set(review_required_evidence_refs(candidate))
    required_evidence_refs = [
        ref for ref in job.evidence_refs if ref in candidate_evidence
    ]
    required_rule_ids = list(rule_ids)
    preimage = {
        "schema_version": 2,
        "review_job_id": job.job_id,
        "case_id": job.case_id,
        "reviewed_state_revision": job.base_state_revision,
        "skill_ref": job.skill_ref.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "causal_assertions": [
            ReviewCausalAssertion(
                rule_id=rule_id,
                statement=f"Independently assess rule {rule_id}.",
            ).model_dump(mode="json")
            for rule_id in required_rule_ids
        ],
        "required_rule_ids": required_rule_ids,
        "required_evidence_refs": required_evidence_refs,
        "mechanical_facts": [],
    }
    return ReviewSubjectV2.model_validate(
        {**preimage, "subject_hash": canonical_json_sha256(preimage)}
    )


__all__ = ["blind_review_subject", "resolved_logparse_plan"]
