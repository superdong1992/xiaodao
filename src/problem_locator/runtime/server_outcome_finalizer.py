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

from .authoritative_targets import AuthoritativeTargetSet
from .outcome_finalizer import SERVER_OUTCOME_RELATIVE_PATH
from .result_types import CapturedTargetLog, ServerGeneratedResultFile
from .server_verifier import VerificationResult
from .user_results import build_server_result_bundle


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
    generated_result_files: tuple[ServerGeneratedResultFile, ...]
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
    authoritative_targets: AuthoritativeTargetSet | None,
    target_logs: tuple[CapturedTargetLog, ...],
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
    verification_requires_inconclusive = (
        verification is not None
        and draft.result_type is OutcomeResultType.COMPLETED
        and not verification.positive_gate_passed
    )
    targets_require_inconclusive = (
        verification is not None
        and authoritative_targets is not None
        and bool(authoritative_targets.unresolved)
        and draft.result_type is not OutcomeResultType.INCONCLUSIVE
    )
    if verification_requires_inconclusive or targets_require_inconclusive:
        assert isinstance(payload, (DiagnosisOutcome, ReviewAssessment))
        result_type = OutcomeResultType.INCONCLUSIVE
        payload = _normalized_inconclusive_payload(payload)
        # The sealed Agent draft is already forbidden from supplying either
        # result kind.  Keep this fail-closed filter as a defense at the
        # server-final seam, then append only freshly generated v2 resources.
        artifact_drafts = [
            item
            for item in artifact_drafts
            if item.artifact_kind
            not in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
        ]

    if isinstance(payload, DiagnosisOutcome):
        payload = _rewrite_diagnosis_provenance(
            payload,
            job_id=job.job_id,
            outcome_id=outcome_id,
        )

    generated_result_files: tuple[ServerGeneratedResultFile, ...] = ()
    user_result = None
    publishes_completed_result = (
        job.job_type is JobType.DIAGNOSE
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, DiagnosisOutcome)
        and payload.candidate_conclusion_draft is not None
    )
    publishes_unresolved_result = result_type is OutcomeResultType.INCONCLUSIVE or (
        job.job_type is JobType.REVIEW
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, ReviewAssessment)
        and payload.verdict is not ReviewVerdict.PASS
    )
    if publishes_completed_result or publishes_unresolved_result:
        if verification is None or not isinstance(
            payload, (DiagnosisOutcome, ReviewAssessment)
        ):
            raise ValueError("a public Result v2 requires a verified diagnosis payload")
        bundle = build_server_result_bundle(
            job=job,
            result_type=result_type,
            payload=payload,
            verification=verification,
            authoritative_targets=authoritative_targets,
            captured_logs=target_logs,
        )
        user_result = bundle.report
        generated_result_files = bundle.files
        artifact_drafts.extend(item.draft for item in generated_result_files)

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

    if user_result is not None:
        validate_user_result_for_outcome(
            job,
            outcome,
            canonical_json_bytes(user_result),
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
        generated_result_files=generated_result_files,
        marker=marker,
    )


__all__ = [
    "SERVER_FINALIZATION_MARKER_NAME",
    "SERVER_FINALIZATION_MARKER_RELATIVE_PATH",
    "ServerFinalizationResult",
    "ServerFinalizedOutcomeMarker",
    "finalize_server_outcome",
]
