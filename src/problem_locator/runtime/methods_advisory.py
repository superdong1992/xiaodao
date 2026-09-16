"""Accept bounded Methods judgments without mechanical evidence rejection.

This module preserves input identity checks. Model quotations, identity tokens,
and marker matching are not proof; only the later server projection may attach
locations resolved from the actual input logs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, replace
from typing import Any, Mapping, Sequence

from problem_locator.contracts import canonical_json_sha256

from .methods_grounding import (
    FrozenTargetLogV1, GroundedEvidenceSourceV1, MethodDiagnosisDraftV1,
    MethodEvidenceV1, MethodGroundingAuditV1, MethodReviewFindingV1, MethodReviewV1, SkillLoadReceiptV1,
    VerifiedMethodDiagnosisV1, _DRAFT_FIELDS, _EVIDENCE_FIELDS, _SOURCE_FIELDS,
    _REVIEW_FIELDS, _REVIEW_FINDING_FIELDS, _REVIEW_VERDICTS, _exact, _nonblank, _string_array,
)
from .methods_skill import ResolvedSpecializedSkillV1


ADVISORY_LIMITATION = (
    "本报告保留模型判断，未逐项复核引用文字、行号、身份标记和方法标记是否支持该判断；"
    "输入日志的归属和文件完整性仍由服务端检查。"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _parse(
    value: Mapping[str, Any], *, effective: bool = False,
) -> tuple[MethodDiagnosisDraftV1, list[dict[str, Any]], list[dict[str, int]]]:
    if not isinstance(value, dict):
        raise ValueError("method diagnosis draft must be an object")
    _exact(value, _DRAFT_FIELDS, "method diagnosis draft")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("method diagnosis draft schema_version must equal integer 1")
    status = value["status"]
    if not isinstance(status, str) or status not in {"CONFIRMED", "PARTIAL", "INSUFFICIENT"}:
        raise ValueError("method diagnosis status is invalid")
    # Effective method lists include recognizable finding IDs. They may be
    # larger than the original summary lists, but remain bounded by the 500
    # findings plus the two original 200-entry method lists.
    method_limit = 1000 if effective else 200
    confirmed = _string_array(value["confirmed_methods"], label="confirmed_methods", maximum=method_limit)
    candidates = _string_array(value["candidate_methods"], label="candidate_methods", maximum=method_limit)
    if set(confirmed) & set(candidates):
        raise ValueError("confirmed_methods and candidate_methods must be disjoint")
    limitations = _string_array(value["limitations"], label="limitations", maximum=512 if effective else 200)
    safety_notes = _string_array(value["safety_notes"], label="safety_notes")
    raw_items = value["evidence"]
    if not isinstance(raw_items, list) or len(raw_items) > 500:
        raise ValueError("evidence must be a bounded array")
    if status == "INSUFFICIENT" and (confirmed or raw_items):
        raise ValueError("INSUFFICIENT forbids confirmed methods and evidence")
    if status == "CONFIRMED" and not confirmed:
        raise ValueError("CONFIRMED requires recognizable method evidence")

    notes: list[dict[str, Any]] = []
    merged: list[dict[str, int]] = []
    evidence: list[MethodEvidenceV1] = []
    seen: dict[MethodEvidenceV1, int] = {}
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict) or set(raw) - _EVIDENCE_FIELDS:
            notes.append({"index": index, "reason_code": "FINDING_STRUCTURE_INVALID"})
            continue
        try:
            method_id = _nonblank(raw.get("method_id"), label="method_id", maximum_bytes=256)
            summary = _nonblank(raw.get("summary"), label="summary")
        except (TypeError, ValueError):
            notes.append({"index": index, "reason_code": "FINDING_STRUCTURE_INVALID"})
            continue
        try:
            tokens = _string_array(raw.get("identity_tokens", []), label="identity_tokens", maximum=100)
        except (TypeError, ValueError):
            tokens = ()
            notes.append({"index": index, "reason_code": "IDENTITY_METADATA_OMITTED"})
        sources: list[GroundedEvidenceSourceV1] = []
        raw_sources = raw.get("sources", [])
        if not isinstance(raw_sources, list) or len(raw_sources) > 100:
            raw_sources = []
            notes.append({"index": index, "reason_code": "SOURCE_METADATA_OMITTED"})
        for source_index, raw_source in enumerate(raw_sources):
            try:
                if not isinstance(raw_source, dict) or set(raw_source) - _SOURCE_FIELDS:
                    raise ValueError("source metadata is invalid")
                source_id = _nonblank(raw_source.get("source_id"), label="source_id", maximum_bytes=128)
                line_number = raw_source.get("line_number")
                if type(line_number) is not int or line_number < 1:
                    raise ValueError("source line_number is invalid")
                # These strings are retained for audit only. Neither is used
                # to construct a report excerpt or an archive path.
                marker = raw_source.get("marker", "")
                line = raw_source.get("line", "")
                if not isinstance(marker, str) or len(marker.encode("utf-8")) > 1024:
                    marker = ""
                if not isinstance(line, str) or len(line.encode("utf-8")) > 65_536:
                    line = ""
                source = GroundedEvidenceSourceV1(source_id, line_number, marker, line)
                if source not in sources:
                    sources.append(source)
            except (TypeError, ValueError):
                notes.append({"index": index, "source_index": source_index,
                              "reason_code": "SOURCE_METADATA_OMITTED"})
        item = MethodEvidenceV1(method_id, summary, tuple(sorted(tokens)), tuple(sources))
        if item in seen:
            merged.append({"index": index, "retained_index": seen[item]})
            continue
        seen[item] = index
        evidence.append(item)
    if raw_items and not evidence:
        raise ValueError("diagnosis has no structurally recognizable evidence")
    return (MethodDiagnosisDraftV1(status, confirmed, candidates, tuple(evidence),
                                   limitations, safety_notes), notes, merged)


def parse_advisory_diagnosis(value: Mapping[str, Any]) -> MethodDiagnosisDraftV1:
    """Read the frozen effective draft without reinstating strict evidence gates."""
    return _parse(value, effective=True)[0]


def parse_advisory_review(value: Mapping[str, Any]) -> MethodReviewV1:
    """Keep the whole-result verdict without requiring mechanical identity tags."""
    if not isinstance(value, dict):
        raise ValueError("method review must be an object")
    _exact(value, _REVIEW_FIELDS, "method review")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("method review schema_version must equal integer 1")
    verdict = value["verdict"]
    if not isinstance(verdict, str) or verdict not in _REVIEW_VERDICTS:
        raise ValueError("method review verdict is invalid")
    raw_findings = value["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > 500:
        raise ValueError("method review findings must be a bounded array")
    findings: list[MethodReviewFindingV1] = []
    for raw in raw_findings:
        if not isinstance(raw, dict) or set(raw) - _REVIEW_FINDING_FIELDS:
            raise ValueError("method review finding fields are invalid")
        method = _nonblank(raw.get("method_id"), label="review method_id", maximum_bytes=256)
        tokens = _string_array(raw.get("identity_tokens", []), label="review identity_tokens")
        finding_verdict = raw.get("verdict")
        if not isinstance(finding_verdict, str) or finding_verdict not in _REVIEW_VERDICTS:
            raise ValueError("method review finding verdict is invalid")
        reason = _nonblank(raw.get("reason"), label="review reason")
        finding = MethodReviewFindingV1(method, tuple(sorted(tokens)), finding_verdict, reason)
        if finding not in findings:
            findings.append(finding)
    limitations = list(_string_array(value["limitations"], label="review limitations"))
    limitations.append("Reviewer 对整份模型结果给出审核判断，未重新执行引用、身份和方法标记的一致性复核。")
    return MethodReviewV1(verdict, tuple(findings), tuple(dict.fromkeys(limitations)))


def accept_method_diagnosis_advisory(
    *, skill: ResolvedSpecializedSkillV1, draft: Mapping[str, Any],
    target_logs: Sequence[FrozenTargetLogV1], logparse_receipt_sha256: str,
    skill_load: SkillLoadReceiptV1, missing_targets: Sequence[str] = (),
) -> VerifiedMethodDiagnosisV1:
    """Keep recognizable judgments while retaining frozen-input identity checks."""
    if not isinstance(skill, ResolvedSpecializedSkillV1):
        raise TypeError("skill must be a resolved specialized Skill")
    if not isinstance(logparse_receipt_sha256, str) or _SHA256.fullmatch(logparse_receipt_sha256) is None:
        raise ValueError("logparse receipt digest is invalid")
    logs = tuple(target_logs)
    if not logs or any(not isinstance(item, FrozenTargetLogV1) for item in logs):
        raise ValueError("advisory diagnosis requires frozen target logs")
    source_ids = tuple(item.source_id for item in logs)
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("target log source ids must be unique")
    if (not isinstance(skill_load, SkillLoadReceiptV1)
            or skill_load.package_tree_sha256 != skill.package_tree_sha256
            or skill_load.scanned_source_ids != source_ids):
        raise ValueError("Methods input identities differ from the frozen scan receipt")
    # No second marker scan: resource ownership/hash checks already bind the
    # exact bytes used by the initial context builder.
    parsed, notes, merged = _parse(draft)
    methods = skill.methods.method_by_id
    limitations = list(dict.fromkeys([*parsed.limitations, ADVISORY_LIMITATION]))
    retained: list[MethodEvidenceV1] = []
    unknown_method = False
    for index, item in enumerate(parsed.evidence):
        if item.method_id not in methods:
            unknown_method = True
            notes.append({"index": index, "reason_code": "UNKNOWN_METHOD_AS_MODEL_JUDGMENT"})
        retained.append(item)
    if unknown_method:
        limitations.append("部分模型方法标识不在当前 Skill 中；对应描述仅作为模型判断保留，不表示已匹配已注册的方法。")
    confirmed = tuple(dict.fromkeys(item.method_id for item in retained))
    candidates = tuple(dict.fromkeys([
        *(method for method in parsed.candidate_methods if method not in confirmed),
        *(method for method in parsed.confirmed_methods if method not in confirmed),
    ]))
    if any(method not in methods for method in candidates):
        limitations.append("部分方法名称未在当前 Skill 中找到，报告将其保留为待核对线索。")
    if notes or any(not item.sources or not item.identity_tokens for item in retained):
        limitations.append("部分发现缺少可用的引用或身份信息；保留模型判断，引用位置仅在实际输入日志中可定位时展示。")
    if merged:
        limitations.append("模型返回的完全重复发现已合并。")
    identities: dict[tuple[str, tuple[str, ...]], int] = {}
    for item in retained:
        identity = (item.method_id, item.identity_tokens)
        identities[identity] = identities.get(identity, 0) + 1
    if any(count > 1 for count in identities.values()):
        limitations.append("同一身份标记出现了不同的模型判断，报告均予保留，尚未核对这些判断是否冲突。")
    if missing_targets:
        limitations.append("部分目标日志未取得：" + "、".join(missing_targets) + "。")
    effective = replace(parsed, status="PARTIAL" if retained else "INSUFFICIENT",
        confirmed_methods=confirmed, candidate_methods=candidates,
        evidence=tuple(retained), limitations=tuple(dict.fromkeys(limitations)))
    audit = MethodGroundingAuditV1(schema_version=1,
        registration_id=skill.registration_id, registration_sha256=skill.registration_sha256,
        package_tree_sha256=skill.package_tree_sha256, combined_sha256=skill.combined_sha256,
        logparse_receipt_sha256=logparse_receipt_sha256, status=effective.status,
        confirmed_methods=effective.confirmed_methods, evidence_count=len(effective.evidence),
        checked_source_count=0, skill_load=skill_load, validation_mode="advisory")
    receipt = {"schema_version": 1, "validation_mode": "advisory",
        "source_draft_sha256": canonical_json_sha256(draft),
        "effective_draft_sha256": canonical_json_sha256({"schema_version": 1, **asdict(effective)}),
        "retained_count": len(retained), "notes": notes, "merged": merged,
        "missing_targets": list(missing_targets)}
    return VerifiedMethodDiagnosisV1(effective, audit, advisory=receipt)


__all__ = ["ADVISORY_LIMITATION", "accept_method_diagnosis_advisory",
           "parse_advisory_diagnosis", "parse_advisory_review"]
