"""Explicit model stubs for routing tests, never a production semantic matcher."""
from __future__ import annotations

import json
import re


def context_section(prompt: str, name: str):
    match = re.search(rf"<<<SECTION \d+ {name}>>>\n(.*?)\n<<<END SECTION>>>", prompt, re.S)
    assert match is not None, name
    return json.loads(match.group(1))


def route_response(index, snapshot, skill_id, *, confidence=0.99, reason="测试模型明确判断适用范围。"):
    """Declare a chosen semantic answer with real frozen quote bytes."""
    quote = snapshot["problem_spec"]["statement"]
    assessments = []
    for entry in index["skills"]:
        if entry["routing"] is None:
            continue
        assessment = {"skill_id": entry["ref"]["id"]}
        for group in ("applicability", "exclusions"):
            verdict = "SUPPORTED" if group == "applicability" and entry["ref"]["id"] == skill_id else "REFUTED"
            assessment[group] = [{
                "condition_id": condition["id"], "verdict": verdict,
                "reason": "固定测试场景的语义判断。",
                "evidence": [{"pointer": "/problem_spec/statement", "quote": quote}],
            } for condition in entry["routing"][group]]
        assessments.append(assessment)
    return {"skill_id": skill_id, "reason": reason, "confidence": confidence,
            "assessments": assessments}


def route_response_for_prompt(prompt, skill_id, **kwargs):
    return route_response(context_section(prompt, "SKILL_INDEX"),
                          context_section(prompt, "CONTEXT_SNAPSHOT"), skill_id, **kwargs)
