"""Server admission for one model-authored review of frozen routing conditions.

The model supplies semantic assessments. This module verifies their completeness
and quoted sources, then applies a conservative, deterministic admission policy.
It does not claim that literal quotation proves the model's semantic judgment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from problem_locator.contracts import (
    Job, JobType, RouteDecision, RouteKind, bytes_sha256, canonical_json_bytes,
)
from problem_locator.integrations.agent_json import parse_agent_json_bytes


MIN_ROUTE_CONFIDENCE = 0.95
_VERDICTS = frozenset({"SUPPORTED", "REFUTED", "UNKNOWN"})
_FALLBACK_REASONS = {
    "MODEL_NO_MATCH": "路由审核未选择专用 Skill，转入通用定位。",
    "NO_ROUTING_DECLARATION": "所选 Skill 尚未声明适用和排除条件，转入通用定位。",
    "LOW_CONFIDENCE": "路由置信度低于 0.95，转入通用定位。",
    "UNCERTAIN_CANDIDATE": "候选 Skill 的适用范围或排除条件仍不明确，转入通用定位。",
    "MULTIPLE_MATCHES": "多个专用 Skill 同时满足适用条件，转入通用定位。",
    "SELECTED_SKILL_NOT_MATCHED": "所选 Skill 未通过适用条件审核，转入通用定位。",
    "NO_ELIGIBLE_SKILLS": "没有声明适用和排除条件的专用 Skill，转入通用定位。",
}


@dataclass(frozen=True, slots=True)
class RouteAdmissionResult:
    decision: RouteDecision
    audit: dict[str, Any]


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} 字段不符合路由审核合同。")
    return value


def _text(value: Any, label: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串。")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} 超过长度限制。")
    return value


def _conditions(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("路由条件必须是数组。")
    names = []
    for item in value:
        _object(item, {"id", "description"}, "路由条件")
        names.append(_text(item["id"], "条件 ID"))
        _text(item["description"], "条件说明")
    if len(names) != len(set(names)):
        raise ValueError("路由条件 ID 不得重复。")
    return tuple(names)


def _routing_candidates(job: Job, skill_index: str) -> dict[str, dict[str, tuple[str, ...]]]:
    index = parse_agent_json_bytes(skill_index.encode("utf-8")).value
    _object(index, {"schema_version", "skills"}, "Skill 索引")
    if type(index["schema_version"]) is not int or index["schema_version"] != 3:
        raise ValueError("Skill 索引版本必须为 3。")
    if not isinstance(index["skills"], list):
        raise ValueError("Skill 索引必须包含候选数组。")
    frozen_refs = {ref.id: ref.model_dump(mode="json") for ref in job.available_skill_refs}
    seen = set()
    eligible = {}
    for item in index["skills"]:
        if not isinstance(item, dict) or "ref" not in item or "routing" not in item:
            raise ValueError("Skill 索引缺少冻结版本或路由声明。")
        ref = item["ref"]
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
            raise ValueError("Skill 索引中的版本引用无效。")
        skill_id = ref["id"]
        if skill_id in seen or frozen_refs.get(skill_id) != ref:
            raise ValueError("Skill 索引与 Job 冻结的候选版本不一致。")
        seen.add(skill_id)
        routing = item["routing"]
        if routing is None:
            continue
        _object(routing, {"applicability", "exclusions"}, "路由声明")
        applicability = _conditions(routing["applicability"])
        exclusions = _conditions(routing["exclusions"])
        if not applicability or len(set(applicability + exclusions)) != len(applicability + exclusions):
            raise ValueError("适用条件不能为空，且同一 Skill 的条件 ID 不得重复。")
        eligible[skill_id] = {"applicability": applicability, "exclusions": exclusions}
    if seen != set(frozen_refs):
        raise ValueError("Skill 索引未完整覆盖 Job 的冻结候选。")
    return eligible


def _quoted_sources(job: Job) -> dict[str, str]:
    if job.job_type is not JobType.ROUTE or job.context_snapshot is None:
        raise ValueError("路由审核需要 ROUTE Job 的冻结上下文。")
    snapshot = job.context_snapshot.model_dump(mode="json")
    sources = {}
    for field, value in snapshot["problem_spec"].items():
        if isinstance(value, str):
            sources[f"/problem_spec/{field}"] = value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    sources[f"/problem_spec/{field}/{index}"] = item
    for collection in ("user_facts", "confirmed_facts"):
        for index, item in enumerate(snapshot[collection]):
            if item["status"] == "ACTIVE":
                sources[f"/{collection}/{index}/statement"] = item["statement"]
    return sources


def _assess_conditions(
    value: Any, expected: tuple[str, ...], sources: dict[str, str], kind: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("条件审核必须是数组。")
    seen = set()
    checked = []
    for item in value:
        _object(item, {"condition_id", "verdict", "reason", "evidence"}, "条件审核")
        condition_id = _text(item["condition_id"], "条件 ID")
        if condition_id not in expected or condition_id in seen:
            raise ValueError("审核包含未知或重复的条件 ID。")
        seen.add(condition_id)
        verdict = item["verdict"]
        if not isinstance(verdict, str) or verdict not in _VERDICTS:
            raise ValueError("条件审核 verdict 无效。")
        _text(item["reason"], "审核理由", max_length=1024)
        if not isinstance(item["evidence"], list):
            raise ValueError("审核证据必须是数组。")
        evidence_valid = bool(item["evidence"])
        for evidence in item["evidence"]:
            _object(evidence, {"pointer", "quote"}, "审核证据")
            if not isinstance(evidence["pointer"], str) or not isinstance(evidence["quote"], str):
                raise ValueError("审核证据的 pointer 和 quote 必须是字符串。")
            source = sources.get(evidence["pointer"])
            evidence_valid = evidence_valid and bool(evidence["quote"].strip()) and (
                source is not None and evidence["quote"] in source
            )
        checked.append({
            "kind": kind, "condition_id": condition_id,
            "model_verdict": verdict,
            "effective_verdict": verdict if evidence_valid else "UNKNOWN",
            "evidence_valid": evidence_valid,
        })
    if seen != set(expected):
        raise ValueError("审核未完整覆盖所有声明的条件。")
    return checked


def evaluate_route_admission(value: Any, job: Job, *, skill_index: str) -> RouteAdmissionResult:
    """Validate the private model protocol and reduce it to the existing DTO."""
    _object(value, {"skill_id", "reason", "confidence", "assessments"}, "ROUTE 响应")
    skill_id = value["skill_id"]
    if skill_id is not None and not isinstance(skill_id, str):
        raise ValueError("skill_id 必须是字符串或 null。")
    refs = {ref.id: ref for ref in job.available_skill_refs}
    if skill_id is not None and skill_id not in refs:
        raise ValueError("所选 Skill 不在冻结目录中。")
    reason = _text(value["reason"], "路由理由", max_length=1024)
    confidence = value["confidence"]
    if (
        type(confidence) not in (int, float) or not 0 <= confidence <= 1
        or not math.isfinite(confidence)
    ):
        raise ValueError("confidence 必须是 0 到 1 之间的有限 JSON 数字。")
    eligible = _routing_candidates(job, skill_index)
    sources = _quoted_sources(job)
    if not isinstance(value["assessments"], list):
        raise ValueError("assessments 必须是数组。")
    candidate_results = []
    seen = set()
    for assessment in value["assessments"]:
        _object(assessment, {"skill_id", "applicability", "exclusions"}, "候选审核")
        candidate_id = _text(assessment["skill_id"], "审核 Skill ID")
        if candidate_id not in eligible or candidate_id in seen:
            raise ValueError("审核包含未知、未声明或重复的候选 Skill。")
        seen.add(candidate_id)
        checks = [
            check for kind in ("applicability", "exclusions")
            for check in _assess_conditions(
                assessment[kind], eligible[candidate_id][kind], sources, kind,
            )
        ]
        ruled_out = any(
            check["effective_verdict"] == ("REFUTED" if check["kind"] == "applicability" else "SUPPORTED")
            for check in checks
        )
        matched = all(
            check["effective_verdict"] == ("SUPPORTED" if check["kind"] == "applicability" else "REFUTED")
            for check in checks
        )
        candidate_results.append({
            "skill_id": candidate_id,
            "status": "ruled_out" if ruled_out else "match" if matched else "uncertain",
            "conditions": checks,
        })
    if seen != set(eligible):
        raise ValueError("审核未完整覆盖所有声明路由条件的候选 Skill。")
    matches = [item["skill_id"] for item in candidate_results if item["status"] == "match"]
    if skill_id is None:
        reason_code = "MODEL_NO_MATCH"
    elif skill_id not in eligible:
        reason_code = "NO_ROUTING_DECLARATION"
    elif confidence < MIN_ROUTE_CONFIDENCE:
        reason_code = "LOW_CONFIDENCE"
    elif any(item["status"] == "uncertain" for item in candidate_results):
        reason_code = "UNCERTAIN_CANDIDATE"
    elif len(matches) > 1:
        reason_code = "MULTIPLE_MATCHES"
    elif matches != [skill_id]:
        reason_code = "SELECTED_SKILL_NOT_MATCHED"
    else:
        reason_code = "ADMITTED"
    admitted = reason_code == "ADMITTED"
    decision = RouteDecision(
        kind=RouteKind.MATCHED if admitted else RouteKind.NO_CAPABILITY,
        skill_ref=refs[skill_id] if admitted else None,
        reason=reason if admitted else _FALLBACK_REASONS[reason_code],
        confidence=confidence,
    )
    audit = {
        "schema_version": 1, "policy": "strict_route_admission_v1",
        "job_id": job.job_id, "case_id": job.case_id,
        "base_state_revision": job.base_state_revision,
        "input_hashes": {
            "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
            "skill_index_sha256": bytes_sha256(skill_index.encode("utf-8")),
        },
        "model_skill_id": skill_id, "model_reason": reason,
        "effective_skill_id": skill_id if admitted else None,
        "reason_code": reason_code, "confidence": confidence,
        "assessments": value["assessments"], "candidate_results": candidate_results,
    }
    return RouteAdmissionResult(decision=decision, audit=audit)
