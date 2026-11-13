"""Bind a short model choice to this execution's recalled, frozen Skills."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from problem_locator.contracts import (
    Job, JobType, RouteDecision, RouteKind, bytes_sha256, canonical_json_bytes,
)
from problem_locator.integrations.agent_json import parse_agent_json_bytes

from .routing_keywords import parse_direct_match_keywords

if TYPE_CHECKING:
    from .route_recall import RouteCandidates


MIN_ROUTE_CONFIDENCE = 0.95
ROUTE_ADMISSION_POLICY = "shortlisted_route_v1"
_FALLBACK_REASONS = {
    "MODEL_NO_MATCH": "路由未选择专用 Skill，转入通用定位。",
    "OUTSIDE_CANDIDATES": "所选 Skill 不在本次候选中，转入通用定位。",
    "LOW_CONFIDENCE": "路由置信度低于 0.95，转入通用定位。",
    "NO_RECALL_MATCH": "未找到与本次问题相关的专用 Skill，转入通用定位。",
    "NO_ELIGIBLE_SKILL": "没有声明适用和排除条件的专用 Skill，转入通用定位。",
}


@dataclass(frozen=True, slots=True)
class RouteAdmissionResult:
    decision: RouteDecision
    audit: dict[str, Any]


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} 字段不符合路由审核合同。")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串。")
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


def read_route_catalog(job: Job, skill_index: str) -> list[dict[str, Any]]:
    """Read the complete server-owned catalog against its frozen Job refs."""
    if job.job_type is not JobType.ROUTE or job.context_snapshot is None:
        raise ValueError("路由需要 ROUTE Job 的冻结上下文。")
    index = parse_agent_json_bytes(skill_index.encode("utf-8")).value
    _object(index, {"schema_version", "skills"}, "Skill 索引")
    if type(index["schema_version"]) is not int or index["schema_version"] != 3:
        raise ValueError("Skill 索引版本必须为 3。")
    if not isinstance(index["skills"], list):
        raise ValueError("Skill 索引必须包含候选数组。")
    frozen_refs = {ref.id: ref.model_dump(mode="json") for ref in job.available_skill_refs}
    seen = set()
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
        fields = {"applicability", "exclusions"}
        if isinstance(routing, dict) and "direct_match_keywords" in routing:
            fields.add("direct_match_keywords")
        _object(routing, fields, "路由声明")
        parse_direct_match_keywords(routing.get("direct_match_keywords", []))
        applicability = _conditions(routing["applicability"])
        exclusions = _conditions(routing["exclusions"])
        if not applicability or len(set(applicability + exclusions)) != len(applicability + exclusions):
            raise ValueError("适用条件不能为空，且同一 Skill 的条件 ID 不得重复。")
    if seen != set(frozen_refs):
        raise ValueError("Skill 索引未完整覆盖 Job 的冻结候选。")
    return index["skills"]


def _routing_candidates(job: Job, skill_index: str) -> dict[str, dict[str, tuple[str, ...]]]:
    return {
        item["ref"]["id"]: {group: tuple(condition["id"] for condition in item["routing"][group])
                            for group in ("applicability", "exclusions")}
        for item in read_route_catalog(job, skill_index) if item["routing"] is not None
    }


def evaluate_route_admission(
    value: Any, job: Job, *, skill_index: str, candidates: RouteCandidates | None = None,
) -> RouteAdmissionResult:
    """Read the model's choice without requiring repeated condition assessments."""
    if candidates is None:
        from .route_recall import recall_route_candidates
        candidates = recall_route_candidates(job, skill_index=skill_index)
    value = value if isinstance(value, dict) else {}
    skill_id = value.get("skill_id")
    if not isinstance(skill_id, str):
        skill_id = None
    refs = {ref.id: ref for ref in job.available_skill_refs}
    reason = value.get("reason")
    reason = reason.strip()[:1024] if isinstance(reason, str) and reason.strip() else "按问题描述选择定位能力。"
    try:
        confidence = float(value.get("confidence", 0))
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    if isinstance(value.get("confidence"), bool) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        confidence = 0.0
    if not candidates.skill_ids:
        reason_code = "NO_RECALL_MATCH" if candidates.audit["ranked_candidates"] else "NO_ELIGIBLE_SKILL"
    elif skill_id not in refs:
        reason_code = "MODEL_NO_MATCH"
    elif skill_id not in candidates.skill_ids:
        reason_code = "OUTSIDE_CANDIDATES"
    elif confidence < MIN_ROUTE_CONFIDENCE:
        reason_code = "LOW_CONFIDENCE"
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
        "schema_version": 1, "policy": ROUTE_ADMISSION_POLICY,
        "job_id": job.job_id, "case_id": job.case_id,
        "base_state_revision": job.base_state_revision,
        "input_hashes": {
            "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
            "skill_index_sha256": bytes_sha256(skill_index.encode("utf-8")),
        },
        "model_skill_id": skill_id, "model_reason": reason,
        "effective_skill_id": skill_id if admitted else None,
        "reason_code": reason_code, "confidence": confidence,
        "retrieval": candidates.audit,
    }
    return RouteAdmissionResult(decision=decision, audit=audit)
