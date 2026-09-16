"""Presentation compatibility only for model-authored JSON responses."""

from __future__ import annotations

import re

from problem_locator.integrations.agent_json import AgentJsonDocument, parse_agent_json_bytes


_BOM = b"\xef\xbb\xbf"
_WHOLE_JSON_FENCE = re.compile(
    rb"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z"
)


def normalize_model_json_bytes(data: bytes) -> tuple[bytes, int]:
    """Return the model JSON body and its byte offset in the original input."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    start = len(_BOM) if data.startswith(_BOM) else 0
    end = len(data)
    while start < end and data[start] in b" \t\r\n":
        start += 1
    while end > start and data[end - 1] in b" \t\r\n":
        end -= 1
    normalized = data[start:end]
    fence = _WHOLE_JSON_FENCE.fullmatch(normalized)
    if fence is not None:
        normalized = fence.group("body")
        start += fence.start("body")
    return normalized, start


def parse_model_json_bytes(data: bytes) -> AgentJsonDocument:
    """Remove one leading BOM and one complete JSON fence, then parse strictly.

    This is deliberately separate from public MCP and Agent tool inputs. Never
    search prose for a JSON fragment, recursively unwrap, or repair JSON syntax.
    """
    normalized, _offset = normalize_model_json_bytes(data)
    return parse_agent_json_bytes(normalized)


__all__ = ["normalize_model_json_bytes", "parse_model_json_bytes"]
