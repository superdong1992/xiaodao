"""Server-owned grounding checks for evidence-first Methods Skill results."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from problem_locator.contracts.enums import MethodsValidationReasonCode

from .methods_skill import ResolvedSpecializedSkillV1


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*\Z")
_DIAGNOSIS_STATUSES = frozenset({"CONFIRMED", "PARTIAL", "INSUFFICIENT"})
_REVIEW_VERDICTS = frozenset({"PASS", "NEED_MORE_EVIDENCE", "REJECT"})
_DRAFT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "confirmed_methods",
        "candidate_methods",
        "evidence",
        "limitations",
        "safety_notes",
    }
)
_EVIDENCE_FIELDS = frozenset({"method_id", "summary", "identity_tokens", "sources"})
_SOURCE_FIELDS = frozenset({"source_id", "line_number", "marker", "line"})
_REVIEW_FIELDS = frozenset({"schema_version", "verdict", "findings", "limitations"})
_REVIEW_FINDING_FIELDS = frozenset({"method_id", "identity_tokens", "verdict", "reason"})


class MethodsValidationError(ValueError):
    """One classified Methods validation failure owned by the Server."""

    def __init__(
        self,
        reason_code: MethodsValidationReasonCode,
        message: str,
    ) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _exact(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")


def _nonblank(value: Any, *, label: str, maximum_bytes: int = 65_536) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise ValueError(f"{label} must be bounded non-blank UTF-8 text")
    return value


def _string_array(
    value: Any,
    *,
    label: str,
    allow_empty: bool = True,
    maximum: int = 200,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(not isinstance(item, str) or not item or item.isspace() for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} must contain unique non-blank strings")
    return tuple(value)


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
        _exact(value, _DRAFT_FIELDS, "method diagnosis draft")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("method diagnosis draft schema_version must equal integer 1")
        status = value["status"]
        if status not in _DIAGNOSIS_STATUSES:
            raise ValueError("method diagnosis status is invalid")
        confirmed = _string_array(value["confirmed_methods"], label="confirmed_methods")
        candidates = _string_array(value["candidate_methods"], label="candidate_methods")
        if set(confirmed) & set(candidates):
            raise ValueError("confirmed_methods and candidate_methods must be disjoint")
        evidence_raw = value["evidence"]
        if not isinstance(evidence_raw, list) or len(evidence_raw) > 500:
            raise ValueError("evidence must be a bounded array")
        evidence: list[MethodEvidenceV1] = []
        for evidence_index, raw_item in enumerate(evidence_raw):
            if not isinstance(raw_item, dict):
                raise ValueError(f"evidence[{evidence_index}] must be an object")
            _exact(raw_item, _EVIDENCE_FIELDS, f"evidence[{evidence_index}]")
            method_id = _nonblank(raw_item["method_id"], label="evidence method_id", maximum_bytes=256)
            summary = _nonblank(raw_item["summary"], label="evidence summary")
            tokens = _string_array(
                raw_item["identity_tokens"],
                label="evidence identity_tokens",
                allow_empty=False,
                maximum=100,
            )
            sources_raw = raw_item["sources"]
            if not isinstance(sources_raw, list) or not sources_raw or len(sources_raw) > 100:
                raise ValueError("evidence sources must be a non-empty bounded array")
            sources: list[GroundedEvidenceSourceV1] = []
            for source_index, raw_source in enumerate(sources_raw):
                if not isinstance(raw_source, dict):
                    raise ValueError("evidence source must be an object")
                _exact(raw_source, _SOURCE_FIELDS, f"evidence source {source_index}")
                source_id = _nonblank(raw_source["source_id"], label="source_id", maximum_bytes=128)
                line_number = raw_source["line_number"]
                if type(line_number) is not int or line_number < 1:
                    raise ValueError("evidence source line_number must be positive")
                marker = _nonblank(raw_source["marker"], label="source marker", maximum_bytes=1024)
                line = _nonblank(raw_source["line"], label="source line")
                if "\n" in marker or "\r" in marker or "\n" in line or "\r" in line:
                    raise ValueError("evidence source marker and line must be single-line")
                sources.append(GroundedEvidenceSourceV1(source_id, line_number, marker, line))
            evidence.append(MethodEvidenceV1(method_id, summary, tokens, tuple(sources)))
        limitations = _string_array(value["limitations"], label="limitations")
        safety_notes = _string_array(value["safety_notes"], label="safety_notes")
        if status == "CONFIRMED" and not confirmed:
            raise ValueError("CONFIRMED requires at least one confirmed method")
        if status == "INSUFFICIENT" and (confirmed or evidence):
            raise ValueError("INSUFFICIENT forbids confirmed methods and evidence")
        return cls(status, confirmed, candidates, tuple(evidence), limitations, safety_notes)


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


@dataclass(frozen=True, slots=True)
class VerifiedMethodDiagnosisV1:
    draft: MethodDiagnosisDraftV1
    audit: MethodGroundingAuditV1


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

    marker_hits: list[tuple[str, str, int]] = []
    hit_method_ids: set[str] = set()
    for target in logs:
        for line_number, line in enumerate(target.lines, start=1):
            for method in skill.methods.methods:
                for marker in method.evidence_markers:
                    if marker_occurs(marker, line):
                        marker_hits.append((target.source_id, marker, line_number))
                        hit_method_ids.add(method.id)
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


def verify_method_diagnosis(
    *,
    skill: ResolvedSpecializedSkillV1,
    draft: Mapping[str, Any] | MethodDiagnosisDraftV1,
    target_logs: Sequence[FrozenTargetLogV1],
    logparse_receipt_sha256: str,
    skill_load: SkillLoadReceiptV1,
) -> VerifiedMethodDiagnosisV1:
    """Ground every claimed source against immutable target-log bytes."""

    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    diagnosis = (
        draft
        if isinstance(draft, MethodDiagnosisDraftV1)
        else MethodDiagnosisDraftV1.from_mapping(draft)
    )
    if not isinstance(logparse_receipt_sha256, str) or _SHA256.fullmatch(logparse_receipt_sha256) is None:
        raise ValueError("logparse receipt digest is invalid")
    logs = tuple(target_logs)
    if not logs:
        raise ValueError("grounding requires at least one frozen target log")
    by_source: dict[str, FrozenTargetLogV1] = {}
    for item in logs:
        if not isinstance(item, FrozenTargetLogV1):
            raise TypeError("target_logs must contain FrozenTargetLogV1")
        if item.source_id in by_source:
            raise ValueError("target log source ids must be unique")
        by_source[item.source_id] = item

    methods = skill.methods.method_by_id
    named_methods = {*diagnosis.confirmed_methods, *diagnosis.candidate_methods}
    unknown_methods = named_methods - set(methods)
    if unknown_methods:
        raise ValueError(f"diagnosis names unknown methods: {sorted(unknown_methods)!r}")

    expected_skill_load = scan_method_markers(skill=skill, target_logs=logs)
    if not isinstance(skill_load, SkillLoadReceiptV1):
        raise TypeError("skill_load must be the pre-context marker scan receipt")
    if skill_load != expected_skill_load:
        raise ValueError("injected Methods cards differ from the frozen marker scan")
    hit_method_ids = set(skill_load.loaded_method_ids)
    if any(method_id not in hit_method_ids for method_id in diagnosis.confirmed_methods):
        raise MethodsValidationError(
            MethodsValidationReasonCode.CONFIRMED_MARKER_SCAN_MISS,
            "confirmed method has no positive marker in the full target-log scan",
        )

    evidence_method_ids: set[str] = set()
    evidence_identities: set[tuple[str, tuple[str, ...]]] = set()
    for item in diagnosis.evidence:
        method = methods.get(item.method_id)
        if method is None:
            raise ValueError("evidence names an unknown method")
        if item.method_id not in diagnosis.confirmed_methods:
            raise ValueError("evidence may only support a confirmed method")
        evidence_method_ids.add(item.method_id)
        identity = (item.method_id, tuple(sorted(item.identity_tokens)))
        if identity in evidence_identities:
            raise ValueError("method evidence identities must be unique")
        evidence_identities.add(identity)
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
            lines = target.lines
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
    if evidence_method_ids != set(diagnosis.confirmed_methods):
        raise MethodsValidationError(
            MethodsValidationReasonCode.CONFIRMED_EVIDENCE_MISSING,
            "every confirmed method must have grounded evidence",
        )

    audit = MethodGroundingAuditV1(
        schema_version=1,
        registration_id=skill.registration_id,
        registration_sha256=skill.registration_sha256,
        package_tree_sha256=skill.package_tree_sha256,
        combined_sha256=skill.combined_sha256,
        logparse_receipt_sha256=logparse_receipt_sha256,
        status=diagnosis.status,
        confirmed_methods=diagnosis.confirmed_methods,
        evidence_count=len(diagnosis.evidence),
        checked_source_count=len(logs),
        skill_load=skill_load,
    )
    return VerifiedMethodDiagnosisV1(draft=diagnosis, audit=audit)


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
        _exact(value, _REVIEW_FIELDS, "method review")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("method review schema_version must equal integer 1")
        verdict = value["verdict"]
        if verdict not in _REVIEW_VERDICTS:
            raise ValueError("method review verdict is invalid")
        findings_raw = value["findings"]
        if not isinstance(findings_raw, list) or len(findings_raw) > 500:
            raise ValueError("method review findings must be a bounded array")
        findings: list[MethodReviewFindingV1] = []
        keys: set[tuple[str, tuple[str, ...]]] = set()
        for raw in findings_raw:
            if not isinstance(raw, dict):
                raise ValueError("method review finding must be an object")
            _exact(raw, _REVIEW_FINDING_FIELDS, "method review finding")
            method_id = _nonblank(raw["method_id"], label="review method_id", maximum_bytes=256)
            tokens = _string_array(raw["identity_tokens"], label="review identity_tokens", allow_empty=False)
            finding_verdict = raw["verdict"]
            if finding_verdict not in _REVIEW_VERDICTS:
                raise ValueError("method review finding verdict is invalid")
            reason = _nonblank(raw["reason"], label="review reason")
            key = (method_id, tuple(sorted(tokens)))
            if key in keys:
                raise ValueError("method review findings must be unique")
            keys.add(key)
            findings.append(MethodReviewFindingV1(method_id, tokens, finding_verdict, reason))
        return cls(
            verdict=verdict,
            findings=tuple(findings),
            limitations=_string_array(value["limitations"], label="review limitations"),
        )


def verify_method_review(
    diagnosis: VerifiedMethodDiagnosisV1,
    review: Mapping[str, Any] | MethodReviewV1,
) -> MethodReviewV1:
    """Bind independent Review findings to the exact grounded evidence set."""

    if not isinstance(diagnosis, VerifiedMethodDiagnosisV1):
        raise TypeError("diagnosis must be a verified method diagnosis")
    parsed = review if isinstance(review, MethodReviewV1) else MethodReviewV1.from_mapping(review)
    expected = {
        (item.method_id, tuple(sorted(item.identity_tokens)))
        for item in diagnosis.draft.evidence
    }
    actual = {
        (item.method_id, tuple(sorted(item.identity_tokens))) for item in parsed.findings
    }
    if actual != expected:
        raise ValueError("review findings must cover the exact grounded evidence identities")
    if parsed.verdict == "PASS" and any(item.verdict != "PASS" for item in parsed.findings):
        raise ValueError("PASS review requires every finding to PASS")
    if parsed.verdict == "REJECT" and not any(item.verdict == "REJECT" for item in parsed.findings):
        raise ValueError("REJECT review requires at least one rejected finding")
    if parsed.verdict == "NEED_MORE_EVIDENCE" and not any(
        item.verdict == "NEED_MORE_EVIDENCE" for item in parsed.findings
    ):
        raise ValueError("NEED_MORE_EVIDENCE review requires one matching finding")
    return parsed


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
