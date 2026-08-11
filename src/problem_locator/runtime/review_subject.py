"""Build the blind, server-owned subject for one REVIEW Job."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from problem_locator.contracts import (
    CaseAggregate,
    DiagnosisOutcome,
    EvidenceBinding,
    Job,
    JobType,
    MechanicalFact,
    ReviewCausalAssertion,
    ReviewSubjectV2,
    canonical_json_sha256,
)

from .context_policy import ResolvedJobAssets


def _verification_contract(assets: ResolvedJobAssets) -> dict[str, Any]:
    if assets.skill is None:
        raise ValueError("REVIEW requires a pinned diagnosis Skill")
    path = Path(assets.skill.root_path) / "diagnosis-skill.json"
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 5:
        raise ValueError("REVIEW requires a diagnosis Skill manifest v5")
    contract = value.get("verification_contract")
    if not isinstance(contract, dict) or contract.get("schema_version") != 2:
        raise ValueError("REVIEW Skill has no verification contract")
    return contract


def _diagnosis_outcome(job: Job, aggregate: CaseAggregate):
    matches = [
        aggregate.outcomes[outcome_id]
        for outcome_id in job.previous_outcome_refs
        if outcome_id in aggregate.outcomes
        and aggregate.outcomes[outcome_id].job_type is JobType.DIAGNOSE
        and aggregate.outcomes[outcome_id].decision_audit is not None
    ]
    if len(matches) != 1:
        raise ValueError("REVIEW requires exactly one audited diagnosis Outcome")
    return matches[0]


def _binding_key(binding: EvidenceBinding) -> str:
    return (
        binding.existing_evidence_id
        if binding.existing_evidence_id is not None
        else f"proposal:{binding.evidence_proposal_key}"
    )


def _formal_evidence_by_binding(diagnosis, candidate) -> dict[str, str]:
    payload = diagnosis.payload
    if not isinstance(payload, DiagnosisOutcome):
        raise ValueError("audited diagnosis has no DiagnosisOutcome payload")
    draft = payload.candidate_conclusion_draft
    if draft is None:
        raise ValueError("audited diagnosis has no Candidate draft")
    draft_groups = [
        draft.supporting_evidence_bindings,
        *(item.evidence_bindings for item in draft.completion_criteria_mapping),
        *(
            item.evidence_bindings
            for item in (
                draft.causal_factors
                + draft.candidate_factors
                + draft.excluded_factors
            )
        ),
    ]
    formal_groups = [
        candidate.supporting_evidence_refs,
        *(item.evidence_refs for item in candidate.completion_criteria_mapping),
        *(
            item.evidence_refs
            for item in (
                candidate.causal_factors
                + candidate.candidate_factors
                + candidate.excluded_factors
            )
        ),
    ]
    if len(draft_groups) != len(formal_groups):
        raise ValueError("Candidate Evidence groups changed before REVIEW")
    result: dict[str, str] = {}
    for bindings, refs in zip(draft_groups, formal_groups, strict=True):
        if len(bindings) != len(refs):
            raise ValueError("Candidate Evidence order changed before REVIEW")
        for binding, evidence_ref in zip(bindings, refs, strict=True):
            key = _binding_key(binding)
            previous = result.setdefault(key, evidence_ref)
            if previous != evidence_ref:
                raise ValueError("one diagnosis binding resolved to multiple Evidence IDs")
            if (
                binding.existing_evidence_id is not None
                and binding.existing_evidence_id != evidence_ref
            ):
                raise ValueError("existing Evidence binding changed before REVIEW")
    return result


def _required_candidate_evidence(candidate, job: Job) -> list[str]:
    required = {
        *candidate.supporting_evidence_refs,
        *(
            evidence_ref
            for mapping in candidate.completion_criteria_mapping
            for evidence_ref in mapping.evidence_refs
        ),
        *(
            evidence_ref
            for factor in (
                candidate.causal_factors
                + candidate.candidate_factors
                + candidate.excluded_factors
            )
            for evidence_ref in factor.evidence_refs
        ),
    }
    ordered = [evidence_ref for evidence_ref in job.evidence_refs if evidence_ref in required]
    if set(ordered) != required:
        raise ValueError("REVIEW Job omits required Candidate Evidence")
    return ordered


def compile_review_subject(
    job: Job,
    aggregate: CaseAggregate,
    assets: ResolvedJobAssets,
) -> ReviewSubjectV2 | None:
    """Return a subject that excludes the Specialist's verdict and explanation."""

    if job.job_type is not JobType.REVIEW:
        return None
    if job.skill_ref is None or job.review_target is None:
        raise ValueError("REVIEW Job is missing its fixed Skill or Candidate target")
    candidate = job.context_snapshot.candidate_conclusion
    if candidate is None:
        raise ValueError("REVIEW Job is missing its fixed Candidate")

    contract = _verification_contract(assets)
    raw_rules = contract.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("verification contract has no rules")
    rule_ids: list[str] = []
    causal: list[ReviewCausalAssertion] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ValueError("verification rule is not an object")
        rule_id = raw_rule.get("id")
        kind = raw_rule.get("kind")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("verification rule has no stable ID")
        rule_ids.append(rule_id)
        if kind == "SEMANTIC_CAUSALITY":
            parameters = raw_rule.get("parameters")
            assertion = (
                parameters.get("assertion")
                if isinstance(parameters, dict)
                else None
            )
            if not isinstance(assertion, str) or not assertion:
                raise ValueError("semantic rule has no assertion")
            causal.append(
                ReviewCausalAssertion(rule_id=rule_id, statement=assertion)
            )
    if not causal:
        raise ValueError("REVIEW requires at least one semantic causality rule")

    diagnosis = _diagnosis_outcome(job, aggregate)
    audit = diagnosis.decision_audit
    assert audit is not None
    formal_by_binding = _formal_evidence_by_binding(diagnosis, candidate)
    required_candidate_evidence = _required_candidate_evidence(candidate, job)

    audit_rules = {item.rule_id: item for item in audit.rules}
    mapped_required_evidence: list[str] = []
    for binding in audit.required_evidence_bindings:
        evidence_ref = formal_by_binding.get(_binding_key(binding))
        if evidence_ref is not None and evidence_ref not in mapped_required_evidence:
            mapped_required_evidence.append(evidence_ref)
    audit_integrity_passed = (
        audit.job_type is JobType.DIAGNOSE
        and audit.case_id == job.case_id
        and audit.skill_ref == job.skill_ref
        and audit.required_rule_ids == rule_ids
        and [item.rule_id for item in audit.rules] == rule_ids
        and set(required_candidate_evidence) <= set(mapped_required_evidence)
    )

    integrity_fact_id = "server_diagnosis_audit_integrity"
    while integrity_fact_id in rule_ids:
        integrity_fact_id += "_marker"
    mechanical: list[MechanicalFact] = [
        MechanicalFact(
            fact_id=integrity_fact_id,
            name="diagnosis_audit_integrity",
            value=(
                "VERIFIED_PASS" if audit_integrity_passed else "VERIFIED_FAIL"
            ),
            source_rule_id=rule_ids[0],
            evidence_refs=mapped_required_evidence,
        )
    ]
    semantic_ids = {item.rule_id for item in causal}
    for rule_id in rule_ids:
        if rule_id in semantic_ids:
            # In particular, do not reveal the Specialist's semantic claim.
            continue
        item = audit_rules.get(rule_id)
        if item is None:
            mechanical.append(
                MechanicalFact(
                    fact_id=rule_id,
                    name=rule_id,
                    value="UNVERIFIABLE",
                    source_rule_id=rule_id,
                    evidence_refs=[],
                )
            )
            continue
        evaluation = item.server_evaluation
        evidence_refs: list[str] = []
        for binding in evaluation.evidence_bindings:
            evidence_ref = formal_by_binding.get(_binding_key(binding))
            if evidence_ref is None:
                audit_integrity_passed = False
                continue
            if evidence_ref not in evidence_refs:
                evidence_refs.append(evidence_ref)
        mechanical.append(
            MechanicalFact(
                fact_id=item.rule_id,
                name=item.rule_id,
                value=evaluation.status.value,
                source_rule_id=item.rule_id,
                evidence_refs=evidence_refs,
            )
        )
    if not audit_integrity_passed:
        mechanical[0] = mechanical[0].model_copy(
            update={"value": "VERIFIED_FAIL"}
        )

    payload: dict[str, object] = {
        "schema_version": 2,
        "review_job_id": job.job_id,
        "case_id": job.case_id,
        "reviewed_state_revision": job.base_state_revision,
        "skill_ref": job.skill_ref.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "causal_assertions": [item.model_dump(mode="json") for item in causal],
        "required_rule_ids": rule_ids,
        "required_evidence_refs": required_candidate_evidence,
        "mechanical_facts": [item.model_dump(mode="json") for item in mechanical],
    }
    payload["subject_hash"] = canonical_json_sha256(payload)
    return ReviewSubjectV2.model_validate(payload)


__all__ = ["compile_review_subject"]
