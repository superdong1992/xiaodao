"""Build the public Result v3 payload and server-owned proposal files.

This module is intentionally downstream of the server verifier.  Agent prose is
published only through an Outcome field that crossed the frozen contract seam;
raw-log citations are reconstructed from the verifier's line ranges and the
server-captured authoritative Logparse bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Iterable

from problem_locator.contracts import (
    AgentArtifactProposalDraft,
    ArtifactKind,
    CompletionCriterionDraftMapping,
    CompletionCriterionStatus,
    DiagnosisResolutionStatus,
    DiagnosisOutcome,
    EvidenceBinding,
    Job,
    JobType,
    OutcomeResultType,
    ResourceKind,
    ReviewAssessment,
    ServerRuleStatus,
    UserResultArchiveMetadataV3,
    UserResultCitationV2,
    UserResultFindingV2,
    UserResultFactorV3,
    UserResultMetadataV3,
    UserResultPayloadV3,
    UserResultTimeObservationV2,
    UserResultTimeRelevanceV2,
    UserResultVerificationRuleV2,
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.result_archive import (
    ResultArchiveLog,
    build_result_archive,
    validate_result_archive_bytes,
)

from .authoritative_targets import AuthoritativeTargetSet
from .result_types import CapturedTargetLog, ServerGeneratedResultFile
from .verification_result import VerificationResult


_RESULT_KEY = "server-user-result"
_ARCHIVE_KEY = "server-user-result-archive"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True, slots=True)
class ServerResultBundle:
    """Canonical public report plus its one or two server-owned files."""

    report: UserResultPayloadV3
    files: tuple[ServerGeneratedResultFile, ...]


def _binding_key(binding: EvidenceBinding) -> tuple[str, str]:
    if binding.existing_evidence_id is not None:
        return "existing", binding.existing_evidence_id
    assert binding.evidence_proposal_key is not None
    return "proposal", binding.evidence_proposal_key


def _unique_bindings(values: Iterable[EvidenceBinding]) -> list[EvidenceBinding]:
    result: list[EvidenceBinding] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = _binding_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _unique_text(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _raw_range(
    captured: CapturedTargetLog,
    *,
    line_start: int,
    line_end: int,
) -> tuple[bytes, str]:
    physical = captured.content.splitlines(keepends=True)
    if line_start < 1 or line_end < line_start or line_end > len(physical):
        raise ValueError("DecisionAudit line range escapes the authoritative target log")
    raw = b"".join(physical[line_start - 1 : line_end])
    try:
        excerpt = raw.rstrip(b"\r\n").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("DecisionAudit raw log citation is not UTF-8") from exc
    if not excerpt.strip():
        raise ValueError("DecisionAudit raw log citation is empty")
    return raw, excerpt


def _generic_citation(binding: EvidenceBinding) -> UserResultCitationV2:
    return UserResultCitationV2(
        evidence_binding=binding,
        archive_name=None,
        line_start=None,
        line_end=None,
        raw_bytes_sha256=None,
        excerpt=None,
    )


def _rule_citations(
    evaluation: object,
    captured_logs: tuple[CapturedTargetLog, ...],
    decision_evidence: dict[tuple[str, str, int, str], str],
) -> list[UserResultCitationV2]:
    citations: list[UserResultCitationV2] = []
    represented: set[tuple[str, str]] = set()
    for line_range in evaluation.line_ranges:
        matches = [
            item for item in captured_logs if item.target.log_path == line_range.path
        ]
        if len(matches) == 1:
            captured = matches[0]
            raw, excerpt = _raw_range(
                captured,
                line_start=line_range.line_start,
                line_end=line_range.line_end,
            )
            if bytes_sha256(raw) != line_range.raw_bytes_sha256:
                raise ValueError("DecisionAudit raw-line hash differs from target log bytes")
            target_binding_keys = {
                _binding_key(binding) for binding in captured.evidence_bindings
            }
            bindings = [
                binding
                for binding in evaluation.evidence_bindings
                if _binding_key(binding) in target_binding_keys
            ]
            archive_name = captured.target.archive_name
        else:
            # REVIEW jobs intentionally do not rerun Logparse and therefore
            # have no resolved target plan.  Their server verifier still emits
            # a canonical decision-evidence record for each trusted raw line.
            # Use that server-owned transcript for the JSON-only unresolved
            # report; completed diagnosis archives never take this branch.
            if captured_logs or line_range.line_start != line_range.line_end:
                raise ValueError(
                    "DecisionAudit line range does not name one authoritative target log"
                )
            bindings = []
            excerpt = ""
            for binding in evaluation.evidence_bindings:
                kind, value = _binding_key(binding)
                evidence_ref = value if kind == "existing" else f"proposal:{value}"
                record_key = (
                    evidence_ref,
                    line_range.path,
                    line_range.line_start,
                    line_range.raw_bytes_sha256,
                )
                record_text = decision_evidence.get(record_key)
                if record_text is not None:
                    bindings.append(binding)
                    if excerpt and excerpt != record_text:
                        raise ValueError(
                            "Decision evidence has conflicting raw-line text"
                        )
                    excerpt = record_text
            archive_name = PurePosixPath(line_range.path).name
        if not bindings or not excerpt or not archive_name:
            raise ValueError(
                "DecisionAudit line range lacks an Evidence binding to its target log"
            )
        for binding in bindings:
            represented.add(_binding_key(binding))
            citations.append(
                UserResultCitationV2(
                    evidence_binding=binding,
                    archive_name=archive_name,
                    line_start=line_range.line_start,
                    line_end=line_range.line_end,
                    raw_bytes_sha256=line_range.raw_bytes_sha256,
                    excerpt=excerpt,
                )
            )
    for binding in evaluation.evidence_bindings:
        if _binding_key(binding) not in represented:
            citations.append(_generic_citation(binding))
    # The Pydantic model rejects exact duplicates, but keeping first occurrence
    # gives a stable report even when two rule inputs converge on one raw line.
    unique: list[UserResultCitationV2] = []
    seen: set[tuple[object, ...]] = set()
    for citation in citations:
        key = (
            *_binding_key(citation.evidence_binding),
            citation.archive_name,
            citation.line_start,
            citation.line_end,
            citation.raw_bytes_sha256,
        )
        if key not in seen:
            seen.add(key)
            unique.append(citation)
    return unique


def _verification_rules(
    verification: VerificationResult,
    captured_logs: tuple[CapturedTargetLog, ...],
) -> list[UserResultVerificationRuleV2]:
    decision_evidence: dict[tuple[str, str, int, str], str] = {}
    for raw_record in verification.decision_evidence_bytes.splitlines(keepends=True):
        value = parse_canonical_json_bytes(raw_record)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "evidence_ref",
                "anchor",
                "relative_path",
                "line_number",
                "raw_line",
                "raw_line_sha256",
            }
            or value.get("schema_version") != 1
            or not isinstance(value.get("evidence_ref"), str)
            or not isinstance(value.get("relative_path"), str)
            or not isinstance(value.get("line_number"), int)
            or isinstance(value.get("line_number"), bool)
            or not isinstance(value.get("raw_line"), str)
            or not isinstance(value.get("raw_line_sha256"), str)
        ):
            raise ValueError("server decision-evidence record is invalid")
        key = (
            value["evidence_ref"],
            value["relative_path"],
            value["line_number"],
            value["raw_line_sha256"],
        )
        existing = decision_evidence.get(key)
        if existing is not None and existing != value["raw_line"]:
            raise ValueError("server decision-evidence records conflict")
        decision_evidence[key] = value["raw_line"]
    result: list[UserResultVerificationRuleV2] = []
    for rule in verification.audit.rules:
        evaluation = rule.server_evaluation
        issues = list(evaluation.issues)
        explanation = f"服务端规则核验结果为 {evaluation.status.value}。"
        if issues:
            explanation += "；".join(issues)
        result.append(
            UserResultVerificationRuleV2(
                rule_id=rule.rule_id,
                rule_kind=evaluation.rule_kind,
                status=evaluation.status,
                explanation=explanation,
                evidence_bindings=list(evaluation.evidence_bindings),
                citations=_rule_citations(
                    evaluation,
                    captured_logs,
                    decision_evidence,
                ),
                observed_times=list(evaluation.observed_times),
                event_observations=list(evaluation.event_observations),
                derived_values=list(evaluation.derived_values),
                issues=issues,
            )
        )
    return result


def _finding_citations(
    binding: EvidenceBinding,
    rules: list[UserResultVerificationRuleV2],
) -> list[UserResultCitationV2]:
    key = _binding_key(binding)
    result: list[UserResultCitationV2] = []
    seen: set[tuple[object, ...]] = set()
    for rule in rules:
        for citation in rule.citations:
            if _binding_key(citation.evidence_binding) != key:
                continue
            citation_key = (
                citation.archive_name,
                citation.line_start,
                citation.line_end,
                citation.raw_bytes_sha256,
            )
            if citation_key not in seen:
                seen.add(citation_key)
                result.append(citation)
    return result or [_generic_citation(binding)]


def _findings(
    payload: DiagnosisOutcome | ReviewAssessment,
    *,
    completed: bool,
    verification: VerificationResult,
    rules: list[UserResultVerificationRuleV2],
) -> list[UserResultFindingV2]:
    if not completed or not isinstance(payload, DiagnosisOutcome):
        return []
    verified = {
        _binding_key(binding)
        for binding in verification.audit.required_evidence_bindings
    }
    result: list[UserResultFindingV2] = []
    for finding in payload.findings:
        if not finding.evidence_bindings or any(
            _binding_key(binding) not in verified
            for binding in finding.evidence_bindings
        ):
            raise ValueError("a public Finding is not backed by verified Evidence")
        citations: list[UserResultCitationV2] = []
        for binding in finding.evidence_bindings:
            citations.extend(_finding_citations(binding, rules))
        result.append(
            UserResultFindingV2(
                statement=finding.statement,
                confidence=finding.confidence,
                evidence_bindings=list(finding.evidence_bindings),
                citations=citations,
            )
        )
    return result


def _completion_mappings(
    job: Job,
    payload: DiagnosisOutcome | ReviewAssessment,
    *,
    completed: bool,
) -> list[CompletionCriterionDraftMapping]:
    if completed:
        assert isinstance(payload, DiagnosisOutcome)
        candidate = payload.candidate_conclusion_draft
        assert candidate is not None
        return list(candidate.completion_criteria_mapping)
    if job.job_type is JobType.REVIEW:
        candidate = job.context_snapshot.candidate_conclusion
        if candidate is None:
            raise ValueError("REVIEW user result has no fixed Candidate")
        return [
            CompletionCriterionDraftMapping(
                criterion_index=item.criterion_index,
                criterion=item.criterion,
                status=item.status,
                evidence_bindings=[
                    EvidenceBinding(
                        existing_evidence_id=evidence_id,
                        evidence_proposal_key=None,
                    )
                    for evidence_id in item.evidence_refs
                ],
                explanation=item.explanation,
            )
            for item in candidate.completion_criteria_mapping
        ]
    return [
        CompletionCriterionDraftMapping(
            criterion_index=index,
            criterion=criterion,
            status=CompletionCriterionStatus.UNKNOWN,
            evidence_bindings=[],
            explanation="服务端验证未形成可发布的完成结论。",
        )
        for index, criterion in enumerate(
            job.context_snapshot.problem_spec.completion_criteria
        )
    ]


def _supporting_bindings(
    job: Job,
    payload: DiagnosisOutcome | ReviewAssessment,
    verification: VerificationResult,
    *,
    completed: bool,
) -> list[EvidenceBinding]:
    if completed:
        assert isinstance(payload, DiagnosisOutcome)
        candidate = payload.candidate_conclusion_draft
        assert candidate is not None
        return list(candidate.supporting_evidence_bindings)
    if job.job_type is JobType.REVIEW:
        candidate = job.context_snapshot.candidate_conclusion
        if candidate is not None:
            return [
                EvidenceBinding(
                    existing_evidence_id=evidence_id,
                    evidence_proposal_key=None,
                )
                for evidence_id in candidate.supporting_evidence_refs
            ]
    return _unique_bindings(verification.audit.required_evidence_bindings)


def _time_relevance(
    verification: VerificationResult,
    authoritative_targets: AuthoritativeTargetSet | None,
    rules: list[UserResultVerificationRuleV2],
) -> UserResultTimeRelevanceV2:
    derived_values = _unique_text(
        evaluation.server_evaluation.derived_anchor_time
        for evaluation in verification.audit.rules
        if evaluation.server_evaluation.derived_anchor_time is not None
    )
    problem_time = (
        authoritative_targets.problem_time
        if authoritative_targets is not None
        else (derived_values[0] if len(derived_values) == 1 else None)
    )
    observations: list[UserResultTimeObservationV2] = []
    if problem_time is not None:
        problem = datetime.strptime(problem_time, _TIMESTAMP_FORMAT).replace(
            tzinfo=UTC
        )
        seen: set[tuple[str, str]] = set()
        for rule in verification.audit.rules:
            for observed in rule.server_evaluation.observed_times:
                key = (rule.rule_id, observed)
                if key in seen:
                    continue
                seen.add(key)
                event = datetime.strptime(observed, _TIMESTAMP_FORMAT).replace(
                    tzinfo=UTC
                )
                observations.append(
                    UserResultTimeObservationV2(
                        rule_id=rule.rule_id,
                        event_time=observed,
                        offset_ms=int((event - problem).total_seconds() * 1000),
                    )
                )
    citations: list[UserResultCitationV2] = []
    rule_by_id = {rule.rule_id: rule for rule in rules}
    for observation in observations:
        citations.extend(rule_by_id[observation.rule_id].citations)
    unique_citations: list[UserResultCitationV2] = []
    seen_citations: set[tuple[object, ...]] = set()
    for citation in citations:
        key = (
            *_binding_key(citation.evidence_binding),
            citation.archive_name,
            citation.line_start,
            citation.line_end,
            citation.raw_bytes_sha256,
        )
        if key not in seen_citations:
            seen_citations.add(key)
            unique_citations.append(citation)
    time_window_statuses = [
        item.server_evaluation.status
        for item in verification.audit.rules
        if item.server_evaluation.rule_kind == "EVENT_TIME_WINDOW"
    ]
    if any(status is ServerRuleStatus.VERIFIED_FAIL for status in time_window_statuses):
        assessment = "NOT_RELEVANT"
    elif time_window_statuses and all(
        status is ServerRuleStatus.VERIFIED_PASS for status in time_window_statuses
    ):
        assessment = "RELEVANT"
    else:
        assessment = "UNKNOWN"
    explanation = (
        "观测时间已按问题时间计算毫秒级相对偏移。"
        if observations
        else "没有足够的服务端观测时间可计算与问题时间的相对关系。"
    )
    return UserResultTimeRelevanceV2(
        assessment=assessment,
        problem_time=problem_time,
        derived_anchor_time=(derived_values[0] if len(derived_values) == 1 else None),
        observations=observations,
        explanation=explanation,
        citations=unique_citations,
    )


def _gaps(
    payload: DiagnosisOutcome | ReviewAssessment,
    verification: VerificationResult,
    authoritative_targets: AuthoritativeTargetSet | None,
    *,
    completed: bool,
) -> list[str]:
    values: list[str] = []
    if authoritative_targets is not None:
        values.extend(
            f"目标日志 {target.label} 的匹配状态为 {target.match_status}，无法形成完整日志交付。"
            for target in authoritative_targets.unresolved
        )
    for rule in verification.audit.rules:
        evaluation = rule.server_evaluation
        if evaluation.status in {
            ServerRuleStatus.VERIFIED_FAIL,
            ServerRuleStatus.UNVERIFIABLE,
            ServerRuleStatus.NOT_APPLICABLE,
        }:
            values.append(
                f"服务端规则 {rule.rule_id} 未通过：{evaluation.status.value}。"
            )
            values.extend(evaluation.issues)
    if isinstance(payload, ReviewAssessment):
        values.extend(payload.unsupported_findings)
        values.extend(payload.evidence_conflicts)
        values.extend(payload.missing_evidence)
        values.extend(payload.stale_references)
    result = _unique_text(values)
    if not completed and not result:
        result.append("服务端验证门禁未通过，未形成可发布的根因结论。")
    return result


def _limitations(
    authoritative_targets: AuthoritativeTargetSet | None,
) -> list[str]:
    if authoritative_targets is None:
        return []
    result: list[str] = []
    for target in authoritative_targets.targets:
        if target.match_status != "nearest":
            continue
        if target.caveats:
            result.extend(
                f"目标日志 {target.label} 使用 nearest 匹配：{caveat}"
                for caveat in target.caveats
            )
        else:
            result.append(f"目标日志 {target.label} 使用 nearest 匹配。")
    return _unique_text(result)


def _result_factors(
    payload: DiagnosisOutcome | ReviewAssessment,
    *,
    candidate_result: bool,
    rules: list[UserResultVerificationRuleV2],
) -> tuple[list[UserResultFactorV3], list[UserResultFactorV3], list[UserResultFactorV3]]:
    if not candidate_result or not isinstance(payload, DiagnosisOutcome):
        return [], [], []
    candidate = payload.candidate_conclusion_draft
    assert candidate is not None

    def convert(values) -> list[UserResultFactorV3]:
        result: list[UserResultFactorV3] = []
        for factor in values:
            citations: list[UserResultCitationV2] = []
            for binding in factor.evidence_bindings:
                citations.extend(_finding_citations(binding, rules))
            result.append(
                UserResultFactorV3(
                    factor_id=factor.factor_id,
                    role=factor.role,
                    statement=factor.statement,
                    evidence_bindings=list(factor.evidence_bindings),
                    required_rule_ids=list(factor.required_rule_ids),
                    citations=citations,
                )
            )
        return result

    return (
        convert(candidate.causal_factors),
        convert(candidate.candidate_factors),
        convert(candidate.excluded_factors),
    )


def _result_archive_logs(
    captured_logs: tuple[CapturedTargetLog, ...],
    report: UserResultPayloadV3,
) -> tuple[ResultArchiveLog, ...]:
    public_bindings = _unique_bindings(
        [
            *report.supporting_evidence_bindings,
            *(
                binding
                for mapping in report.completion_criteria_mapping
                for binding in mapping.evidence_bindings
            ),
            *(
                binding
                for finding in report.findings
                for binding in finding.evidence_bindings
            ),
            *(
                binding
                for factor in (
                    report.causal_factors
                    + report.candidate_factors
                    + report.excluded_factors
                )
                for binding in factor.evidence_bindings
            ),
            *(
                binding
                for rule in report.verification_rules
                for binding in rule.evidence_bindings
            ),
        ]
    )
    result: list[ResultArchiveLog] = []
    for item in captured_logs:
        captured_keys = {_binding_key(value) for value in item.evidence_bindings}
        result.append(
            ResultArchiveLog(
                target=item.target,
                content=item.content,
                evidence_bindings=tuple(
                    binding
                    for binding in public_bindings
                    if _binding_key(binding) in captured_keys
                ),
            )
        )
    return tuple(result)


def build_server_result_bundle(
    *,
    job: Job,
    result_type: OutcomeResultType,
    payload: DiagnosisOutcome | ReviewAssessment,
    verification: VerificationResult,
    authoritative_targets: AuthoritativeTargetSet | None,
    captured_logs: tuple[CapturedTargetLog, ...],
) -> ServerResultBundle:
    """Build canonical diagnosis JSON and a ZIP for reviewed candidate results."""

    candidate_result = (
        job.job_type is JobType.DIAGNOSE
        and result_type is OutcomeResultType.COMPLETED
        and isinstance(payload, DiagnosisOutcome)
        and payload.candidate_conclusion_draft is not None
    )
    if candidate_result:
        if not verification.positive_gate_passed:
            raise ValueError("a candidate public result requires the selected path gate")
        if authoritative_targets is not None:
            authoritative_targets.require_deliverable()
            if tuple(item.target for item in captured_logs) != authoritative_targets.targets:
                raise ValueError("the public archive does not cover every resolved target")
        elif captured_logs:
            raise ValueError("target logs require a server-authoritative target set")

    rules = _verification_rules(verification, captured_logs)
    candidate = (
        payload.candidate_conclusion_draft
        if candidate_result and isinstance(payload, DiagnosisOutcome)
        else None
    )
    if candidate is not None:
        report_status = (
            "COMPLETED"
            if candidate.resolution_status is DiagnosisResolutionStatus.COMPLETE
            else "PARTIAL"
        )
    else:
        report_status = "INCONCLUSIVE"
    causal_factors, candidate_factors, excluded_factors = _result_factors(
        payload,
        candidate_result=candidate_result,
        rules=rules,
    )
    report = UserResultPayloadV3(
        schema_version=3,
        format_id="problem-locator-diagnosis-v3",
        status=report_status,
        source_job_type=job.job_type,
        problem_statement=job.context_snapshot.problem_spec.statement,
        root_cause=(
            candidate.statement
            if candidate is not None
            and candidate.resolution_status is DiagnosisResolutionStatus.COMPLETE
            else None
        ),
        findings=_findings(
            payload,
            completed=candidate_result,
            verification=verification,
            rules=rules,
        ),
        causal_factors=causal_factors,
        candidate_factors=candidate_factors,
        excluded_factors=excluded_factors,
        supporting_evidence_bindings=_supporting_bindings(
            job,
            payload,
            verification,
            completed=candidate_result,
        ),
        completion_criteria_mapping=_completion_mappings(
            job,
            payload,
            completed=candidate_result,
        ),
        verification_rules=rules,
        time_relevance=_time_relevance(
            verification,
            authoritative_targets,
            rules,
        ),
        evidence_gaps=_gaps(
            payload,
            verification,
            authoritative_targets,
            completed=(report_status == "COMPLETED"),
        ),
        limitations=_unique_text(
            [
                *(
                    payload.limitations
                    if isinstance(payload, DiagnosisOutcome)
                    else []
                ),
                *_limitations(authoritative_targets),
            ]
        ),
        recommendations=[
            payload.recommended_next_step
            if isinstance(payload, DiagnosisOutcome)
            else payload.recommendation
        ],
        safety_notes=(
            list(payload.safety_notes)
            if isinstance(payload, DiagnosisOutcome)
            else []
        ),
    )
    report_bytes = canonical_json_bytes(report)
    result_draft = AgentArtifactProposalDraft(
        proposal_key=_RESULT_KEY,
        artifact_kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json",
        content_type="application/json",
        resource_kind=ResourceKind.FILE,
        workspace_relative_path=(
            f"output/proposals/{_RESULT_KEY}/diagnosis-result.json"
        ),
        declared_size=len(report_bytes),
        declared_sha256=bytes_sha256(report_bytes),
        metadata=UserResultMetadataV3(
            schema_version=3,
            format_id="problem-locator-diagnosis-v3",
            description="服务端验证后生成的诊断结果 v3。",
        ),
    )
    files = [ServerGeneratedResultFile(draft=result_draft, content=report_bytes)]
    if candidate_result:
        archive_logs = _result_archive_logs(captured_logs, report)
        problem_time = (
            None if authoritative_targets is None else authoritative_targets.problem_time
        )
        archive_bytes = build_result_archive(
            report,
            problem_time=problem_time,
            target_logs=archive_logs,
        )
        validate_result_archive_bytes(
            archive_bytes,
            report=report,
            problem_time=problem_time,
            target_logs=archive_logs,
        )
        archive_draft = AgentArtifactProposalDraft(
            proposal_key=_ARCHIVE_KEY,
            artifact_kind=ArtifactKind.USER_RESULT_ARCHIVE,
            name="result.zip",
            content_type="application/zip",
            resource_kind=ResourceKind.FILE,
            workspace_relative_path=f"output/proposals/{_ARCHIVE_KEY}/result.zip",
            declared_size=len(archive_bytes),
            declared_sha256=bytes_sha256(archive_bytes),
            metadata=UserResultArchiveMetadataV3(
                schema_version=3,
                format_id="problem-locator-result-archive-v3",
                description="服务端验证后生成的诊断结果归档 v3。",
                user_result_proposal_key=_RESULT_KEY,
                target_log_count=len(archive_logs),
            ),
        )
        files.append(
            ServerGeneratedResultFile(draft=archive_draft, content=archive_bytes)
        )
    return ServerResultBundle(report=report, files=tuple(files))


__all__ = ["ServerResultBundle", "build_server_result_bundle"]
