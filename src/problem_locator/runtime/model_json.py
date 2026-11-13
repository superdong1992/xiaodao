"""Presentation compatibility only for model-authored JSON responses."""

from __future__ import annotations

import re
from dataclasses import dataclass

from problem_locator.contracts import InvalidJsonBytesError, bytes_sha256
from problem_locator.integrations.agent_json import AgentJsonDocument, parse_agent_json_bytes


_BOM = b"\xef\xbb\xbf"
MAX_MODEL_JSON_EXTRACTION_BYTES = 1024 * 1024
_WHITESPACE = b" \t\r\n"
_WHOLE_JSON_FENCE = re.compile(
    rb"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ModelJsonExtraction:
    """The adopted byte range; receipts deliberately contain no model text."""

    raw_bytes: bytes
    effective_bytes: bytes
    start: int
    end: int
    rule: str

    def to_receipt(self) -> dict[str, str | int]:
        return {
            "rule": self.rule,
            "raw_sha256": bytes_sha256(self.raw_bytes),
            "raw_size_bytes": len(self.raw_bytes),
            "effective_sha256": bytes_sha256(self.effective_bytes),
            "effective_size_bytes": len(self.effective_bytes),
            "start_byte": self.start,
            "end_byte": self.end,
        }


@dataclass(frozen=True, slots=True)
class ModelJsonParse:
    document: AgentJsonDocument
    extraction: ModelJsonExtraction | None = None


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


def _object_end(data: bytes, start: int, end: int) -> int | None:
    """Scan a single object once, respecting nested containers and strings.

    None means its lexical boundary is damaged, not that a nested object should
    be tried. Grammar, duplicate keys and non-finite numbers remain parser work.
    """
    stack: list[int] = []
    quoted = escaped = False
    for index in range(start, end):
        char = data[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if char == 34:
            quoted = True
        elif char in (123, 91):
            stack.append(125 if char == 123 else 93)
        elif char in (125, 93):
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index + 1
    return None


def _trim_range(data: bytes, start: int, end: int) -> tuple[int, int]:
    while start < end and data[start] in _WHITESPACE:
        start += 1
    while end > start and data[end - 1] in _WHITESPACE:
        end -= 1
    return start, end


def _scan_mixed_model_json(data: bytes, start: int, end: int) -> tuple[int, int, str]:
    """Read one complete object; prose and Markdown are presentation only."""
    cursor = start
    candidate = None
    after_fence = False
    while cursor < end:
        newline = data.find(b"\n", cursor, end)
        next_line = end if newline < 0 else newline + 1
        line_end = end if newline < 0 else newline
        line_start, line_end = _trim_range(data, cursor, line_end)
        line = data[line_start:line_end]
        if not line:
            cursor = next_line
            continue
        if line.startswith((b"[", b'"', _BOM)):
            # Do not mistake an object nested inside another JSON value for
            # the response itself.
            raise InvalidJsonBytesError("model JSON extraction found a non-object root")
        opening = line.find(b"{")
        # An inline object must stand on its own, not inside a preceding
        # array or quoted JSON value. Balanced prose punctuation is harmless.
        quoted = escaped = False
        array_depth = 0
        for char in line[:opening] if opening >= 0 else line:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char == 91:
                array_depth += 1
            elif char == 93:
                array_depth -= 1
        if quoted or array_depth > 0:
            raise InvalidJsonBytesError("model JSON extraction found a non-object root")
        if opening >= 0:
            if candidate is not None:
                raise InvalidJsonBytesError("model JSON extraction contains multiple objects")
            body_start = line_start + opening
            body_end = _object_end(data, body_start, end)
            if body_end is None:
                raise InvalidJsonBytesError("model JSON extraction requires one complete object")
            rule = "final_json_fence_v1" if after_fence else "final_json_object_v1"
            candidate = body_start, body_end, rule
            cursor = body_end
            continue
        after_fence = line.startswith(b"```")
        cursor = next_line
    if candidate is None:
        raise InvalidJsonBytesError("model JSON extraction found no object")
    return candidate


def extract_model_json_bytes(data: bytes) -> ModelJsonExtraction:
    """Extract one explicitly bounded final object, without repairing syntax.

    Only model responses use this boundary. Public Agent and MCP inputs stay
    strict. The bounded fallback uses a fixed number of linear passes, and
    never reparses every opening brace or retries nested candidates.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_MODEL_JSON_EXTRACTION_BYTES:
        raise InvalidJsonBytesError("model JSON extraction exceeds its 1048576-byte fallback limit")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidJsonBytesError("model JSON extraction requires valid UTF-8 throughout") from exc
    start = len(_BOM) if data.startswith(_BOM) else 0
    start, end = _trim_range(data, start, len(data))
    body_start, body_end, rule = _scan_mixed_model_json(data, start, end)
    return ModelJsonExtraction(data, data[body_start:body_end], body_start, body_end, rule)


def _parse_model_document(data: bytes) -> AgentJsonDocument:
    try:
        return parse_agent_json_bytes(data)
    except RecursionError as exc:
        raise InvalidJsonBytesError("model JSON nesting exceeds the parser recursion limit") from exc


def parse_model_json_response(data: bytes) -> ModelJsonParse:
    """Strict parsing first; only failed mixed presentations use extraction."""
    normalized, _offset = normalize_model_json_bytes(data)
    try:
        return ModelJsonParse(_parse_model_document(normalized))
    except InvalidJsonBytesError as exc:
        # Never mine nested objects from a different JSON root type. Complete
        # objects may still be followed by ordinary explanatory text.
        if isinstance(exc.__cause__, RecursionError) or normalized.startswith((b"[", b'"', _BOM)):
            raise
    extraction = extract_model_json_bytes(data)
    return ModelJsonParse(_parse_model_document(extraction.effective_bytes), extraction)


def parse_model_json_bytes(data: bytes) -> AgentJsonDocument:
    """Backward-compatible document API for model-authored JSON only."""
    return parse_model_json_response(data).document


__all__ = [
    "MAX_MODEL_JSON_EXTRACTION_BYTES",
    "ModelJsonExtraction",
    "ModelJsonParse",
    "extract_model_json_bytes",
    "normalize_model_json_bytes",
    "parse_model_json_bytes",
    "parse_model_json_response",
]
