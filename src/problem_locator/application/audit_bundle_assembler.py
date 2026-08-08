"""Assemble the allowlisted observable record for an UNRESOLVED Case."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from problem_locator.contracts import (
    AgentJobOutcome,
    CaseAggregate,
    DecisionAuditV2,
    EvidenceBinding,
    ExecutionRecordStore,
    Job,
    JobOutcome,
    JobType,
    NonEmptyText,
    PositiveInt,
    RelativePosixPath,
    ReviewSubjectV2,
    Sha256,
    UnresolvedResultDraft,
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.runtime.server_outcome_finalizer import (
    ServerFinalizedOutcomeMarker,
)

from .audit_bundle import AuditBundleSource, BuiltAuditBundle, build_audit_bundle


_OPTIONAL_JOB_FILES = (
    "broker_audit.json",
)
_DECISION_EVIDENCE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _DecisionEvidenceLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    evidence_ref: NonEmptyText
    anchor: NonEmptyText | None
    relative_path: RelativePosixPath
    line_number: PositiveInt
    raw_line: Annotated[str, Field(strict=True)]
    raw_line_sha256: Sha256


def _binding_key(binding: EvidenceBinding) -> str:
    existing = binding.existing_evidence_id
    if existing is not None:
        return existing
    proposal = binding.evidence_proposal_key
    if proposal is None:  # pragma: no cover - protected by the contract model
        raise ValueError("DecisionAudit contains an empty Evidence binding")
    return f"proposal:{proposal}"


def _validated_decision_evidence_bytes(
    *,
    evidence_bytes: bytes,
    audit: DecisionAuditV2,
) -> bytes:
    """Bind canonical JSONL records to the audit's exact physical-line set."""

    expected_bindings: dict[tuple[str, int, int, str], set[str]] = {}
    for rule in audit.rules:
        evaluation = rule.server_evaluation
        bindings = {_binding_key(item) for item in evaluation.evidence_bindings}
        for line_range in evaluation.line_ranges:
            key = (
                line_range.path,
                line_range.line_start,
                line_range.line_end,
                line_range.raw_bytes_sha256,
            )
            expected_bindings.setdefault(key, set()).update(bindings)

    records: list[_DecisionEvidenceLine] = []
    if evidence_bytes:
        physical_records = evidence_bytes.splitlines(keepends=True)
        if b"".join(physical_records) != evidence_bytes:
            raise ValueError("decision evidence JSONL has an invalid byte boundary")
        for raw_record in physical_records:
            try:
                record = parse_canonical_json_bytes(
                    raw_record,
                    _DecisionEvidenceLine,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "decision evidence contains a non-canonical or invalid JSONL record"
                ) from exc
            records.append(record)

    actual: set[tuple[str, int, int, str]] = set()
    for record in records:
        if _DECISION_EVIDENCE_SHA256.fullmatch(record.raw_line_sha256) is None:
            # Pydantic already enforces this.  Keep the check local to this
            # trust boundary so a future model relaxation cannot weaken it.
            raise ValueError("decision evidence contains an invalid raw-line hash")
        key = (
            record.relative_path,
            record.line_number,
            record.line_number,
            record.raw_line_sha256,
        )
        if key in actual:
            raise ValueError("decision evidence contains a duplicate physical line")
        allowed_bindings = expected_bindings.get(key)
        if allowed_bindings is None or record.evidence_ref not in allowed_bindings:
            raise ValueError("decision evidence is not bound to its DecisionAudit line")
        actual.add(key)

    if actual != set(expected_bindings):
        raise ValueError(
            "decision evidence line ranges differ from the accepted DecisionAudit"
        )
    return evidence_bytes


def _stdio_metadata_bytes(
    records: ExecutionRecordStore,
    job_id: str,
) -> bytes:
    """Summarize local agent streams without copying their payloads."""

    streams: dict[str, dict[str, object]] = {}
    for stream_name in ("stdout", "stderr"):
        payload = records.read_audit_bytes(job_id, f"{stream_name}.log")
        streams[stream_name] = {
            "available": payload is not None,
            "size": None if payload is None else len(payload),
            "sha256": None if payload is None else bytes_sha256(payload),
        }
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "streams": streams,
        }
    )


def _audit_job_ids(
    aggregate: CaseAggregate,
    source_job: Job,
) -> tuple[str, ...]:
    selected: set[str] = {source_job.job_id}
    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    if candidate is not None:
        selected.add(candidate.proposed_by_job_id)
    for outcome_id in source_job.previous_outcome_refs:
        outcome = aggregate.outcomes.get(outcome_id)
        if outcome is not None:
            selected.add(outcome.job_id)
    return tuple(
        sorted(
            selected,
            key=lambda job_id: (
                aggregate.jobs[job_id].created_at
                if job_id in aggregate.jobs
                else "",
                job_id,
            ),
        )
    )


def _required_execution_bytes(
    records: ExecutionRecordStore,
    job_id: str,
    filename: str,
) -> bytes:
    payload = records.read_audit_bytes(job_id, filename)
    if payload is None:
        raise ValueError(
            f"required V2 execution record is unavailable: {job_id}/{filename}"
        )
    return payload


def _validated_finalization_bytes(
    *,
    marker_bytes: bytes,
    agent_outcome_bytes: bytes,
    source_outcome: JobOutcome,
    draft_bytes: bytes,
    decision_audit_bytes: bytes,
    decision_evidence_bytes: bytes,
) -> bytes:
    """Reject a present-but-inconsistent server finalization record."""

    try:
        marker = parse_canonical_json_bytes(
            marker_bytes,
            ServerFinalizedOutcomeMarker,
        )
        agent_outcome = parse_canonical_json_bytes(
            agent_outcome_bytes,
            AgentJobOutcome,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("server finalization record is not valid JSON") from exc
    common_fields = (
        "outcome_id",
        "job_id",
        "case_id",
        "job_type",
        "base_state_revision",
        "result_type",
        "payload",
        "consumed_evidence_refs",
        "error",
        "produced_at",
        "decision_audit",
    )
    if any(
        getattr(agent_outcome, field) != getattr(source_outcome, field)
        for field in common_fields
    ):
        raise ValueError(
            "server-finalized Agent Outcome differs from the accepted Outcome"
        )
    if len(agent_outcome.proposed_evidence_drafts) != len(
        source_outcome.proposed_evidence
    ) or len(agent_outcome.proposed_artifact_drafts) != len(
        source_outcome.proposed_artifacts
    ):
        raise ValueError("accepted Outcome proposal closure differs from finalization")
    for draft, proposal in zip(
        agent_outcome.proposed_evidence_drafts,
        source_outcome.proposed_evidence,
        strict=True,
    ):
        if any(
            getattr(draft, field) != getattr(proposal, field)
            for field in (
                "proposal_key",
                "source_type",
                "source_binding",
                "locator",
                "summary",
            )
        ):
            raise ValueError("accepted Evidence proposal differs from finalization")
        staged = proposal.staged_resource_ref
        if draft.workspace_relative_path is None:
            if staged is not None or proposal.content_hash is not None:
                raise ValueError("resource-free Evidence acquired staged content")
        elif (
            staged is None
            or proposal.content_hash != staged.sha256
            or (
                draft.declared_size is not None
                and draft.declared_size != staged.size
            )
            or (
                draft.declared_sha256 is not None
                and draft.declared_sha256 != staged.sha256
            )
        ):
            raise ValueError("staged Evidence differs from its finalized draft")
    for draft, proposal in zip(
        agent_outcome.proposed_artifact_drafts,
        source_outcome.proposed_artifacts,
        strict=True,
    ):
        if any(
            getattr(draft, field) != getattr(proposal, field)
            for field in (
                "proposal_key",
                "artifact_kind",
                "name",
                "content_type",
                "resource_kind",
                "metadata",
            )
        ) or (
            draft.declared_size is not None
            and draft.declared_size != proposal.size
        ) or (
            draft.declared_sha256 is not None
            and draft.declared_sha256 != proposal.sha256
        ):
            raise ValueError("accepted Artifact proposal differs from finalization")
    draft_sha256 = hashlib.sha256(draft_bytes).hexdigest()
    decision_audit_sha256 = hashlib.sha256(decision_audit_bytes).hexdigest()
    decision_evidence_sha256 = hashlib.sha256(decision_evidence_bytes).hexdigest()
    if (
        marker.source_draft_sha256 != draft_sha256
        or marker.outcome_size != len(agent_outcome_bytes)
        or marker.outcome_sha256
        != hashlib.sha256(agent_outcome_bytes).hexdigest()
        or marker.decision_audit_sha256 != decision_audit_sha256
        or marker.decision_evidence_sha256 != decision_evidence_sha256
        or source_outcome.decision_audit is None
        or source_outcome.decision_audit.source_draft_sha256 != draft_sha256
    ):
        raise ValueError("server finalization record does not bind the accepted Outcome")
    return marker_bytes


def _validated_review_subject_bytes(
    *,
    subject_bytes: bytes,
    job: Job,
    outcome: JobOutcome,
) -> bytes:
    try:
        subject = parse_canonical_json_bytes(subject_bytes, ReviewSubjectV2)
    except (TypeError, ValueError) as exc:
        raise ValueError("persisted REVIEW subject is invalid") from exc
    audit = outcome.decision_audit
    target = job.review_target
    if (
        subject_bytes != canonical_json_bytes(subject)
        or audit is None
        or target is None
        or subject.review_job_id != job.job_id
        or subject.case_id != job.case_id
        or subject.reviewed_state_revision != job.base_state_revision
        or subject.skill_ref != job.skill_ref
        or subject.subject_hash != audit.subject_hash
        or subject.candidate.conclusion_id != target.candidate_conclusion_id
        or subject.candidate.revision != target.candidate_revision
        or subject.candidate.content_hash != target.candidate_content_hash
    ):
        raise ValueError("persisted REVIEW subject differs from the accepted Outcome")
    return subject_bytes


def assemble_unresolved_audit_bundle(
    *,
    aggregate: CaseAggregate,
    source_job: Job,
    source_outcome: JobOutcome,
    unresolved: UnresolvedResultDraft,
    resolved_evidence_refs: Sequence[str],
    execution_records: ExecutionRecordStore,
) -> BuiltAuditBundle:
    """Build an audit ZIP without exposing rejected user-result payload bytes."""

    if (
        source_job.case_id != aggregate.case.case_id
        or source_outcome.case_id != aggregate.case.case_id
        or unresolved.source_job_id != source_job.job_id
        or unresolved.source_outcome_id != source_outcome.outcome_id
    ):
        raise ValueError("unresolved audit subject identity is inconsistent")

    candidate = aggregate.case.diagnosis_state.candidate_conclusion
    case_summary = {
        "schema_version": 1,
        "case_id": aggregate.case.case_id,
        "terminal_status": "UNRESOLVED",
        "unresolved": unresolved.model_dump(mode="json"),
        "problem_spec": aggregate.case.diagnosis_state.problem_spec.model_dump(
            mode="json"
        ),
        "user_facts": [
            item.model_dump(mode="json")
            for item in aggregate.case.diagnosis_state.user_facts
        ],
        "candidate": (
            None
            if candidate is None
            else {
                "not_final": True,
                "value": candidate.model_dump(mode="json"),
            }
        ),
    }
    sources: list[AuditBundleSource] = [
        AuditBundleSource(
            "case-summary.json",
            canonical_json_bytes(case_summary),
            required=True,
        )
    ]

    job_ids = _audit_job_ids(aggregate, source_job)
    outcomes_by_job = {item.job_id: item for item in aggregate.outcomes.values()}
    outcomes_by_job[source_outcome.job_id] = source_outcome
    for job_id in job_ids:
        job = aggregate.jobs.get(job_id)
        outcome = outcomes_by_job.get(job_id)
        if job is None or outcome is None:
            raise ValueError("audit Job closure is incomplete")
        prefix = f"jobs/{job.job_type.value.lower()}/{job_id}"
        sources.extend(
            (
                AuditBundleSource(
                    f"{prefix}/job.json",
                    canonical_json_bytes(job),
                    required=True,
                ),
                AuditBundleSource(
                    f"{prefix}/context.txt",
                    _required_execution_bytes(
                        execution_records, job_id, "context.txt"
                    ),
                    required=True,
                ),
                AuditBundleSource(
                    f"{prefix}/job_outcome.json",
                    canonical_json_bytes(outcome),
                    required=True,
                ),
            )
        )
        sources.append(
            AuditBundleSource(
                f"{prefix}/stdio-metadata.json",
                _stdio_metadata_bytes(execution_records, job_id),
                required=True,
            )
        )
        if outcome.decision_audit is not None:
            audit_bytes = _required_execution_bytes(
                execution_records,
                job_id,
                "decision_audit.json",
            )
            if audit_bytes != canonical_json_bytes(outcome.decision_audit):
                raise ValueError(
                    "persisted decision audit differs from the accepted Outcome"
                )
            draft_bytes = _required_execution_bytes(
                execution_records,
                job_id,
                "agent_job_outcome.draft.json",
            )
            agent_outcome_bytes = _required_execution_bytes(
                execution_records,
                job_id,
                "agent_job_outcome.json",
            )
            decision_evidence_bytes = _validated_decision_evidence_bytes(
                evidence_bytes=_required_execution_bytes(
                    execution_records,
                    job_id,
                    "decision_evidence.jsonl",
                ),
                audit=outcome.decision_audit,
            )
            finalization_bytes = _validated_finalization_bytes(
                marker_bytes=_required_execution_bytes(
                    execution_records,
                    job_id,
                    "finalization_manifest.json",
                ),
                agent_outcome_bytes=agent_outcome_bytes,
                source_outcome=outcome,
                draft_bytes=draft_bytes,
                decision_audit_bytes=audit_bytes,
                decision_evidence_bytes=decision_evidence_bytes,
            )
            sources.append(
                AuditBundleSource(
                    f"{prefix}/agent_job_outcome.draft.json",
                    draft_bytes,
                    required=True,
                )
            )
            sources.append(
                AuditBundleSource(
                    f"{prefix}/agent_job_outcome.json",
                    agent_outcome_bytes,
                    required=True,
                )
            )
            sources.append(
                AuditBundleSource(
                    f"{prefix}/decision_audit.json",
                    audit_bytes,
                    required=True,
                )
            )
            sources.append(
                AuditBundleSource(
                    f"{prefix}/decision_evidence.jsonl",
                    decision_evidence_bytes,
                    required=True,
                )
            )
            sources.append(
                AuditBundleSource(
                    f"{prefix}/finalization_manifest.json",
                    finalization_bytes,
                    required=True,
                )
            )
            if job.job_type is JobType.REVIEW:
                review_subject_bytes = _validated_review_subject_bytes(
                    subject_bytes=_required_execution_bytes(
                        execution_records,
                        job_id,
                        "review_subject.json",
                    ),
                    job=job,
                    outcome=outcome,
                )
                sources.append(
                    AuditBundleSource(
                        f"{prefix}/review_subject.json",
                        review_subject_bytes,
                        required=True,
                    )
                )
        for filename in _OPTIONAL_JOB_FILES:
            payload = execution_records.read_audit_bytes(job_id, filename)
            if payload is not None:
                sources.append(
                    AuditBundleSource(
                        f"{prefix}/{filename}",
                        payload,
                        required=False,
                    )
                )

    evidence_ids: list[str] = []
    seen_evidence: set[str] = set()
    for ref in resolved_evidence_refs:
        if ref not in seen_evidence:
            seen_evidence.add(ref)
            evidence_ids.append(ref)
    index = []
    for evidence_id in evidence_ids:
        evidence = aggregate.evidence.get(evidence_id)
        index.append(
            {
                "evidence_id": evidence_id,
                "available_in_state": evidence is not None,
                "metadata": (
                    None if evidence is None else evidence.model_dump(mode="json")
                ),
            }
        )
    sources.append(
        AuditBundleSource(
            "evidence/index.json",
            canonical_json_bytes({"schema_version": 1, "items": index}),
            required=True,
        )
    )
    return build_audit_bundle(
        case_id=aggregate.case.case_id,
        source_outcome_id=source_outcome.outcome_id,
        sources=sources,
    )


__all__ = ["assemble_unresolved_audit_bundle"]
