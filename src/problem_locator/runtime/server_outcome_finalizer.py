"""Server-only conversion of a sealed Agent draft into the unique Outcome."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from problem_locator.contracts import (
    AgentJobOutcome,
    AgentJobOutcomeDraftV2,
    ArtifactKind,
    DecisionAuditV2,
    DiagnosisOutcome,
    DiagnosisProvenanceType,
    DiagnosisStateDelta,
    Job,
    JobType,
    OutcomeResultType,
    ReviewAssessment,
    ReviewVerdict,
    UserResultPayload,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
    validate_outcome_for_job,
    validate_user_result_for_outcome,
)
from problem_locator.integrations.agent_json import atomic_replace_agent_json
from problem_locator.integrations.logparse.paths import resolve_workspace_path

from .outcome_finalizer import SERVER_OUTCOME_RELATIVE_PATH
from .server_verifier import VerificationResult


SERVER_FINALIZATION_MARKER_NAME = "server-job-outcome.finalized"
SERVER_FINALIZATION_MARKER_RELATIVE_PATH = (
    f"runtime/server-state/{SERVER_FINALIZATION_MARKER_NAME}"
)


class ServerFinalizedOutcomeMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    relative_path: Literal["output/job_outcome.json"]
    source_draft_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    outcome_size: Annotated[int, Field(ge=0)]
    outcome_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decision_audit_sha256: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ]
    decision_evidence_sha256: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ]


@dataclass(frozen=True, slots=True)
class ServerFinalizationResult:
    outcome: AgentJobOutcome
    canonical_bytes: bytes
    decision_audit_bytes: bytes | None
    decision_evidence_bytes: bytes
    user_result: UserResultPayload | None
    marker: ServerFinalizedOutcomeMarker


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


def _rewrite_diagnosis_provenance(
    payload: DiagnosisOutcome,
    *,
    job_id: str,
    outcome_id: str,
) -> DiagnosisOutcome:
    value = payload.model_dump(mode="python")
    delta = value["state_delta"]
    for field_name in (
        "proposed_facts",
        "add_active_hypotheses",
        "add_open_questions",
    ):
        for item in delta[field_name]:
            provenance = item["provenance"]
            if (
                provenance["source_type"] is DiagnosisProvenanceType.AGENT_OUTCOME
                and provenance["source_ref"] == job_id
            ):
                provenance["source_ref"] = outcome_id
    return DiagnosisOutcome.model_validate(value)


def _normalized_inconclusive_payload(
    payload: DiagnosisOutcome | ReviewAssessment,
) -> DiagnosisOutcome | ReviewAssessment:
    if isinstance(payload, DiagnosisOutcome):
        return DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step=(
                "Required evidence rules did not pass server verification."
            ),
        )
    return payload.model_copy(
        update={
            "verdict": ReviewVerdict.REJECT,
            "unsupported_findings": [
                "Required evidence rules did not pass server verification."
            ],
            "evidence_conflicts": [],
            "missing_evidence": [],
            "stale_references": [],
            "requested_requirement_ids": [],
            "recommendation": (
                "Keep the Candidate unresolved and inspect the DecisionAudit."
            ),
        }
    )


def _server_state_path(root: Path) -> Path:
    runtime = resolve_workspace_path(root, "runtime", must_exist=True)
    server_state = runtime / "server-state"
    try:
        server_state.mkdir()
    except FileExistsError as exc:
        raise ValueError("server finalization state already exists") from exc
    metadata = server_state.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("server finalization state is not a plain directory")
    return server_state


def finalize_server_outcome(
    *,
    workspace_root: Path,
    job: Job,
    manifest: WorkspaceInputManifest,
    draft: AgentJobOutcomeDraftV2,
    draft_bytes: bytes,
    outcome_id: str,
    produced_at: str,
    verification: VerificationResult | None,
    user_result_bytes: bytes | None,
) -> ServerFinalizationResult:
    """Validate, possibly downgrade, then atomically publish one final Outcome."""

    if draft.job_type is JobType.ROUTE or draft.result_type is OutcomeResultType.FAILED:
        if verification is not None:
            raise ValueError("ROUTE/FAILED drafts forbid a DecisionAudit")
        audit = None
        evidence_bytes = b""
    else:
        if verification is None:
            raise ValueError("non-failed DIAGNOSE/REVIEW drafts require verification")
        audit = verification.audit
        evidence_bytes = verification.decision_evidence_bytes

    result_type = draft.result_type
    payload = draft.payload
    artifact_drafts = list(draft.proposed_artifact_drafts)
    if (
        verification is not None
        and draft.result_type is OutcomeResultType.COMPLETED
        and not verification.positive_gate_passed
    ):
        assert isinstance(payload, (DiagnosisOutcome, ReviewAssessment))
        result_type = OutcomeResultType.INCONCLUSIVE
        payload = _normalized_inconclusive_payload(payload)
        artifact_drafts = [
            item
            for item in artifact_drafts
            if item.artifact_kind
            not in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
        ]
        user_result_bytes = None

    if isinstance(payload, DiagnosisOutcome):
        payload = _rewrite_diagnosis_provenance(
            payload,
            job_id=job.job_id,
            outcome_id=outcome_id,
        )

    outcome = AgentJobOutcome(
        outcome_id=outcome_id,
        job_id=draft.job_id,
        case_id=draft.case_id,
        job_type=draft.job_type,
        base_state_revision=draft.base_state_revision,
        result_type=result_type,
        payload=payload,
        consumed_evidence_refs=list(draft.consumed_evidence_refs),
        proposed_evidence_drafts=list(draft.proposed_evidence_drafts),
        proposed_artifact_drafts=artifact_drafts,
        error=draft.error,
        produced_at=produced_at,
        decision_audit=audit,
    )
    validate_outcome_for_job(job, outcome, manifest)

    user_result = None
    if user_result_bytes is not None:
        user_result = validate_user_result_for_outcome(
            job,
            outcome,
            user_result_bytes,
        )

    outcome_bytes = canonical_json_bytes(outcome)
    audit_bytes = None if audit is None else canonical_json_bytes(audit)
    output_path = resolve_workspace_path(
        workspace_root,
        SERVER_OUTCOME_RELATIVE_PATH,
        must_exist=False,
    )
    try:
        output_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("server-owned Outcome path already exists")
    server_state = _server_state_path(workspace_root)
    atomic_replace_agent_json(output_path, outcome_bytes)
    marker = ServerFinalizedOutcomeMarker(
        schema_version=2,
        relative_path=SERVER_OUTCOME_RELATIVE_PATH,
        source_draft_sha256=bytes_sha256(draft_bytes),
        outcome_size=len(outcome_bytes),
        outcome_sha256=bytes_sha256(outcome_bytes),
        decision_audit_sha256=(
            None if audit_bytes is None else bytes_sha256(audit_bytes)
        ),
        decision_evidence_sha256=(
            None if audit_bytes is None else bytes_sha256(evidence_bytes)
        ),
    )
    marker_path = server_state / SERVER_FINALIZATION_MARKER_NAME
    atomic_replace_agent_json(marker_path, canonical_json_bytes(marker))
    return ServerFinalizationResult(
        outcome=outcome,
        canonical_bytes=outcome_bytes,
        decision_audit_bytes=audit_bytes,
        decision_evidence_bytes=evidence_bytes,
        user_result=user_result,
        marker=marker,
    )


__all__ = [
    "SERVER_FINALIZATION_MARKER_NAME",
    "SERVER_FINALIZATION_MARKER_RELATIVE_PATH",
    "ServerFinalizationResult",
    "ServerFinalizedOutcomeMarker",
    "finalize_server_outcome",
]
