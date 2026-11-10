"""A conservative literal shortcut before the existing semantic ROUTE review.

The lexical vetoes below only avoid obvious misleading contexts. They do not
prove the meaning of arbitrary natural-language descriptions.
"""
from __future__ import annotations

import re
from typing import Any

from problem_locator.contracts import (
    Job, JobType, RouteDecision, RouteKind, bytes_sha256, canonical_json_bytes,
)
from problem_locator.integrations.agent_json import parse_agent_json_bytes

from .route_admission import RouteAdmissionResult, _routing_candidates
from .routing_keywords import parse_direct_match_keywords


DIRECT_KEYWORD_ROUTE_POLICY = "direct_keyword_route_v1"
_ASCII_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_UNCLEAR_CHINESE = (
    "不是", "并非", "没有", "不存在", "未出现", "未发生", "未见", "未报", "未触发",
    "未再出现", "未再次出现", "未曾出现", "不曾出现", "不再出现", "未再发生",
    "未再次发生", "不曾发生", "不再发生", "未观察到", "未检测到", "未发现", "未复现",
    "无此", "否认", "排除", "不涉及", "不包含", "不要", "疑似", "怀疑", "可能",
    "是否", "不确定", "历史", "之前", "曾经", "上次", "过去", "示例", "例如",
    "比如", "样例", "举例", "例子", "举个例", "模板", "引用", "对比", "相比", "比较", "而非", "而不是",
    "假设", "假如", "如果",
)
_UNCLEAR_ENGLISH = re.compile(
    r"(?<![A-Za-z0-9_])(?:not|no|never|without|suspect(?:ed)?|possible|possibly|maybe|might|"
    r"previous(?:ly)?|historical|history|example|sample|quoted?|compar(?:e|ed|ison)|versus|vs|if|hypothetical|"
    r"cannot|(?:isn|aren|wasn|weren|don|doesn|didn|hasn|haven|hadn|couldn|wouldn|shouldn|mustn|needn|"
    r"mightn|can|won|shan|ain|oughtn)['’]t)"
    r"(?![A-Za-z0-9_])", re.IGNORECASE,
)
_QUOTED_TEXT = re.compile(r'''"[^"\n]*"|'[^'\n]*'|“[^”\n]*”|‘[^’\n]*’|`[^`\n]*`''')


def _literal_matches(text: str, keywords: tuple[str, ...]) -> list[dict[str, Any]]:
    matches = []
    for keyword in keywords:
        offset = 0
        while (start := text.find(keyword, offset)) >= 0:
            end = start + len(keyword)
            if not (
                keyword[0] in _ASCII_WORD and start > 0 and text[start - 1] in _ASCII_WORD
                or keyword[-1] in _ASCII_WORD and end < len(text) and text[end] in _ASCII_WORD
            ):
                matches.append({"keyword": keyword, "start": start, "end": end})
                break  # One receipt per declared keyword keeps the audit bounded.
            offset = start + 1
    return sorted(matches, key=lambda item: (item["start"], item["end"], item["keyword"]))


def _has_unclear_context(text: str, matches: list[dict[str, Any]]) -> bool:
    if any(word in text for word in _UNCLEAR_CHINESE) or _UNCLEAR_ENGLISH.search(text):
        return True
    if "```" in text or "~~~" in text or re.search(r"(?:^|\n)\s*>", text):
        return True
    quotes = list(_QUOTED_TEXT.finditer(text))
    for match in matches:
        keyword = match["keyword"]
        end_boundary = r"(?![A-Za-z0-9_])" if keyword[-1] in _ASCII_WORD else ""
        if re.search(r"[无不未非]\s*" + re.escape(keyword) + end_boundary, text):
            return True
        if any(_literal_matches(quote.group(), (keyword,)) for quote in quotes):
            return True
    return False


def try_direct_keyword_route(
    job: Job, *, skill_index: str, raw_problem_text: str,
) -> RouteAdmissionResult | None:
    """Return a rule-admitted frozen Skill, or defer to semantic routing."""
    if job.job_type is not JobType.ROUTE or job.context_snapshot is None:
        raise ValueError("直接关键词路由需要 ROUTE Job 的冻结上下文。")
    if not isinstance(raw_problem_text, str) or not raw_problem_text.strip():
        raise ValueError("直接关键词路由需要 Case 的原始问题描述。")
    # This verifies every ref and condition, including candidates without a hit.
    eligible = _routing_candidates(job, skill_index)
    index = parse_agent_json_bytes(skill_index.encode("utf-8")).value
    matched = []
    for item in index["skills"]:
        routing = item["routing"]
        if routing is None:
            continue
        keywords = parse_direct_match_keywords(routing.get("direct_match_keywords", []))
        matches = _literal_matches(raw_problem_text, keywords)
        if matches:
            matched.append((item["ref"]["id"], matches))
    if job.previous_outcome_refs or len(matched) != 1:
        return None
    skill_id, matches = matched[0]
    if eligible[skill_id]["exclusions"] or _has_unclear_context(raw_problem_text, matches):
        return None
    ref = next(ref for ref in job.available_skill_refs if ref.id == skill_id)
    reason = "问题原文唯一命中专用 Skill 声明的直接路由关键词。"
    decision = RouteDecision(kind=RouteKind.MATCHED, skill_ref=ref, reason=reason, confidence=1.0)
    return RouteAdmissionResult(decision=decision, audit={
        "schema_version": 1, "policy": DIRECT_KEYWORD_ROUTE_POLICY,
        "job_id": job.job_id, "case_id": job.case_id,
        "base_state_revision": job.base_state_revision,
        "input_hashes": {
            "context_snapshot_sha256": bytes_sha256(canonical_json_bytes(job.context_snapshot)),
            "skill_index_sha256": bytes_sha256(skill_index.encode("utf-8")),
            "raw_problem_text_sha256": bytes_sha256(raw_problem_text.encode("utf-8")),
        },
        "model_called": False, "model_skill_id": None, "effective_skill_id": skill_id,
        "reason_code": "DIRECT_KEYWORD_MATCH", "reason": reason, "confidence": 1.0,
        "confidence_basis": "deterministic_literal_match",
        "matches": matches, "match_offset_unit": "unicode_codepoint",
    })
