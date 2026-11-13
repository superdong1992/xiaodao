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
    FrozenTargetLogV1, MethodDiagnosisDraftV1,
    MethodEvidenceV1, MethodGroundingAuditV1, MethodReviewV1, SkillLoadReceiptV1,
    VerifiedMethodDiagnosisV1,
)
from .methods_skill import ResolvedSpecializedSkillV1


ADVISORY_LIMITATION = (
    "本报告保留模型判断，未逐项复核引用文字、行号、身份标记和方法标记是否支持该判断；"
    "输入日志的归属和文件完整性仍由服务端检查。"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _parse(
    value: Mapping[str, Any],
) -> tuple[MethodDiagnosisDraftV1, list[dict[str, Any]], list[dict[str, int]]]:
    parsed = MethodDiagnosisDraftV1.from_mapping(value)
    evidence: list[MethodEvidenceV1] = []
    merged: list[dict[str, int]] = []
    seen: dict[MethodEvidenceV1, int] = {}
    for index, finding in enumerate(parsed.evidence):
        item = replace(finding, identity_tokens=tuple(sorted(finding.identity_tokens)))
        if item in seen:
            merged.append({"index": index, "retained_index": seen[item]})
            continue
        seen[item] = index
        evidence.append(item)
    return replace(parsed, evidence=tuple(evidence)), [], merged


def parse_advisory_diagnosis(value: Mapping[str, Any]) -> MethodDiagnosisDraftV1:
    """Read the frozen effective draft without reinstating strict evidence gates."""
    return _parse(value)[0]


def parse_advisory_review(value: Mapping[str, Any]) -> MethodReviewV1:
    """Keep the whole-result verdict without requiring mechanical identity tags."""
    parsed = MethodReviewV1.from_mapping(value)
    limitation = "Reviewer 对整份模型结果给出审核判断，未重新执行引用、身份和方法标记的一致性复核。"
    return replace(parsed, limitations=tuple(dict.fromkeys((*parsed.limitations, limitation))))


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
