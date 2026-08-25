from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import (
    SCHEMA_MODELS,
    AgentArtifactProposalDraft,
    AgentEvidenceProposalDraft,
    AgentJobOutcome,
    ArtifactKind,
    ArtifactProposal,
    AttachmentRequirementConstraints,
    CandidateConclusionDraft,
    CompletionCriterionDraftMapping,
    DecisionAuditV2,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceProposal,
    EvidenceSourceBinding,
    EvidenceSourceType,
    Finding,
    InputRequirementConstraints,
    Job,
    JobOutcome,
    LogparseEvidenceLocator,
    LogparseParseClaim,
    LogparseParseParameters,
    LogparseRunMetadata,
    OutcomeResultType,
    PendingRequirement,
    RequirementKind,
    RequirementStatus,
    ResourceKind,
    StagedResourceRef,
    UserResultArchiveMetadataV3,
    UserResultMetadata,
    UserResultPayload,
    WorkspaceAttachmentInput,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
    canonical_json_sha256,
    parse_canonical_json_bytes,
    validate_logparse_claim_for_job,
    validate_outcome_for_job,
    validate_user_result_for_outcome,
    validate_workspace_manifest_for_job,
)

from tests.deterministic.contracts.fakes import InMemoryBinaryStream, InMemoryResourceStore


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "contracts"
LOGPARSE_RUN_KEY = "logparse-run"
LOGPARSE_EVIDENCE_KEY = "logparse-evidence"
USER_RESULT_KEY = "user-result"
USER_RESULT_ARCHIVE_KEY = "user-result-archive"
ORDER_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000081"
ATTACHMENT_REQUIREMENT_ID = "00000000-0000-0000-0000-000000000082"
OTHER_EVIDENCE_ID = "00000000-0000-0000-0000-000000000099"
PRODUCED_AT = "2026-07-31T00:01:30.000Z"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _job_and_manifest(
    *,
    include_attachment: bool,
    include_logparse_run: bool,
) -> tuple[Job, WorkspaceInputManifest]:
    if include_logparse_run and not include_attachment:
        raise ValueError("a frozen LOGPARSE_RUN requires its source Attachment")

    job_payload = _load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    manifest_payload = _load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    if not include_logparse_run:
        job_payload["artifact_refs"] = []
        manifest_payload["entries"] = [
            entry
            for entry in manifest_payload["entries"]
            if entry["input_kind"] != "ARTIFACT"
        ]
    if not include_attachment:
        job_payload["attachment_refs"] = []
        manifest_payload["entries"] = [
            entry
            for entry in manifest_payload["entries"]
            if entry["input_kind"] != "ATTACHMENT"
        ]

    if not include_logparse_run:
        if include_attachment:
            plan = manifest_payload["resolved_logparse_plan"]
            plan["attachment_id"] = job_payload["attachment_refs"][0]
            plan["artifact_id"] = None
        else:
            manifest_payload["resolved_logparse_plan"] = None

    job = Job.model_validate(job_payload)
    manifest = WorkspaceInputManifest.model_validate(manifest_payload)
    assert validate_workspace_manifest_for_job(manifest, job) is manifest
    return job, manifest


def _empty_delta(
    pending_requirements: list[PendingRequirement] | None = None,
) -> DiagnosisStateDelta:
    return DiagnosisStateDelta(
        problem_spec_patch=None,
        add_user_facts=[],
        proposed_facts=[],
        add_active_hypotheses=[],
        update_hypotheses=[],
        reject_hypotheses=[],
        add_open_questions=[],
        resolve_questions=[],
        add_pending_requirements=pending_requirements or [],
        fulfill_requirements=[],
        add_evidence_bindings=[],
    )


def _agent_outcome(
    job: Job,
    *,
    outcome_id: str,
    result_type: OutcomeResultType,
    payload: DiagnosisOutcome,
    evidence: list[AgentEvidenceProposalDraft] | None = None,
    artifacts: list[AgentArtifactProposalDraft] | None = None,
) -> AgentJobOutcome:
    evidence_drafts = evidence or []
    artifact_drafts = artifacts or []
    audit = (
        None
        if result_type
        in {OutcomeResultType.NEED_INPUT, OutcomeResultType.NEED_ATTACHMENT}
        else _decision_audit(job, evidence_drafts, payload)
    )
    return AgentJobOutcome(
        outcome_id=outcome_id,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=result_type,
        payload=payload,
        consumed_evidence_refs=[],
        proposed_evidence_drafts=evidence_drafts,
        proposed_artifact_drafts=artifact_drafts,
        error=None,
        produced_at=PRODUCED_AT,
        decision_audit=audit,
    )


def _decision_audit(
    job: Job,
    evidence: list[AgentEvidenceProposalDraft],
    payload: DiagnosisOutcome,
) -> DecisionAuditV2:
    """Represent the authoritative V2 audit boundary used by these fixtures."""

    assert job.skill_ref is not None
    bindings = [
        EvidenceBinding(
            existing_evidence_id=None,
            evidence_proposal_key=item.proposal_key,
        )
        for item in evidence
    ]
    rule_id = "causal_chain"
    candidate = payload.candidate_conclusion_draft
    terminal_path_id = (
        candidate.terminal_path_id if candidate is not None else "none"
    )
    terminal_resolution_status = (
        candidate.resolution_status.value if candidate is not None else "NONE"
    )
    claim_result = "PASS" if candidate is not None else "UNKNOWN"
    return DecisionAuditV2.model_validate(
        {
            "schema_version": 2,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "skill_ref": job.skill_ref.model_dump(mode="json"),
            "source_draft_sha256": "1" * 64,
            "subject_hash": "2" * 64,
            "candidate_target": None,
            "diagnosis_audit_hash": None,
            "selected_terminal_path_id": terminal_path_id,
            "terminal_resolution_status": terminal_resolution_status,
            "required_rule_ids": [rule_id],
            "required_evidence_bindings": [
                item.model_dump(mode="json") for item in bindings
            ],
            "rules": [
                {
                    "rule_id": rule_id,
                    "agent_claim": {
                        "rule_id": rule_id,
                        "claimed_result": claim_result,
                        "fact_refs": [],
                        "citations": [],
                        "explanation": "The fixture explicitly assesses causality.",
                    },
                    "server_evaluation": {
                        "rule_id": rule_id,
                        "rule_kind": "SEMANTIC_CAUSALITY",
                        "status": "SEMANTIC_ONLY",
                        "fact_refs": [],
                        "evidence_bindings": [
                            item.model_dump(mode="json") for item in bindings
                        ],
                        "anchor_id": None,
                        "derived_anchor_time": None,
                        "observed_times": [],
                        "line_ranges": [],
                        "event_observations": [],
                        "derived_values": [],
                        "issues": [],
                    },
                }
            ],
        }
    )


def _schema_round_trip(
    schema_name: str,
    value: AgentJobOutcome | JobOutcome | UserResultPayload,
) -> Any:
    payload = value.model_dump(mode="json")
    validated = TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(payload)
    assert validated == value
    assert parse_canonical_json_bytes(
        canonical_json_bytes(value), model_type=type(value)
    ) == value
    return validated


def _assert_agent_contract_rejects(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SCHEMA_MODELS["agent-job-outcome.schema.json"]).validate_python(
            payload
        )


def _order_requirement(job: Job) -> PendingRequirement:
    return PendingRequirement(
        requirement_id=ORDER_REQUIREMENT_ID,
        kind=RequirementKind.INPUT,
        name="order_id",
        prompt="Provide the order identifier for the timed-out request.",
        required=True,
        constraints=InputRequirementConstraints(
            value_type="STRING",
            min_utf8_bytes=1,
            max_utf8_bytes=128,
            pattern=r"^[A-Za-z0-9._-]+$",
            allowed_values=[],
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=job.job_id,
        fulfilled_by_refs=[],
    )


def _attachment_requirement(job: Job) -> PendingRequirement:
    return PendingRequirement(
        requirement_id=ATTACHMENT_REQUIREMENT_ID,
        kind=RequirementKind.ATTACHMENT,
        name="log_archive",
        prompt="Attach one archive containing only the target incident logs.",
        required=True,
        constraints=AttachmentRequirementConstraints(
            allowed_content_types=[
                "application/gzip",
                "application/zip",
                "application/x-tar",
            ],
            min_count=1,
            max_count=1,
        ),
        status=RequirementStatus.OPEN,
        requested_by_job_id=job.job_id,
        fulfilled_by_refs=[],
    )


def _stage_logparse_tree(
    tmp_path: Path,
    job: Job,
) -> StagedResourceRef:
    root = tmp_path / "logparse-run"
    (root / "task" / "logs").mkdir(parents=True)
    (root / "task" / "logs" / "client.log").write_bytes(
        b"2026-07-31T00:00:03.000Z deadline exceeded order=synthetic-order\n"
    )
    (root / "task" / "parse_manifest.json").write_bytes(
        canonical_json_bytes(
            {
                "product": job.logparse_product,
                "task_id": "task",
            }
        )
    )

    staged = InMemoryResourceStore().stage_tree(
        job.job_id,
        LOGPARSE_RUN_KEY,
        root,
    )
    assert staged.tree_manifest is not None
    assert [entry.path for entry in staged.tree_manifest.entries] == [
        "task/logs/client.log",
        "task/parse_manifest.json",
    ]
    assert staged.sha256 == canonical_json_sha256(staged.tree_manifest)
    assert staged.size == sum(entry.size for entry in staged.tree_manifest.entries)
    return staged


def _first_need_input_bundle(
    tmp_path: Path,
) -> tuple[
    Job,
    WorkspaceInputManifest,
    AgentJobOutcome,
    LogparseParseClaim,
    bytes,
    StagedResourceRef,
]:
    job, manifest = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=False,
    )
    attachment = next(
        entry
        for entry in manifest.entries
        if isinstance(entry, WorkspaceAttachmentInput)
    )
    staged = _stage_logparse_tree(tmp_path, job)
    assert job.logparse_tool_ref is not None
    assert job.logparse_product is not None
    metadata = LogparseRunMetadata(
        tree_manifest_sha256=staged.sha256,
        logparse_version_ref=job.logparse_tool_ref,
        parse_manifest_relative_path="task/parse_manifest.json",
        source_attachment_id=attachment.resource_id,
        source_attachment_sha256=attachment.sha256,
        parse_parameters=LogparseParseParameters(product=job.logparse_product),
    )
    artifact = AgentArtifactProposalDraft(
        proposal_key=LOGPARSE_RUN_KEY,
        artifact_kind=ArtifactKind.LOGPARSE_RUN,
        name="parsed-log-run",
        content_type="application/vnd.problem-locator.logparse-run+directory",
        resource_kind=ResourceKind.DIRECTORY,
        workspace_relative_path=(
            f"output/proposals/{LOGPARSE_RUN_KEY}/tree"
        ),
        declared_size=staged.size,
        declared_sha256=staged.sha256,
        metadata=metadata,
    )
    evidence = AgentEvidenceProposalDraft(
        proposal_key=LOGPARSE_EVIDENCE_KEY,
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=LOGPARSE_RUN_KEY,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="task/logs/client.log",
            start_line=1,
            end_line=1,
            start_time="2026-07-31T00:00:03.000Z",
            end_time="2026-07-31T00:00:03.000Z",
        ),
        summary="The client log identifies the timed-out request window.",
        workspace_relative_path=None,
        declared_size=None,
        declared_sha256=None,
    )
    evidence_binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=LOGPARSE_EVIDENCE_KEY,
    )
    outcome = _agent_outcome(
        job,
        outcome_id="00000000-0000-0000-0000-000000000021",
        result_type=OutcomeResultType.NEED_INPUT,
        payload=DiagnosisOutcome(
            findings=[
                Finding(
                    statement="The parsed client log fixes the failing RPC window.",
                    evidence_bindings=[evidence_binding],
                    confidence=0.9,
                )
            ],
            state_delta=_empty_delta([_order_requirement(job)]),
            requested_input=[ORDER_REQUIREMENT_ID],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Provide order_id; retain the parsed run.",
        ),
        evidence=[evidence],
        artifacts=[artifact],
    )
    assert validate_outcome_for_job(job, outcome, manifest) is outcome

    parse_request_bytes = canonical_json_bytes(
        {
            "anchors": [
                {
                    "label": "client",
                    "module": "COMPACT",
                    "pid": "101",
                    "process_name": "checkout-client",
                    "slot": "1",
                },
                {
                    "label": "server",
                    "module": "COMPACT",
                    "pid": "202",
                    "process_name": "inventory-server",
                    "slot": "2",
                },
            ],
            "artifact_proposal_key": LOGPARSE_RUN_KEY,
            "attachment_id": attachment.resource_id,
            "problem_time": "2026-07-31T00:00:03.000Z",
            "schema_version": 1,
        }
    )
    claim = LogparseParseClaim(
        schema_version=1,
        job_id=job.job_id,
        attachment_id=attachment.resource_id,
        attachment_sha256=attachment.sha256,
        artifact_proposal_key=LOGPARSE_RUN_KEY,
        logparse_tool_ref=job.logparse_tool_ref,
        request_sha256=bytes_sha256(parse_request_bytes),
    )
    return job, manifest, outcome, claim, parse_request_bytes, staged


def _normalized_first_outcome(
    outcome: AgentJobOutcome,
    staged: StagedResourceRef,
) -> JobOutcome:
    evidence_draft = outcome.proposed_evidence_drafts[0]
    artifact_draft = outcome.proposed_artifact_drafts[0]
    return JobOutcome(
        outcome_id=outcome.outcome_id,
        job_id=outcome.job_id,
        case_id=outcome.case_id,
        job_type=outcome.job_type,
        base_state_revision=outcome.base_state_revision,
        result_type=outcome.result_type,
        payload=outcome.payload,
        consumed_evidence_refs=outcome.consumed_evidence_refs,
        proposed_evidence=[
            EvidenceProposal(
                proposal_key=evidence_draft.proposal_key,
                source_type=evidence_draft.source_type,
                source_binding=evidence_draft.source_binding,
                locator=evidence_draft.locator,
                summary=evidence_draft.summary,
                content_hash=None,
                staged_resource_ref=None,
            )
        ],
        proposed_artifacts=[
            ArtifactProposal(
                proposal_key=artifact_draft.proposal_key,
                artifact_kind=artifact_draft.artifact_kind,
                name=artifact_draft.name,
                content_type=artifact_draft.content_type,
                resource_kind=artifact_draft.resource_kind,
                size=staged.size,
                sha256=staged.sha256,
                staged_resource_ref=staged,
                metadata=artifact_draft.metadata,
            )
        ],
        error=None,
        produced_at=outcome.produced_at,
        decision_audit=outcome.decision_audit,
    )


def _need_attachment_outcome(job: Job) -> AgentJobOutcome:
    return _agent_outcome(
        job,
        outcome_id="00000000-0000-0000-0000-000000000022",
        result_type=OutcomeResultType.NEED_ATTACHMENT,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta([_attachment_requirement(job)]),
            requested_input=[],
            requested_attachments=[ATTACHMENT_REQUIREMENT_ID],
            candidate_conclusion_draft=None,
            recommended_next_step="Attach exactly one supported incident-log archive.",
        ),
    )


def _reroute_outcome(job: Job) -> AgentJobOutcome:
    return _agent_outcome(
        job,
        outcome_id="00000000-0000-0000-0000-000000000023",
        result_type=OutcomeResultType.REROUTE,
        payload=DiagnosisOutcome(
            findings=[],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=None,
            recommended_next_step="Reroute because this Skill does not cover the symptom.",
        ),
    )


def _completed_outcome(
    job: Job,
) -> tuple[AgentJobOutcome, UserResultPayload, bytes]:
    source_artifact_id = job.artifact_refs[0]
    evidence = AgentEvidenceProposalDraft(
        proposal_key="server-evidence",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=EvidenceSourceBinding(
            existing_source_ref=source_artifact_id,
            artifact_proposal_key=None,
        ),
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path="parse_manifest.json",
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary="The reused parsed run identifies the server-side delay.",
        workspace_relative_path=None,
        declared_size=None,
        declared_sha256=None,
    )
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=evidence.proposal_key,
    )
    mappings = [
        CompletionCriterionDraftMapping(
            criterion_index=index,
            criterion=criterion,
            status="SATISFIED",
            evidence_bindings=[binding],
            explanation="The reused parsed run identifies the timed-out request.",
        )
        for index, criterion in enumerate(
            job.context_snapshot.problem_spec.completion_criteria
        )
    ]
    candidate = CandidateConclusionDraft(
        proposal_key="candidate",
        existing_conclusion_id=None,
        resolution_status="COMPLETE",
        terminal_path_id="complete",
        statement="The inventory RPC exceeded its deadline while waiting for a connection.",
        causal_factors=[
            {
                "factor_id": "primary_cause",
                "role": "CAUSE",
                "statement": "The inventory RPC exceeded its deadline while waiting for a connection.",
                "evidence_bindings": [binding],
                "required_rule_ids": ["causal_chain"],
            }
        ],
        candidate_factors=[],
        excluded_factors=[],
        supporting_evidence_bindings=[binding],
        completion_criteria_mapping=mappings,
    )
    result = UserResultPayload(
        schema_version=3,
        format_id="problem-locator-diagnosis-v3",
        status="COMPLETED",
        source_job_type=job.job_type,
        problem_statement=job.context_snapshot.problem_spec.statement,
        root_cause=candidate.statement,
        findings=[
            {
                "statement": candidate.statement,
                "confidence": 0.95,
                "evidence_bindings": [binding],
                "citations": [
                    {
                        "evidence_binding": binding,
                        "archive_name": None,
                        "line_start": None,
                        "line_end": None,
                        "raw_bytes_sha256": None,
                        "excerpt": None,
                    }
                ],
            }
        ],
        causal_factors=[
            {
                "factor_id": "primary_cause",
                "role": "CAUSE",
                "statement": candidate.statement,
                "evidence_bindings": [binding],
                "required_rule_ids": ["causal_chain"],
                "citations": [
                    {
                        "evidence_binding": binding,
                        "archive_name": None,
                        "line_start": None,
                        "line_end": None,
                        "raw_bytes_sha256": None,
                        "excerpt": None,
                    }
                ],
            }
        ],
        candidate_factors=[],
        excluded_factors=[],
        supporting_evidence_bindings=candidate.supporting_evidence_bindings,
        completion_criteria_mapping=candidate.completion_criteria_mapping,
        verification_rules=[
            {
                "rule_id": "causal_chain",
                "rule_kind": "SEMANTIC_CAUSALITY",
                "status": "SEMANTIC_ONLY",
                "explanation": "The fixture explicitly assesses causality.",
                "evidence_bindings": [binding],
                "citations": [
                    {
                        "evidence_binding": binding,
                        "archive_name": None,
                        "line_start": None,
                        "line_end": None,
                        "raw_bytes_sha256": None,
                        "excerpt": None,
                    }
                ],
                "observed_times": [],
                "event_observations": [],
                "derived_values": [],
                "issues": [],
            }
        ],
        time_relevance={
            "assessment": "UNKNOWN",
            "problem_time": None,
            "derived_anchor_time": None,
            "observations": [],
            "explanation": "No verified event timestamp was available.",
            "citations": [],
        },
        evidence_gaps=[],
        limitations=["No verified event timestamp was available."],
        recommendations=["Submit the candidate to independent review."],
        safety_notes=[
            "This result covers only the fixed Diagnosis Skill scope."
        ],
    )
    result_bytes = canonical_json_bytes(result)
    artifact = AgentArtifactProposalDraft(
        proposal_key=USER_RESULT_KEY,
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=f"output/proposals/{USER_RESULT_KEY}/payload",
        declared_size=len(result_bytes),
        declared_sha256=bytes_sha256(result_bytes),
        metadata=UserResultMetadata(
            schema_version=3,
            format_id="problem-locator-diagnosis-v3",
            description="Diagnosis result",
        ),
    )
    archive = AgentArtifactProposalDraft(
        proposal_key=USER_RESULT_ARCHIVE_KEY,
        artifact_kind=ArtifactKind.USER_RESULT_ARCHIVE,
        name="result.zip",
        content_type="application/zip",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=(
            f"output/proposals/{USER_RESULT_ARCHIVE_KEY}/result.zip"
        ),
        declared_size=1,
        declared_sha256=bytes_sha256(b"x"),
        metadata=UserResultArchiveMetadataV3(
            schema_version=3,
            format_id="problem-locator-result-archive-v3",
            description="Readable diagnosis result and target logs.",
            user_result_proposal_key=USER_RESULT_KEY,
            target_log_count=0,
        ),
    )
    outcome = _agent_outcome(
        job,
        outcome_id="00000000-0000-0000-0000-000000000024",
        result_type=OutcomeResultType.COMPLETED,
        payload=DiagnosisOutcome(
            findings=[
                Finding(
                    statement=candidate.statement,
                    evidence_bindings=[binding],
                    confidence=0.95,
                )
            ],
            state_delta=_empty_delta(),
            requested_input=[],
            requested_attachments=[],
            candidate_conclusion_draft=candidate,
            recommended_next_step="Submit the candidate to independent review.",
        ),
        evidence=[evidence],
        artifacts=[artifact, archive],
    )
    return outcome, result, result_bytes


def test_diagnose_agent_outcome_accepts_all_four_s07_result_types(
    tmp_path: Path,
) -> None:
    first_job, first_manifest, first, _, _, _ = _first_need_input_bundle(
        tmp_path
    )
    attachment_job, attachment_manifest = _job_and_manifest(
        include_attachment=False,
        include_logparse_run=False,
    )
    need_attachment = _need_attachment_outcome(attachment_job)
    reroute = _reroute_outcome(attachment_job)
    completed_job, completed_manifest = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=True,
    )
    completed, _, _ = _completed_outcome(completed_job)

    contexts = [
        (first_job, first_manifest, first),
        (attachment_job, attachment_manifest, need_attachment),
        (attachment_job, attachment_manifest, reroute),
        (completed_job, completed_manifest, completed),
    ]
    assert {outcome.result_type for _, _, outcome in contexts} == {
        OutcomeResultType.NEED_INPUT,
        OutcomeResultType.NEED_ATTACHMENT,
        OutcomeResultType.REROUTE,
        OutcomeResultType.COMPLETED,
    }
    for job, manifest, outcome in contexts:
        assert validate_outcome_for_job(job, outcome, manifest) is outcome
        _schema_round_trip("agent-job-outcome.schema.json", outcome)


def test_first_need_input_binds_claim_exact_bytes_and_normalizes_real_tree(
    tmp_path: Path,
) -> None:
    job, manifest, agent, claim, request_bytes, staged = (
        _first_need_input_bundle(tmp_path)
    )
    assert validate_logparse_claim_for_job(
        claim,
        job,
        manifest,
        request_bytes,
        agent,
    ) is claim
    different_request = canonical_json_bytes(
        {
            **parse_canonical_json_bytes(request_bytes),
            "problem_time": "2026-07-31T00:00:04.000Z",
        }
    )
    with pytest.raises(ValueError, match="request"):
        validate_logparse_claim_for_job(
            claim,
            job,
            manifest,
            different_request,
            agent,
        )

    normalized = _normalized_first_outcome(agent, staged)
    assert validate_outcome_for_job(job, normalized, manifest) is normalized
    assert validate_logparse_claim_for_job(
        claim,
        job,
        manifest,
        request_bytes,
        normalized,
    ) is claim
    _schema_round_trip("job-outcome.schema.json", normalized)
    normalized_payload = normalized.model_dump(mode="json")
    assert "workspace_relative_path" not in str(normalized_payload)
    logparse_artifact = normalized.proposed_artifacts[0]
    assert logparse_artifact.staged_resource_ref.tree_manifest == staged.tree_manifest
    assert logparse_artifact.sha256 == canonical_json_sha256(staged.tree_manifest)


def test_candidate_and_unique_user_result_have_exact_canonical_bytes(
    tmp_path: Path,
) -> None:
    job, manifest = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=True,
    )
    outcome, result, result_bytes = _completed_outcome(job)
    assert validate_outcome_for_job(job, outcome, manifest) is outcome
    assert validate_user_result_for_outcome(job, outcome, result_bytes) == result
    _schema_round_trip("agent-job-outcome.schema.json", outcome)
    _schema_round_trip("user-result.schema.json", result)
    assert result_bytes == canonical_json_bytes(result)
    assert parse_canonical_json_bytes(
        result_bytes,
        model_type=UserResultPayload,
    ) == result

    user_results = [
        artifact
        for artifact in outcome.proposed_artifact_drafts
        if artifact.artifact_kind is ArtifactKind.USER_RESULT
    ]
    assert len(user_results) == 1
    assert user_results[0].proposal_key == USER_RESULT_KEY
    assert user_results[0].metadata == UserResultMetadata(
        schema_version=3,
        format_id="problem-locator-diagnosis-v3",
        description="Diagnosis result",
    )
    assert user_results[0].declared_size == len(result_bytes)
    assert user_results[0].declared_sha256 == bytes_sha256(result_bytes)

    staged = InMemoryResourceStore().stage_file(
        job.job_id,
        USER_RESULT_KEY,
        InMemoryBinaryStream(result_bytes),
        expected_size=len(result_bytes),
        expected_sha256=bytes_sha256(result_bytes),
    )
    archive_bytes = b"x"
    staged_archive = InMemoryResourceStore().stage_file(
        job.job_id,
        USER_RESULT_ARCHIVE_KEY,
        InMemoryBinaryStream(archive_bytes),
        expected_size=len(archive_bytes),
        expected_sha256=bytes_sha256(archive_bytes),
    )
    evidence_draft = outcome.proposed_evidence_drafts[0]
    artifact_drafts = outcome.proposed_artifact_drafts
    staged_by_key = {
        USER_RESULT_KEY: staged,
        USER_RESULT_ARCHIVE_KEY: staged_archive,
    }
    normalized = JobOutcome(
        outcome_id=outcome.outcome_id,
        job_id=outcome.job_id,
        case_id=outcome.case_id,
        job_type=outcome.job_type,
        base_state_revision=outcome.base_state_revision,
        result_type=outcome.result_type,
        payload=outcome.payload,
        consumed_evidence_refs=outcome.consumed_evidence_refs,
        proposed_evidence=[
            EvidenceProposal(
                proposal_key=evidence_draft.proposal_key,
                source_type=evidence_draft.source_type,
                source_binding=evidence_draft.source_binding,
                locator=evidence_draft.locator,
                summary=evidence_draft.summary,
                content_hash=None,
                staged_resource_ref=None,
            )
        ],
        proposed_artifacts=[
            ArtifactProposal(
                proposal_key=artifact_draft.proposal_key,
                artifact_kind=artifact_draft.artifact_kind,
                name=artifact_draft.name,
                content_type=artifact_draft.content_type,
                resource_kind=artifact_draft.resource_kind,
                size=staged_by_key[artifact_draft.proposal_key].size,
                sha256=staged_by_key[artifact_draft.proposal_key].sha256,
                staged_resource_ref=staged_by_key[artifact_draft.proposal_key],
                metadata=artifact_draft.metadata,
            )
            for artifact_draft in artifact_drafts
        ],
        error=None,
        produced_at=outcome.produced_at,
        decision_audit=outcome.decision_audit,
    )
    assert validate_outcome_for_job(job, normalized, manifest) is normalized
    assert validate_user_result_for_outcome(job, normalized, result_bytes) == result
    _schema_round_trip("job-outcome.schema.json", normalized)


def test_user_result_is_rejected_when_candidate_is_absent_missing_or_duplicated() -> None:
    job, _ = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=True,
    )
    outcome, _, _ = _completed_outcome(job)

    without_candidate = outcome.model_dump(mode="json")
    without_candidate["payload"]["candidate_conclusion_draft"] = None
    _assert_agent_contract_rejects(without_candidate)

    missing_result = outcome.model_dump(mode="json")
    missing_result["proposed_artifact_drafts"] = []
    _assert_agent_contract_rejects(missing_result)

    duplicate_result = outcome.model_dump(mode="json")
    duplicate = copy.deepcopy(duplicate_result["proposed_artifact_drafts"][0])
    duplicate["proposal_key"] = "user-result-copy"
    duplicate["workspace_relative_path"] = (
        "output/proposals/user-result-copy/payload"
    )
    duplicate_result["proposed_artifact_drafts"].append(duplicate)
    _assert_agent_contract_rejects(duplicate_result)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("problem_statement", "A different fixed problem statement."),
        ("root_cause", "A different root cause."),
        (
            "supporting_evidence_bindings",
            [
                {
                    "existing_evidence_id": OTHER_EVIDENCE_ID,
                    "evidence_proposal_key": None,
                }
            ],
        ),
        (
            "completion_criteria_mapping",
            [
                {
                    "criterion_index": 0,
                    "criterion": "Identify the timed-out request.",
                    "status": "SATISFIED",
                    "evidence_bindings": [
                        {
                            "existing_evidence_id": OTHER_EVIDENCE_ID,
                            "evidence_proposal_key": None,
                        }
                    ],
                    "explanation": "A semantically different mapping.",
                }
            ],
        ),
    ],
)
def test_public_user_result_seam_rejects_each_semantic_mismatch(
    field_name: str,
    replacement: Any,
) -> None:
    job, manifest = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=True,
    )
    outcome, result, _ = _completed_outcome(job)
    assert validate_outcome_for_job(job, outcome, manifest) is outcome
    result_payload = result.model_dump(mode="json")
    result_payload[field_name] = replacement
    mismatched = UserResultPayload.model_validate(result_payload)
    mismatched_bytes = canonical_json_bytes(mismatched)
    _schema_round_trip("user-result.schema.json", mismatched)

    with pytest.raises(ValueError, match=field_name):
        validate_user_result_for_outcome(job, outcome, mismatched_bytes)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("content_type", "application/problem+json"),
        (
            "metadata",
            {
                "schema_version": 1,
                "format_id": "diagnostic-export-v1",
                "description": "Diagnosis result",
            },
        ),
    ],
)
def test_public_contract_rejects_user_result_content_type_or_metadata(
    field_name: str,
    replacement: Any,
) -> None:
    job, _ = _job_and_manifest(
        include_attachment=True,
        include_logparse_run=True,
    )
    outcome, _, _ = _completed_outcome(job)
    payload = outcome.model_dump(mode="json")
    payload["proposed_artifact_drafts"][0][field_name] = replacement

    _assert_agent_contract_rejects(payload)
