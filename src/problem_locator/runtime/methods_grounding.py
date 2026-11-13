"""Server-owned grounding checks for evidence-first Methods Skill results."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from problem_locator.contracts.enums import MethodsValidationReasonCode

from .methods_skill import ResolvedSpecializedSkillV1


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*\Z")
_DIAGNOSIS_STATUSES = frozenset({"CONFIRMED", "PARTIAL", "INSUFFICIENT"})
_REVIEW_VERDICTS = frozenset({"PASS", "NEED_MORE_EVIDENCE", "REJECT"})


class MethodsValidationError(ValueError):
    """One classified Methods validation failure owned by the Server."""

    def __init__(
        self,
        reason_code: MethodsValidationReasonCode,
        message: str,
    ) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: Any) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(item for item in items if isinstance(item, str) and item.strip()))


def _objects(value: Any) -> tuple[dict[str, Any], ...]:
    return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, (list, tuple)) else ()


def _verdict(value: Any, choices: frozenset[str], default: str) -> str:
    text = _text(value).strip().upper()
    return text if text in choices else default


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("target log relative_path is unsafe")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class FrozenTargetLogV1:
    source_id: str
    relative_path: str
    content_sha256: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("target log source_id is invalid")
        object.__setattr__(self, "relative_path", _safe_relative_path(self.relative_path))
        if not isinstance(self.content, bytes):
            raise TypeError("target log content must be bytes")
        if (
            not isinstance(self.content_sha256, str)
            or _SHA256.fullmatch(self.content_sha256) is None
            or hashlib.sha256(self.content).hexdigest() != self.content_sha256
        ):
            raise ValueError("target log content_sha256 is invalid")
        try:
            self.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("target log must be UTF-8") from exc

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.content.decode("utf-8").splitlines())


@dataclass(frozen=True, slots=True)
class GroundedEvidenceSourceV1:
    source_id: str
    line_number: int
    marker: str
    line: str


@dataclass(frozen=True, slots=True)
class MethodEvidenceV1:
    method_id: str
    summary: str
    identity_tokens: tuple[str, ...]
    sources: tuple[GroundedEvidenceSourceV1, ...]


@dataclass(frozen=True, slots=True)
class MethodDiagnosisDraftV1:
    status: str
    confirmed_methods: tuple[str, ...]
    candidate_methods: tuple[str, ...]
    evidence: tuple[MethodEvidenceV1, ...]
    limitations: tuple[str, ...]
    safety_notes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MethodDiagnosisDraftV1":
        if not isinstance(value, dict):
            raise ValueError("method diagnosis draft must be an object")
        confirmed = _strings(value.get("confirmed_methods"))
        candidates = tuple(item for item in _strings(value.get("candidate_methods")) if item not in confirmed)
        evidence: list[MethodEvidenceV1] = []
        for raw_item in _objects(value.get("evidence")):
            method_id = _text(raw_item.get("method_id"))
            summary = _text(raw_item.get("summary"))
            if not method_id.strip() or not summary.strip():
                continue
            sources: list[GroundedEvidenceSourceV1] = []
            for raw_source in _objects(raw_item.get("sources")):
                source_id = _text(raw_source.get("source_id"))
                line_number = raw_source.get("line_number")
                if not source_id.strip() or type(line_number) is not int or line_number < 1:
                    continue
                source = GroundedEvidenceSourceV1(source_id, line_number,
                    _text(raw_source.get("marker")), _text(raw_source.get("line")))
                if source not in sources:
                    sources.append(source)
            evidence.append(MethodEvidenceV1(method_id, summary,
                _strings(raw_item.get("identity_tokens")), tuple(sources)))
        status = _verdict(value.get("status"), _DIAGNOSIS_STATUSES,
                          "PARTIAL" if confirmed or evidence or candidates else "INSUFFICIENT")
        if status == "INSUFFICIENT" and (confirmed or evidence):
            status = "PARTIAL"
        elif status == "CONFIRMED" and not confirmed and not evidence:
            status = "PARTIAL" if candidates else "INSUFFICIENT"
        return cls(status, confirmed, candidates, tuple(evidence),
                   _strings(value.get("limitations")), _strings(value.get("safety_notes")))



@dataclass(frozen=True, slots=True)
class SkillLoadReceiptV1:
    package_tree_sha256: str
    scanned_source_ids: tuple[str, ...]
    marker_hits: tuple[tuple[str, str, int], ...]
    loaded_method_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MethodGroundingAuditV1:
    schema_version: int
    registration_id: str
    registration_sha256: str
    package_tree_sha256: str
    combined_sha256: str
    logparse_receipt_sha256: str
    status: str
    confirmed_methods: tuple[str, ...]
    evidence_count: int
    checked_source_count: int
    skill_load: SkillLoadReceiptV1
    # Historical audit files omit this field and retain strict semantics.
    validation_mode: str = "strict"

    def __post_init__(self) -> None:
        if self.validation_mode not in {"strict", "advisory"}:
            raise ValueError("Methods evidence validation mode is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedMethodDiagnosisV1:
    draft: MethodDiagnosisDraftV1
    audit: MethodGroundingAuditV1
    # Present after server-owned selection of independently grounded findings.
    selection: Mapping[str, Any] | None = None
    advisory: Mapping[str, Any] | None = None


def marker_occurs(marker: str, line: str) -> bool:
    """Match marker text without changing the frozen evidence bytes."""

    return marker.casefold() in line.casefold()


def scan_method_markers(
    *,
    skill: ResolvedSpecializedSkillV1,
    target_logs: Sequence[FrozenTargetLogV1],
) -> SkillLoadReceiptV1:
    """Scan every frozen log before loading any method-card content."""

    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    logs = tuple(target_logs)
    if not logs:
        raise ValueError("marker scan requires at least one frozen target log")
    source_ids: set[str] = set()
    for item in logs:
        if not isinstance(item, FrozenTargetLogV1):
            raise TypeError("target_logs must contain FrozenTargetLogV1")
        if item.source_id in source_ids:
            raise ValueError("target log source ids must be unique")
        source_ids.add(item.source_id)

    marker_bindings = tuple(
        (method.id, marker, marker.casefold())
        for method in skill.methods.methods
        for marker in method.evidence_markers
    )
    folded_markers = tuple(
        dict.fromkeys(folded for _, _, folded in marker_bindings)
    )
    marker_hits: list[tuple[str, str, int]] = []
    hit_method_ids: set[str] = set()
    for target in logs:
        for line_number, line in enumerate(target.lines, start=1):
            folded_line = line.casefold()
            matched_markers = {
                marker for marker in folded_markers if marker in folded_line
            }
            if not matched_markers:
                continue
            for method_id, marker, folded_marker in marker_bindings:
                if folded_marker in matched_markers:
                    marker_hits.append((target.source_id, marker, line_number))
                    hit_method_ids.add(method_id)
    return SkillLoadReceiptV1(
        package_tree_sha256=skill.package_tree_sha256,
        scanned_source_ids=tuple(item.source_id for item in logs),
        marker_hits=tuple(marker_hits),
        loaded_method_ids=tuple(
            method.id
            for method in skill.methods.methods
            if method.id in hit_method_ids
        ),
    )


def verify_method_evidence(
    item: MethodEvidenceV1,
    *,
    skill: ResolvedSpecializedSkillV1,
    by_source: Mapping[str, FrozenTargetLogV1],
    source_lines: dict[str, tuple[str, ...]],
) -> None:
    """Verify one complete finding without borrowing another finding's sources."""
    if not item.sources:
        raise ValueError("evidence has no cited target log")
    method = skill.methods.method_by_id.get(item.method_id)
    if method is None:
        raise ValueError("evidence names an unknown method")
    seen_sources: set[tuple[str, int]] = set()
    cited_lines: list[str] = []
    for source in item.sources:
        target = by_source.get(source.source_id)
        if target is None:
            raise ValueError("evidence source_id is not a frozen target log")
        source_key = (source.source_id, source.line_number)
        if source_key in seen_sources:
            raise ValueError("evidence sources must not duplicate a target line")
        seen_sources.add(source_key)
        lines = source_lines.get(source.source_id)
        if lines is None:
            lines = target.lines
            source_lines[source.source_id] = lines
        if source.line_number > len(lines):
            raise ValueError("evidence source line_number exceeds the frozen log")
        actual_line = lines[source.line_number - 1]
        if source.line != actual_line:
            raise ValueError("evidence source line differs from the frozen log")
        if source.marker not in method.evidence_markers:
            raise MethodsValidationError(
                MethodsValidationReasonCode.EVIDENCE_MARKER_NOT_INDEXED,
                "evidence marker is not indexed by its method",
            )
        if not marker_occurs(source.marker, actual_line):
            raise ValueError("evidence marker is absent from the cited line")
        cited_lines.append(actual_line)
    if any(not any(token in line for line in cited_lines) for token in item.identity_tokens):
        raise ValueError("identity_tokens must occur in the same evidence sources")


def _verify_method_inputs(
    *, skill: ResolvedSpecializedSkillV1, target_logs: Sequence[FrozenTargetLogV1],
    logparse_receipt_sha256: str, skill_load: SkillLoadReceiptV1,
) -> MethodGroundingAuditV1:
    """Check server-owned inputs once, independently of model judgments."""
    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    if not isinstance(logparse_receipt_sha256, str) or _SHA256.fullmatch(logparse_receipt_sha256) is None:
        raise ValueError("logparse receipt digest is invalid")
    logs = tuple(target_logs)
    expected_skill_load = scan_method_markers(skill=skill, target_logs=logs)
    if skill_load != expected_skill_load:
        raise ValueError("injected Methods cards differ from the frozen marker scan")
    return MethodGroundingAuditV1(
        schema_version=1, registration_id=skill.registration_id,
        registration_sha256=skill.registration_sha256,
        package_tree_sha256=skill.package_tree_sha256, combined_sha256=skill.combined_sha256,
        logparse_receipt_sha256=logparse_receipt_sha256, status="INSUFFICIENT",
        confirmed_methods=(), evidence_count=0, checked_source_count=len(logs), skill_load=skill_load)


def verify_method_diagnosis(
    *, skill: ResolvedSpecializedSkillV1, draft: Mapping[str, Any] | MethodDiagnosisDraftV1,
    target_logs: Sequence[FrozenTargetLogV1], logparse_receipt_sha256: str,
    skill_load: SkillLoadReceiptV1,
) -> VerifiedMethodDiagnosisV1:
    """Keep independently grounded findings without rejecting the whole response."""
    from .methods_selection import diagnosis_mapping, select_method_diagnosis

    value = diagnosis_mapping(draft) if isinstance(draft, MethodDiagnosisDraftV1) else draft
    return select_method_diagnosis(skill=skill, draft=value, target_logs=target_logs,
        logparse_receipt_sha256=logparse_receipt_sha256, skill_load=skill_load)



@dataclass(frozen=True, slots=True)
class MethodReviewFindingV1:
    method_id: str
    identity_tokens: tuple[str, ...]
    verdict: str
    reason: str


@dataclass(frozen=True, slots=True)
class MethodReviewV1:
    verdict: str
    findings: tuple[MethodReviewFindingV1, ...]
    limitations: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MethodReviewV1":
        if not isinstance(value, dict):
            raise ValueError("method review must be an object")
        verdict = _verdict(value.get("verdict"), _REVIEW_VERDICTS, "NEED_MORE_EVIDENCE")
        findings: list[MethodReviewFindingV1] = []
        for raw in _objects(value.get("findings")):
            method_id = _text(raw.get("method_id"))
            if not method_id.strip():
                continue
            finding = MethodReviewFindingV1(method_id, _strings(raw.get("identity_tokens")),
                _verdict(raw.get("verdict"), _REVIEW_VERDICTS, "NEED_MORE_EVIDENCE"),
                _text(raw.get("reason")) or "模型未提供逐项说明。")
            if finding not in findings:
                findings.append(finding)
        return cls(verdict, tuple(findings), _strings(value.get("limitations")))



def verify_method_review(
    diagnosis: VerifiedMethodDiagnosisV1,
    review: Mapping[str, Any] | MethodReviewV1,
) -> MethodReviewV1:
    """Bind independent Review findings to the exact grounded evidence set."""

    if not isinstance(diagnosis, VerifiedMethodDiagnosisV1):
        raise TypeError("diagnosis must be a verified method diagnosis")
    if diagnosis.audit.validation_mode == "advisory":
        from .methods_advisory import parse_advisory_review

        # The whole-result verdict remains the reviewer's decision. Identity
        # spelling and coverage are explanatory metadata in advisory mode.
        return review if isinstance(review, MethodReviewV1) else parse_advisory_review(review)
    parsed = review if isinstance(review, MethodReviewV1) else MethodReviewV1.from_mapping(review)
    aligned: list[MethodReviewFindingV1] = []
    for evidence in diagnosis.draft.evidence:
        identity = (evidence.method_id, tuple(sorted(evidence.identity_tokens)))
        matches = [item for item in parsed.findings
                   if (item.method_id, tuple(sorted(item.identity_tokens))) == identity]
        if matches and len({item.verdict for item in matches}) == 1:
            aligned.append(matches[0])
        else:
            aligned.append(MethodReviewFindingV1(identity[0], identity[1], "NEED_MORE_EVIDENCE",
                "模型未对该项给出明确审核判断。"))
    verdicts = {item.verdict for item in aligned}
    verdict = parsed.verdict
    if "REJECT" in verdicts:
        verdict = "REJECT"
    elif "NEED_MORE_EVIDENCE" in verdicts and verdict == "PASS":
        verdict = "NEED_MORE_EVIDENCE"
    return replace(parsed, verdict=verdict, findings=tuple(aligned))



__all__ = [
    "FrozenTargetLogV1",
    "GroundedEvidenceSourceV1",
    "MethodDiagnosisDraftV1",
    "MethodEvidenceV1",
    "MethodGroundingAuditV1",
    "MethodsValidationError",
    "MethodReviewFindingV1",
    "MethodReviewV1",
    "SkillLoadReceiptV1",
    "VerifiedMethodDiagnosisV1",
    "marker_occurs",
    "scan_method_markers",
    "verify_method_diagnosis",
    "verify_method_review",
]
