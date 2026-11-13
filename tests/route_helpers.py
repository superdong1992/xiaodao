"""Explicit model stubs for routing tests, never a production semantic matcher."""
from __future__ import annotations

import json
import re


def context_section(prompt: str, name: str):
    match = re.search(rf"<<<SECTION \d+ {name}>>>\n(.*?)\n<<<END SECTION>>>", prompt, re.S)
    assert match is not None, name
    return json.loads(match.group(1))


def route_response(index, snapshot, skill_id, *, confidence=0.99, reason="测试模型明确判断适用范围。"):
    """Return only the model-owned choice; versions remain server-owned."""
    return {"skill_id": skill_id, "reason": reason, "confidence": confidence}


def route_response_for_prompt(prompt, skill_id, **kwargs):
    return route_response(context_section(prompt, "SKILL_INDEX"),
                          context_section(prompt, "CONTEXT_SNAPSHOT"), skill_id, **kwargs)
