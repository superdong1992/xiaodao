"""Deterministic lexical recall for a bounded semantic routing review."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from problem_locator.contracts import Job, JobType, bytes_sha256, canonical_json_bytes

from .route_admission import read_route_catalog


ROUTE_RECALL_POLICY = "lexical_top5_v1"
MAX_ROUTE_CANDIDATES = 5
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:[_.:/-][a-z0-9]+)*")
_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_STOP_WORDS = frozenset("""a an and are as at be been being by can check could did do does
error errors failed failing failure failures fault faults for from has have how if in into is
it its log logs may message no not of on or please problem problems report request result
service should system that the their then there these this those to use user was were what
when where which will with would you your timeout timeouts timed out diagnosis diagnose
diagnostic diagnostics locate locating slow slowness issue issues
问题 错误 异常 失败 超时 故障 定位 诊断 分析 检查 报告 用户 情况 出现 发生 系统 服务 请求
操作 相关 结果 原因 重试 配置 如何 怎么 是否 可以 需要 一个 这个 那个 不能 无法 导致 以及
还有 没有 当前 确认 可能 已经 提供 使用 应该 处理 建议
""".split())
_CHINESE_STOP_WORDS = re.compile("|".join(
    re.escape(word) for word in sorted(_STOP_WORDS, key=lambda word: (-len(word), word))
    if _CHINESE.fullmatch(word)
))


@dataclass(frozen=True, slots=True)
class RouteCandidates:
    skill_ids: tuple[str, ...]
    model_index: str
    audit: dict[str, Any]


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _tokens(text: str) -> frozenset[str]:
    normalized = _normalize(text)
    tokens: set[str] = set()
    for identifier in _IDENTIFIER.findall(normalized):
        tokens.add(identifier)
        tokens.update(re.split(r"[_.:/-]", identifier))
    # Keep both the complete identifier and its camel-case parts.
    camel_parts = _CAMEL_BOUNDARY.sub(" ", unicodedata.normalize("NFKC", text)).casefold()
    tokens.update(_IDENTIFIER.findall(camel_parts))
    for word in _CHINESE.findall(_CHINESE_STOP_WORDS.sub(" ", normalized)):
        tokens.update(word[index:index + 2] for index in range(len(word) - 1))
    return frozenset(token for token in tokens if len(token) >= 2 and token not in _STOP_WORDS)


def _query_texts(job: Job) -> tuple[str, ...]:
    if job.job_type is not JobType.ROUTE or job.context_snapshot is None:
        raise ValueError("候选召回需要 ROUTE Job 的固定上下文。")
    snapshot = job.context_snapshot
    texts = []
    for value in snapshot.problem_spec.model_dump(mode="json").values():
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(value)
    texts.extend(item.statement for collection in (snapshot.user_facts, snapshot.confirmed_facts)
                 for item in collection if item.status.value == "ACTIVE")
    return tuple(texts)


def _name(skill_id: str) -> str:
    return skill_id.rsplit("/", 1)[-1]


def _weighted_tokens(entry: dict[str, Any]) -> dict[str, int]:
    fields = [(_name(entry["ref"]["id"]), 3), (entry.get("capability", ""), 3),
              (entry.get("summary", ""), 1)]
    fields.extend((condition["description"], 2) for condition in entry["routing"]["applicability"])
    weights: dict[str, int] = {}
    for text, weight in fields:
        for token in _tokens(text):
            weights[token] = max(weights.get(token, 0), weight)
    return weights


def _keyword_matches(keywords: list[str], texts: tuple[str, ...]) -> list[str]:
    # Normalized duplicates and repeated mentions count as one declared keyword.
    declared: dict[str, str] = {}
    for keyword in sorted(keywords):
        declared.setdefault(_normalize(keyword), keyword)
    matches = []
    for normalized, original in sorted(declared.items()):
        pattern = re.escape(normalized)
        if normalized[0].isascii() and (normalized[0].isalnum() or normalized[0] == "_"):
            pattern = r"(?<![a-z0-9_])" + pattern
        if normalized[-1].isascii() and (normalized[-1].isalnum() or normalized[-1] == "_"):
            pattern += r"(?![a-z0-9_])"
        if any(re.search(pattern, text) for text in texts):
            matches.append(original)
    return matches


def recall_route_candidates(job: Job, *, skill_index: str) -> RouteCandidates:
    """Select at most five positive matches from the complete pinned catalog."""
    entries = [entry for entry in read_route_catalog(job, skill_index) if entry["routing"] is not None]
    query_texts = _query_texts(job)
    normalized_texts = tuple(_normalize(text) for text in query_texts)
    query_tokens = frozenset(token for text in query_texts for token in _tokens(text))
    documents = {entry["ref"]["id"]: _weighted_tokens(entry) for entry in entries}
    frequencies = Counter(token for document in documents.values() for token in document)
    ranked = []
    for entry in entries:
        skill_id = entry["ref"]["id"]
        weights = documents[skill_id]
        score = sum(weights[token] * (1 + math.log((len(entries) + 1) / (frequencies[token] + 1)))
                    for token in sorted(query_tokens & weights.keys()))
        ranked.append({
            "skill_id": skill_id, "score": score,
            "keyword_matches": _keyword_matches(entry["routing"].get("direct_match_keywords", []), normalized_texts),
            "selected": False,
        })
    ranked.sort(key=lambda item: (-len(item["keyword_matches"]), -item["score"], item["skill_id"]))
    selected = [item for item in ranked if item["keyword_matches"] or item["score"] > 0][:MAX_ROUTE_CANDIDATES]
    for item in selected:
        item["selected"] = True
    skill_ids = tuple(item["skill_id"] for item in selected)
    by_id = {entry["ref"]["id"]: entry for entry in entries}
    model_index = canonical_json_bytes({"schema_version": 1, "skills": [{
        "skill_id": skill_id, "name": _name(skill_id),
        "capability": by_id[skill_id].get("capability", ""),
        "summary": by_id[skill_id].get("summary", ""),
        "routing": {kind: by_id[skill_id]["routing"][kind] for kind in ("applicability", "exclusions")},
    } for skill_id in skill_ids]}).decode("utf-8")
    return RouteCandidates(skill_ids=skill_ids, model_index=model_index, audit={
        "policy": ROUTE_RECALL_POLICY,
        "candidate_ids": list(skill_ids),
        "ranked_candidates": ranked,
        "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
        "skill_index_sha256": bytes_sha256(skill_index.encode("utf-8")),
        "model_index_sha256": bytes_sha256(model_index.encode("utf-8")),
    })
