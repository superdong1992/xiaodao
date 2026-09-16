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
    rb"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z"
)
_FENCE_OPEN = re.compile(rb"```(?:json)?[ \t]*\Z")
_ARRAY_HINT = re.compile(rb'\[[ \t]*(?:\Z|[\[\]{}"0-9tfn-])')
_SCALAR_LINE = re.compile(
    rb'(?:true|false|null|NaN|Infinity|-Infinity|'
    rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
    rb'[ \t]*(?:\Z|[,}\]])'
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
    """Locate one final envelope in linear time, without parsing candidates."""
    cursor = start
    saw_prose = False
    while cursor < end:
        newline = data.find(b"\n", cursor, end)
        next_line = end if newline < 0 else newline + 1
        line_end = end if newline < 0 else newline
        line_start, line_end = _trim_range(data, cursor, line_end)
        line = data[line_start:line_end]
        if not line:
            cursor = next_line
            continue
        if line.startswith(b"```"):
            if _FENCE_OPEN.fullmatch(line) is None or not saw_prose:
                raise InvalidJsonBytesError("model JSON extraction requires one final JSON fence")
            body_start = next_line
            cursor = next_line
            while cursor < end:
                newline = data.find(b"\n", cursor, end)
                next_line = end if newline < 0 else newline + 1
                line_end = end if newline < 0 else newline
                close_start, close_end = _trim_range(data, cursor, line_end)
                closing = data[close_start:close_end]
                if closing.startswith(b"```"):
                    if closing != b"```" or data[next_line:end].strip(_WHITESPACE):
                        raise InvalidJsonBytesError("model JSON extraction has ambiguous or trailing content")
                    body_start, body_end = _trim_range(data, body_start, cursor)
                    if data[body_start:body_start + 1] != b"{" or data[body_end - 1:body_end] != b"}":
                        raise InvalidJsonBytesError("model JSON extraction requires an object body")
                    # A damaged string may be handled by a surface-specific
                    # repair after extraction. An earlier complete object may
                    # never be selected or discarded to resolve ambiguity.
                    complete_end = _object_end(data, body_start, body_end)
                    if complete_end is not None and complete_end != body_end:
                        raise InvalidJsonBytesError("model JSON extraction contains multiple or trailing values")
                    return body_start, body_end, "final_json_fence_v1"
                cursor = next_line
            raise InvalidJsonBytesError("model JSON extraction has an incomplete fence")
        if line.startswith(b"{"):
            if not saw_prose:
                raise InvalidJsonBytesError("model JSON extraction cannot unwrap a damaged JSON root")
            complete_end = _object_end(data, line_start, end)
            if complete_end is None or data[complete_end:end].strip(_WHITESPACE):
                raise InvalidJsonBytesError("model JSON extraction requires one complete final object")
            return line_start, complete_end, "final_json_object_v1"
        if (
            b"{" in line
            or b"```" in line
            or line.startswith((b"[", b'"', _BOM))
            or _ARRAY_HINT.search(line) is not None
            or _SCALAR_LINE.match(line) is not None
        ):
            raise InvalidJsonBytesError("model JSON extraction has another possible JSON value")
        saw_prose = True
        cursor = next_line
    raise InvalidJsonBytesError("model JSON extraction found no final object")


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
        # Preserve strict errors and the existing surface-specific recovery
        # path for roots and whole fences. Never mine their nested objects.
        if isinstance(exc.__cause__, RecursionError) or normalized.startswith((b"{", b"[", b'"', _BOM)):
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
