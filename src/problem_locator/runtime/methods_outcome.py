"""Map grounded Methods protocol values into the product's persisted domain DTOs.

The Agent never authors an ``AgentJobOutcomeDraftV2`` on the specialized path.
This module is the server-owned bridge that preserves the existing Candidate,
Review, Evidence, and Result persistence model after the hard-cut Methods
protocol has been validated and grounded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    AgentEvidenceCitation,
    AgentEvidenceProposalDraft,
    AgentJobOutcomeDraftV2,
    AgentRuleClaim,
    CandidateConclusionDraft,
    CausalFactorDraft,
    CausalFactorRole,
    CompletionCriterionDraftMapping,
    CompletionCriterionStatus,
    DecisionAuditV2,
    DecisionRuleAudit,
    DiagnosisMode,
    DiagnosisOutcome,
    DiagnosisResolutionStatus,
    DiagnosisStateDelta,
    EvidenceBinding,
    EvidenceSourceBinding,
    EvidenceSourceType,
    Finding,
    Job,
    JobType,
    LogparseEvidenceLocator,
    OutcomeResultType,
    ReviewAssessment,
    ReviewVerdict,
    RuleClaimResult,
    ServerRuleEvaluation,
    ServerRuleStatus,
    VerifiedLogLineRange,
    WorkspaceInputManifest,
    bytes_sha256,
    canonical_json_bytes,
    canonical_json_sha256,
)
from problem_locator.contracts.enums import MethodsValidationReasonCode

from .authoritative_targets import AuthoritativeTargetSet
from .methods_grounding import (
    MethodReviewV1,
    MethodsValidationError,
    VerifiedMethodDiagnosisV1,
    marker_occurs,
)
from .output_reader import ValidatedMethodsPreprocessing, ValidatedProposalResource
from .result_types import CapturedTargetLog
from .verification_result import VerificationResult


_EVIDENCE_RULE_KIND = "METHOD_GROUNDED_EVIDENCE"
_CANDIDATE_RULE_KIND = "METHOD_CANDIDATE"
_SUFFICIENCY_RULE_KIND = "METHOD_EVIDENCE_SUFFICIENCY"
_SEMANTIC_RULE_KIND = "SEMANTIC_CAUSALITY"


class MethodsPreprocessingExecutionLike(Protocol):
    validated: ValidatedMethodsPreprocessing


@dataclass(frozen=True, slots=True)
class MappedMethodsDraft:
    """All immutable values needed by finalization and proposal staging."""

    draft: AgentJobOutcomeDraftV2
    # These are the canonical Agent-authored Methods bytes, not a serialization
    # of the internal domain bridge above.  DecisionAudit and the finalization
    # marker deliberately bind the actual protocol input.
    draft_bytes: bytes
    verification: VerificationResult
    proposal_resources: tuple[ValidatedProposalResource, ...]
    authoritative_targets: AuthoritativeTargetSet | None
    target_logs: tuple[CapturedTargetLog, ...]


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


def _binding_key(binding: EvidenceBinding) -> str:
    if binding.existing_evidence_id is not None:
        return binding.existing_evidence_id
    assert binding.evidence_proposal_key is not None
    return f"proposal:{binding.evidence_proposal_key}"


def _unique_bindings(values: list[EvidenceBinding]) -> list[EvidenceBinding]:
    result: list[EvidenceBinding] = []
    seen: set[str] = set()
    for value in values:
        key = _binding_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _method_rule_id(method_id: str, identity_tokens: tuple[str, ...]) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "method_id": method_id,
                "identity_tokens": sorted(identity_tokens),
            }
        )
    ).hexdigest()[:16]
    return f"methods:{method_id}:{digest}"


def _candidate_rule_id(method_id: str) -> str:
    return f"methods:candidate:{method_id}"


def _factor_id(method_id: str) -> str:
    normalized = "".join(
        character if character.isascii() and character.isalnum() else "_"
        for character in method_id.lower()
    ).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"m_{normalized}"
    value = normalized
    if len(value) <= 64:
        return value
    suffix = hashlib.sha256(method_id.encode("utf-8")).hexdigest()[:10]
    return f"{value[:53]}_{suffix}"


def _physical_line(content: bytes, line_number: int) -> tuple[bytes, str]:
    lines = content.splitlines(keepends=True)
    if line_number < 1 or line_number > len(lines):
        raise ValueError("grounded Methods citation escapes its target log")
    raw = lines[line_number - 1]
    try:
        text = raw.rstrip(b"\r\n").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("grounded Methods citation is not UTF-8") from exc
    if not text.strip():
        raise ValueError("grounded Methods citation is empty")
    return raw, text


def _target_binding(target: CapturedTargetLog) -> EvidenceBinding:
    expected = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key=f"methods-target-{target.target.ordinal}",
    )
    if target.evidence_bindings != (expected,):
        raise ValueError("Methods target Evidence binding is not server-owned")
    return expected


def _evidence_draft(target: CapturedTargetLog) -> AgentEvidenceProposalDraft:
    source = target.target
    if source.log_path is None:
        raise ValueError("Methods target has no deliverable log path")
    if source.source_kind == "OUTPUT_PROPOSAL":
        source_binding = EvidenceSourceBinding(
            existing_source_ref=None,
            artifact_proposal_key=source.source_ref,
        )
    elif source.source_kind == "INPUT_ARTIFACT":
        source_binding = EvidenceSourceBinding(
            existing_source_ref=source.source_ref,
            artifact_proposal_key=None,
        )
    else:  # pragma: no cover - frozen enum exhaustiveness
        raise ValueError("Methods target source kind is unsupported")
    return AgentEvidenceProposalDraft(
        proposal_key=f"methods-target-{source.ordinal}",
        source_type=EvidenceSourceType.LOGPARSE,
        source_binding=source_binding,
        locator=LogparseEvidenceLocator(
            kind="LOGPARSE",
            relative_path=source.log_path,
            start_line=None,
            end_line=None,
            start_time=None,
            end_time=None,
        ),
        summary=f"Server-frozen Logparse target {source.label}.",
        workspace_relative_path=None,
        declared_size=None,
        declared_sha256=None,
    )


def _artifact_drafts(
    resources: tuple[ValidatedProposalResource, ...],
) -> list[AgentArtifactProposalDraft]:
    result: list[AgentArtifactProposalDraft] = []
    for resource in resources:
        if not isinstance(resource.draft, AgentArtifactProposalDraft):
            raise ValueError("Methods preprocessing may stage only a Logparse Artifact")
        resource.verify_unchanged()
        result.append(resource.draft)
    return result


def _decision_evidence_record(
    *,
    binding: EvidenceBinding,
    source_id: str,
    relative_path: str,
    line_number: int,
    raw_line: bytes,
    text: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "evidence_ref": _binding_key(binding),
            "anchor": source_id,
            "relative_path": relative_path,
            "line_number": line_number,
            "raw_line": text,
            "raw_line_sha256": bytes_sha256(raw_line),
        }
    )


def _diagnosis_projection(
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    source_draft_bytes: bytes,
    verified: VerifiedMethodDiagnosisV1,
    preprocessing: MethodsPreprocessingExecutionLike,
) -> MappedMethodsDraft:
    if (
        job.job_type is not JobType.DIAGNOSE
        or job.diagnosis_mode is not DiagnosisMode.SPECIALIZED
        or job.skill_ref is None
        or job.context_snapshot is None
    ):
        raise ValueError("Methods diagnosis mapping requires a specialized DIAGNOSE Job")
    if manifest.job_id != job.job_id or manifest.job_type is not JobType.DIAGNOSE:
        raise ValueError("Methods diagnosis Workspace identity differs from its Job")
    if (
        job.skill_ref.id != f"diagnosis-skill/{verified.audit.registration_id}"
        or job.skill_ref.content_hash != verified.audit.combined_sha256
    ):
        raise ValueError("grounded Methods identity differs from the pinned Job Skill")

    validated = preprocessing.validated
    target_logs = validated.target_logs
    if not target_logs or tuple(item.target for item in target_logs) != tuple(
        item for item in validated.authoritative_targets.targets if item.deliverable
    ):
        raise ValueError("Methods target capture differs from the authoritative target set")
    by_source = {item.target.label: item for item in target_logs}
    if len(by_source) != len(target_logs):
        raise ValueError("Methods target source IDs are not unique")
    bindings_by_source = {
        source_id: _target_binding(item) for source_id, item in by_source.items()
    }
    all_bindings = [bindings_by_source[item.target.label] for item in target_logs]
    evidence_drafts = [_evidence_draft(item) for item in target_logs]
    proposal_resources = validated.proposal_resources
    artifact_drafts = _artifact_drafts(proposal_resources)
    artifact_keys = {item.proposal_key for item in artifact_drafts}
    for item in evidence_drafts:
        source_key = item.source_binding.artifact_proposal_key
        if source_key is not None and source_key not in artifact_keys:
            raise ValueError("Methods Evidence names an absent Logparse proposal")

    rule_claims: list[AgentRuleClaim] = []
    audit_rules: list[DecisionRuleAudit] = []
    evidence_rule_ids: dict[str, list[str]] = {}
    evidence_bindings_by_method: dict[str, list[EvidenceBinding]] = {}
    decision_evidence: list[bytes] = []
    seen_records: set[tuple[str, str, int]] = set()
    for evidence in verified.draft.evidence:
        rule_id = _method_rule_id(evidence.method_id, evidence.identity_tokens)
        evidence_rule_ids.setdefault(evidence.method_id, []).append(rule_id)
        citations: list[AgentEvidenceCitation] = []
        evaluation_bindings: list[EvidenceBinding] = []
        line_ranges: list[VerifiedLogLineRange] = []
        for source in evidence.sources:
            captured = by_source.get(source.source_id)
            if captured is None or captured.target.log_path is None:
                raise ValueError("grounded Methods source is not an authoritative target")
            binding = bindings_by_source[source.source_id]
            raw_line, text = _physical_line(captured.content, source.line_number)
            if text != source.line or not marker_occurs(source.marker, text):
                raise MethodsValidationError(
                    MethodsValidationReasonCode.EVIDENCE_SOURCE_CHANGED,
                    "grounded Methods source changed before Outcome mapping",
                )
            citations.append(
                AgentEvidenceCitation(
                    evidence_binding=binding,
                    line_start=source.line_number,
                    line_end=source.line_number,
                )
            )
            evaluation_bindings.append(binding)
            line_ranges.append(
                VerifiedLogLineRange(
                    path=captured.target.log_path,
                    line_start=source.line_number,
                    line_end=source.line_number,
                    raw_bytes_sha256=bytes_sha256(raw_line),
                )
            )
            record_key = (
                _binding_key(binding),
                captured.target.log_path,
                source.line_number,
            )
            if record_key not in seen_records:
                seen_records.add(record_key)
                decision_evidence.append(
                    _decision_evidence_record(
                        binding=binding,
                        source_id=source.source_id,
                        relative_path=captured.target.log_path,
                        line_number=source.line_number,
                        raw_line=raw_line,
                        text=text,
                    )
                )
        evaluation_bindings = _unique_bindings(evaluation_bindings)
        method_bindings = evidence_bindings_by_method.setdefault(
            evidence.method_id, []
        )
        method_bindings[:] = _unique_bindings([*method_bindings, *evaluation_bindings])
        claim = AgentRuleClaim(
            rule_id=rule_id,
            claimed_result=RuleClaimResult.PASS,
            fact_refs=[],
            citations=citations,
            explanation=(
                f"Grounded Methods evidence for {evidence.method_id}; "
                f"identity_tokens={list(evidence.identity_tokens)!r}."
            ),
        )
        rule_claims.append(claim)
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule_id,
                agent_claim=claim,
                server_evaluation=ServerRuleEvaluation(
                    rule_id=rule_id,
                    rule_kind=_EVIDENCE_RULE_KIND,
                    status=ServerRuleStatus.VERIFIED_PASS,
                    fact_refs=[],
                    evidence_bindings=evaluation_bindings,
                    anchor_id=None,
                    derived_anchor_time=None,
                    observed_times=[],
                    event_observations=[],
                    derived_values=[],
                    line_ranges=line_ranges,
                    issues=[],
                ),
            )
        )

    candidate_rule_ids: dict[str, str] = {}
    for method_id in verified.draft.candidate_methods:
        rule_id = _candidate_rule_id(method_id)
        candidate_rule_ids[method_id] = rule_id
        claim = AgentRuleClaim(
            rule_id=rule_id,
            claimed_result=RuleClaimResult.UNKNOWN,
            fact_refs=[],
            citations=[],
            explanation=f"Method {method_id} remains a candidate without grounded confirmation.",
        )
        rule_claims.append(claim)
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule_id,
                agent_claim=claim,
                server_evaluation=ServerRuleEvaluation(
                    rule_id=rule_id,
                    rule_kind=_CANDIDATE_RULE_KIND,
                    status=ServerRuleStatus.UNVERIFIABLE,
                    fact_refs=[],
                    evidence_bindings=list(all_bindings),
                    anchor_id=None,
                    derived_anchor_time=None,
                    observed_times=[],
                    event_observations=[],
                    derived_values=[],
                    line_ranges=[],
                    issues=["No grounded confirming marker was cited for this method."],
                ),
            )
        )

    has_confirmed = bool(verified.draft.confirmed_methods)
    if not audit_rules:
        rule_id = "methods:evidence-sufficiency"
        claim = AgentRuleClaim(
            rule_id=rule_id,
            claimed_result=RuleClaimResult.UNKNOWN,
            fact_refs=[],
            citations=[],
            explanation="The Methods pass found insufficient grounded evidence.",
        )
        rule_claims.append(claim)
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule_id,
                agent_claim=claim,
                server_evaluation=ServerRuleEvaluation(
                    rule_id=rule_id,
                    rule_kind=_SUFFICIENCY_RULE_KIND,
                    status=ServerRuleStatus.UNVERIFIABLE,
                    fact_refs=[],
                    evidence_bindings=list(all_bindings),
                    anchor_id=None,
                    derived_anchor_time=None,
                    observed_times=[],
                    event_observations=[],
                    derived_values=[],
                    line_ranges=[],
                    issues=["No method has server-grounded evidence."],
                ),
            )
        )

    complete = (
        verified.draft.status == "CONFIRMED"
        and has_confirmed
        and not verified.draft.candidate_methods
    )
    resolution = (
        DiagnosisResolutionStatus.COMPLETE
        if complete
        else DiagnosisResolutionStatus.PARTIAL
    )
    candidate: CandidateConclusionDraft | None = None
    if has_confirmed:
        summaries_by_method = {
            method_id: " ".join(
                item.summary
                for item in verified.draft.evidence
                if item.method_id == method_id
            )
            for method_id in verified.draft.confirmed_methods
        }
        causal_factors = [
            CausalFactorDraft(
                factor_id=_factor_id(method_id),
                role=CausalFactorRole.CAUSE,
                statement=(
                    f"已确认定位方法 {method_id}：{summaries_by_method[method_id]}"
                ),
                evidence_bindings=evidence_bindings_by_method[method_id],
                required_rule_ids=evidence_rule_ids[method_id],
            )
            for method_id in verified.draft.confirmed_methods
        ]
        candidate_factors = [
            CausalFactorDraft(
                factor_id=_factor_id(method_id),
                role=CausalFactorRole.CONTRIBUTOR,
                statement=f"待确认定位方法：{method_id}。",
                evidence_bindings=list(all_bindings),
                required_rule_ids=[candidate_rule_ids[method_id]],
            )
            for method_id in verified.draft.candidate_methods
        ]
        criteria = job.context_snapshot.problem_spec.completion_criteria
        completion = [
            CompletionCriterionDraftMapping(
                criterion_index=index,
                criterion=criterion,
                status=(
                    CompletionCriterionStatus.SATISFIED
                    if complete
                    else (
                        CompletionCriterionStatus.PARTIALLY_SATISFIED
                        if index == 0
                        else CompletionCriterionStatus.UNKNOWN
                    )
                ),
                evidence_bindings=(list(all_bindings) if complete or index == 0 else []),
                explanation=(
                    "Every confirmed method is grounded in immutable target-log lines."
                    if complete
                    else "Grounded method evidence exists, but the Methods result remains partial."
                ),
            )
            for index, criterion in enumerate(criteria)
        ]
        summaries = [item.summary for item in verified.draft.evidence]
        candidate = CandidateConclusionDraft(
            proposal_key="methods-candidate",
            existing_conclusion_id=None,
            resolution_status=resolution,
            terminal_path_id=("methods_complete" if complete else "methods_partial"),
            statement=" ".join(summaries),
            causal_factors=causal_factors,
            candidate_factors=candidate_factors,
            excluded_factors=[],
            supporting_evidence_bindings=list(all_bindings),
            completion_criteria_mapping=completion,
        )

    findings = [
        Finding(
            statement=item.summary,
            evidence_bindings=evidence_bindings_by_method[item.method_id],
            confidence=1.0,
        )
        for item in verified.draft.evidence
    ]
    result_type = (
        OutcomeResultType.COMPLETED if candidate is not None else OutcomeResultType.INCONCLUSIVE
    )
    payload = DiagnosisOutcome(
        findings=findings if candidate is not None else [],
        state_delta=_empty_delta(),
        requested_input=[],
        requested_attachments=[],
        candidate_conclusion_draft=candidate,
        recommended_next_step=(
            "请根据已确认的定位方法处理对应异常；实施变更前先核对安全说明，修复后按完成条件复验。"
            if candidate is not None
            else "请补充覆盖证据缺口的目标日志，再创建新的定位任务。"
        ),
        limitations=list(verified.draft.limitations),
        safety_notes=list(verified.draft.safety_notes),
    )
    draft = AgentJobOutcomeDraftV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=result_type,
        payload=payload,
        consumed_evidence_refs=list(job.evidence_refs),
        proposed_evidence_drafts=evidence_drafts,
        proposed_artifact_drafts=artifact_drafts,
        error=None,
        rule_claims=rule_claims,
    )
    terminal_status = resolution.value if candidate is not None else "NONE"
    terminal_path = candidate.terminal_path_id if candidate is not None else "methods_none"
    subject_hash = canonical_json_sha256(
        {
            "schema_version": 1,
            "protocol": "methods-diagnosis-v1",
            "job_id": job.job_id,
            "case_id": job.case_id,
            "skill_ref": job.skill_ref.model_dump(mode="json"),
            "problem_spec": job.context_snapshot.problem_spec.model_dump(mode="json"),
            "registration_sha256": verified.audit.registration_sha256,
            "package_tree_sha256": verified.audit.package_tree_sha256,
            "combined_sha256": verified.audit.combined_sha256,
            "logparse_receipt_sha256": verified.audit.logparse_receipt_sha256,
            "required_rule_ids": [item.rule_id for item in audit_rules],
        }
    )
    evidence_bytes = b"".join(decision_evidence)
    if len(evidence_bytes) > job.resource_limits.context_bytes:
        raise ValueError("Methods decision evidence exceeds the Job context limit")
    audit = DecisionAuditV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        skill_ref=job.skill_ref,
        source_draft_sha256=bytes_sha256(source_draft_bytes),
        subject_hash=subject_hash,
        candidate_target=None,
        diagnosis_audit_hash=None,
        selected_terminal_path_id=terminal_path,
        terminal_resolution_status=terminal_status,
        required_rule_ids=[item.rule_id for item in audit_rules],
        required_evidence_bindings=list(all_bindings),
        rules=audit_rules,
    )
    return MappedMethodsDraft(
        draft=draft,
        draft_bytes=source_draft_bytes,
        verification=VerificationResult(
            audit=audit,
            positive_gate_passed=candidate is not None,
            decision_evidence_bytes=evidence_bytes,
        ),
        proposal_resources=proposal_resources,
        authoritative_targets=validated.authoritative_targets,
        target_logs=target_logs,
    )


def _review_projection(
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    source_draft_bytes: bytes,
    diagnosis: VerifiedMethodDiagnosisV1,
    review: MethodReviewV1,
    diagnosis_audit: DecisionAuditV2,
) -> MappedMethodsDraft:
    if (
        job.job_type is not JobType.REVIEW
        or job.skill_ref is None
        or job.review_target is None
        or job.context_snapshot is None
        or job.context_snapshot.candidate_conclusion is None
        or manifest.review_subject is None
    ):
        raise ValueError("Methods review mapping requires one frozen REVIEW subject")
    if (
        manifest.job_id != job.job_id
        or manifest.job_type is not JobType.REVIEW
        or diagnosis_audit.job_type is not JobType.DIAGNOSE
        or diagnosis_audit.case_id != job.case_id
        or diagnosis_audit.skill_ref != job.skill_ref
        or diagnosis_audit.required_rule_ids
        != manifest.review_subject.required_rule_ids
    ):
        raise ValueError("Methods review is not bound to its diagnosis audit")
    evidence_rules = [
        _method_rule_id(item.method_id, item.identity_tokens)
        for item in diagnosis.draft.evidence
    ]
    candidate_rules = [
        _candidate_rule_id(method_id)
        for method_id in diagnosis.draft.candidate_methods
    ]
    expected_rules = [*evidence_rules, *candidate_rules]
    if expected_rules != diagnosis_audit.required_rule_ids:
        raise ValueError("Methods diagnosis rule identities changed before review")
    finding_by_identity = {
        (item.method_id, tuple(sorted(item.identity_tokens))): item
        for item in review.findings
    }
    facts_by_rule: dict[str, list[str]] = {}
    for fact in manifest.review_subject.mechanical_facts:
        refs = facts_by_rule.setdefault(fact.source_rule_id, [])
        refs.extend(ref for ref in fact.evidence_refs if ref not in refs)

    rule_claims: list[AgentRuleClaim] = []
    audit_rules: list[DecisionRuleAudit] = []
    for evidence, rule_id in zip(diagnosis.draft.evidence, evidence_rules, strict=True):
        finding = finding_by_identity[
            (evidence.method_id, tuple(sorted(evidence.identity_tokens)))
        ]
        claimed = {
            "PASS": RuleClaimResult.PASS,
            "NEED_MORE_EVIDENCE": RuleClaimResult.UNKNOWN,
            "REJECT": RuleClaimResult.FAIL,
        }[finding.verdict]
        bindings = [
            EvidenceBinding(existing_evidence_id=ref, evidence_proposal_key=None)
            for ref in facts_by_rule.get(rule_id, [])
        ]
        claim = AgentRuleClaim(
            rule_id=rule_id,
            claimed_result=claimed,
            fact_refs=[],
            citations=[],
            explanation=finding.reason,
        )
        rule_claims.append(claim)
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule_id,
                agent_claim=claim,
                server_evaluation=ServerRuleEvaluation(
                    rule_id=rule_id,
                    rule_kind=_SEMANTIC_RULE_KIND,
                    status=ServerRuleStatus.SEMANTIC_ONLY,
                    fact_refs=[],
                    evidence_bindings=bindings,
                    anchor_id=None,
                    derived_anchor_time=None,
                    observed_times=[],
                    event_observations=[],
                    derived_values=[],
                    line_ranges=[],
                    issues=[],
                ),
            )
        )

    for method_id, rule_id in zip(
        diagnosis.draft.candidate_methods,
        candidate_rules,
        strict=True,
    ):
        bindings = [
            EvidenceBinding(existing_evidence_id=ref, evidence_proposal_key=None)
            for ref in facts_by_rule.get(rule_id, [])
        ]
        claim = AgentRuleClaim(
            rule_id=rule_id,
            claimed_result=RuleClaimResult.UNKNOWN,
            fact_refs=[],
            citations=[],
            explanation=(
                f"Method {method_id} remained unconfirmed in the grounded diagnosis."
            ),
        )
        rule_claims.append(claim)
        audit_rules.append(
            DecisionRuleAudit(
                rule_id=rule_id,
                agent_claim=claim,
                server_evaluation=ServerRuleEvaluation(
                    rule_id=rule_id,
                    rule_kind=_CANDIDATE_RULE_KIND,
                    status=ServerRuleStatus.UNVERIFIABLE,
                    fact_refs=[],
                    evidence_bindings=bindings,
                    anchor_id=None,
                    derived_anchor_time=None,
                    observed_times=[],
                    event_observations=[],
                    derived_values=[],
                    line_ranges=[],
                    issues=["The diagnosis supplied no grounded identity for this candidate method."],
                ),
            )
        )

    verdict = ReviewVerdict(review.verdict)
    reasons = [item.reason for item in review.findings if item.verdict != "PASS"]
    assessment = ReviewAssessment(
        candidate_conclusion_id=job.review_target.candidate_conclusion_id,
        candidate_revision=job.review_target.candidate_revision,
        candidate_content_hash=job.review_target.candidate_content_hash,
        reviewed_state_revision=job.base_state_revision,
        reviewed_evidence_refs=list(manifest.review_subject.required_evidence_refs),
        verdict=verdict,
        unsupported_findings=(reasons if verdict is ReviewVerdict.REJECT else []),
        evidence_conflicts=[],
        missing_evidence=(
            reasons if verdict is ReviewVerdict.NEED_MORE_EVIDENCE else []
        ),
        stale_references=[],
        requested_requirement_ids=[],
        recommendation=(
            "Accept the independently reviewed Methods Candidate."
            if verdict is ReviewVerdict.PASS
            else "Do not accept the Candidate without resolving the review findings."
        ),
    )
    draft = AgentJobOutcomeDraftV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.COMPLETED,
        payload=assessment,
        consumed_evidence_refs=list(manifest.review_subject.required_evidence_refs),
        proposed_evidence_drafts=[],
        proposed_artifact_drafts=[],
        error=None,
        rule_claims=rule_claims,
    )
    candidate = job.context_snapshot.candidate_conclusion
    required_bindings = [
        EvidenceBinding(existing_evidence_id=ref, evidence_proposal_key=None)
        for ref in manifest.review_subject.required_evidence_refs
    ]
    audit = DecisionAuditV2(
        schema_version=2,
        job_id=job.job_id,
        case_id=job.case_id,
        job_type=job.job_type,
        skill_ref=job.skill_ref,
        source_draft_sha256=bytes_sha256(source_draft_bytes),
        subject_hash=manifest.review_subject.subject_hash,
        candidate_target=job.review_target,
        diagnosis_audit_hash=bytes_sha256(canonical_json_bytes(diagnosis_audit)),
        selected_terminal_path_id=candidate.terminal_path_id,
        terminal_resolution_status=candidate.resolution_status.value,
        required_rule_ids=expected_rules,
        required_evidence_bindings=required_bindings,
        rules=audit_rules,
    )
    return MappedMethodsDraft(
        draft=draft,
        draft_bytes=source_draft_bytes,
        verification=VerificationResult(
            audit=audit,
            positive_gate_passed=True,
            decision_evidence_bytes=b"",
        ),
        proposal_resources=(),
        authoritative_targets=None,
        target_logs=(),
    )


def map_verified_methods_draft(
    *,
    job: Job,
    manifest: WorkspaceInputManifest,
    source_draft_bytes: bytes,
    verified_diagnosis: VerifiedMethodDiagnosisV1,
    verified_review: MethodReviewV1 | None = None,
    preprocessing: MethodsPreprocessingExecutionLike | None = None,
    diagnosis_audit: DecisionAuditV2 | None = None,
) -> MappedMethodsDraft:
    """Map exactly one verified Methods DIAGNOSE or REVIEW protocol value."""

    if not isinstance(source_draft_bytes, bytes) or not source_draft_bytes:
        raise ValueError("Methods source draft bytes must be non-empty")
    if job.job_type is JobType.DIAGNOSE:
        if verified_review is not None or preprocessing is None or diagnosis_audit is not None:
            raise ValueError("Methods DIAGNOSE mapping arguments are inconsistent")
        return _diagnosis_projection(
            job=job,
            manifest=manifest,
            source_draft_bytes=source_draft_bytes,
            verified=verified_diagnosis,
            preprocessing=preprocessing,
        )
    if job.job_type is JobType.REVIEW:
        if verified_review is None or preprocessing is not None or diagnosis_audit is None:
            raise ValueError("Methods REVIEW mapping arguments are inconsistent")
        return _review_projection(
            job=job,
            manifest=manifest,
            source_draft_bytes=source_draft_bytes,
            diagnosis=verified_diagnosis,
            review=verified_review,
            diagnosis_audit=diagnosis_audit,
        )
    raise ValueError("Methods outcome mapping supports only DIAGNOSE and REVIEW")


__all__ = [
    "MappedMethodsDraft",
    "MethodsPreprocessingExecutionLike",
    "map_verified_methods_draft",
]
