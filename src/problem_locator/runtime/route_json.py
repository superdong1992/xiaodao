"""Bounded, auditable quote recovery for the model-authored ROUTE reason only.

Strict JSON is always tried first. Recovery only inserts a backslash before an
unescaped quote inside one uniquely identifiable reason string. It never repairs
public input, control fields, escapes, delimiters, truncation or duplicate keys.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from problem_locator.contracts import InvalidJsonBytesError, bytes_sha256
from problem_locator.integrations.agent_json import (
    AgentJsonDocument,
    parse_agent_json_bytes,
)
from problem_locator.runtime.model_json import (
    ModelJsonExtraction,
    extract_model_json_bytes,
    normalize_model_json_bytes,
)


# These limits apply only after strict parsing fails. At most 128 candidates of
# at most 64 KiB are parsed; normal valid responses retain their existing limits.
MAX_ROUTE_RECOVERY_BYTES = 64 * 1024
MAX_ROUTE_RECOVERY_QUOTES = 128
_FIELDS = frozenset({"skill_id", "reason", "confidence"})
# A colon after an apparent quoted key could be a swallowed field, including a
# field whose preceding comma is missing. Conservative rejection is intentional.
_FIELD_SHAPE = re.compile(r'"(?:[^"\\]|\\.)*"\s*:')
_WHITESPACE = " \t\r\n"
_DECODER = json.JSONDecoder()


@dataclass(frozen=True, slots=True)
class RouteQuoteRecovery:
    raw_bytes: bytes
    effective_bytes: bytes
    inserted_escape_offsets: tuple[int, ...]

    def to_receipt(self) -> dict[str, Any]:
        """Return content-free metadata; offsets refer to original UTF-8 bytes."""
        return {
            "schema_version": 1,
            "rule": "route_reason_unescaped_quotes_v1",
            "raw_sha256": bytes_sha256(self.raw_bytes),
            "raw_size_bytes": len(self.raw_bytes),
            "effective_sha256": bytes_sha256(self.effective_bytes),
            "effective_size_bytes": len(self.effective_bytes),
            "inserted_escape_offsets": list(self.inserted_escape_offsets),
        }


@dataclass(frozen=True, slots=True)
class RouteJsonParse:
    document: AgentJsonDocument
    recovery: RouteQuoteRecovery | None = None
    extraction: ModelJsonExtraction | None = None


def _skip_space(text: str, offset: int) -> int:
    while offset < len(text) and text[offset] in _WHITESPACE:
        offset += 1
    return offset


def _expect(text: str, offset: int, character: str) -> int:
    offset = _skip_space(text, offset)
    if offset >= len(text) or text[offset] != character:
        raise InvalidJsonBytesError("ROUTE quote recovery requires intact object structure")
    return offset + 1


def _control_value(key: str, value: Any) -> bool:
    if key == "skill_id":
        return value is None or isinstance(value, str)
    return (
        key == "confidence"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1
        and math.isfinite(value)
    )


def _reason_start(text: str) -> int:
    """Read the prefix strictly; only the reason's quoted value may be broken."""
    offset = _expect(text, 0, "{")
    seen: set[str] = set()
    while True:
        offset = _skip_space(text, offset)
        try:
            key, offset = _DECODER.raw_decode(text, offset)
        except (ValueError, RecursionError) as exc:
            raise InvalidJsonBytesError("ROUTE quote recovery requires an intact field name") from exc
        if not isinstance(key, str) or key not in _FIELDS or key in seen:
            raise InvalidJsonBytesError("ROUTE quote recovery forbids duplicate or extra fields")
        seen.add(key)
        offset = _expect(text, offset, ":")
        offset = _skip_space(text, offset)
        if key == "reason":
            if offset >= len(text) or text[offset] != '"':
                raise InvalidJsonBytesError("ROUTE quote recovery requires a reason string")
            return offset + 1
        try:
            value, offset = _DECODER.raw_decode(text, offset)
        except (ValueError, RecursionError) as exc:
            raise InvalidJsonBytesError("ROUTE quote recovery requires intact control fields") from exc
        if not _control_value(key, value):
            raise InvalidJsonBytesError("ROUTE quote recovery requires valid control fields")
        offset = _expect(text, offset, ",")


def _insert_escapes(raw: bytes, offsets: tuple[int, ...]) -> bytes:
    parts: list[bytes] = []
    previous = 0
    for offset in offsets:
        parts.extend((raw[previous:offset], b"\\"))
        previous = offset
    parts.append(raw[previous:])
    return b"".join(parts)


def _route_shape(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _FIELDS
        and isinstance(value["reason"], str)
        and _control_value("skill_id", value["skill_id"])
        and _control_value("confidence", value["confidence"])
    )


def _parse_route_body(raw: bytes, normalized: bytes, body_offset: int) -> RouteJsonParse:
    """Parse one selected body; never search another candidate during recovery."""
    try:
        return RouteJsonParse(parse_agent_json_bytes(normalized))
    except RecursionError as exc:
        raise InvalidJsonBytesError("ROUTE JSON nesting exceeds the parser limit") from exc
    except InvalidJsonBytesError as exc:
        # Duplicate keys, non-finite numbers and invalid Unicode are not syntax
        # failures eligible for recovery, even if they occur inside a reason.
        if not isinstance(exc.__cause__, json.JSONDecodeError):
            raise
        original_error = exc
    if len(raw) > MAX_ROUTE_RECOVERY_BYTES:
        raise InvalidJsonBytesError("ROUTE quote recovery exceeds the 64 KiB input budget") from original_error
    text = normalized.decode("utf-8")
    start = _reason_start(text)
    offset = start
    quotes: list[int] = []
    candidates: list[RouteJsonParse] = []
    while offset < len(text):
        character = text[offset]
        if ord(character) < 0x20:
            break
        if character == "\\":
            # Existing escapes are immutable. An invalid one ends the region in
            # which a string can legally continue; do not escape its backslash.
            if offset + 1 >= len(text):
                break
            escaped = text[offset + 1]
            if escaped == "u":
                digits = text[offset + 2:offset + 6]
                if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                    break
                offset += 6
                continue
            if escaped not in '"\\/bfnrt':
                break
            offset += 2
            continue
        if character != '"':
            offset += 1
            continue
        if len(quotes) >= MAX_ROUTE_RECOVERY_QUOTES:
            raise InvalidJsonBytesError("ROUTE quote recovery exceeds the 128-quote search budget") from original_error
        if quotes and _FIELD_SHAPE.search(text[start:offset]) is None:
            relative_offsets = tuple(len(text[:position].encode("utf-8")) for position in quotes)
            byte_offsets = tuple(body_offset + position for position in relative_offsets)
            effective_body = _insert_escapes(normalized, relative_offsets)
            try:
                document = parse_agent_json_bytes(effective_body)
            except (InvalidJsonBytesError, RecursionError):
                pass
            else:
                if _route_shape(document.value):
                    effective = (
                        effective_body if body_offset == 0 and len(normalized) == len(raw)
                        else _insert_escapes(raw, byte_offsets)
                    )
                    candidates.append(RouteJsonParse(
                        document, RouteQuoteRecovery(raw, effective, byte_offsets)
                    ))
                    if len(candidates) > 1:
                        raise InvalidJsonBytesError("ROUTE reason quote recovery is ambiguous") from original_error
        quotes.append(offset)
        offset += 1
    if len(candidates) != 1:
        raise InvalidJsonBytesError("ROUTE reason quotes cannot be recovered unambiguously") from original_error
    return candidates[0]


def parse_route_json_bytes(raw: bytes) -> RouteJsonParse:
    """Parse a complete ROUTE response, with bounded presentation compatibility.

    Strict parsing and existing reason-only recovery run first. Mixed Markdown
    may supply one unambiguous final JSON body, which then receives exactly the
    same parsing and recovery. Recovery receipts preserve all original bytes
    except their recorded inserted backslashes, even when extraction is needed.
    Extraction receipts retain the selected original body before quote recovery.
    The caller must still perform the complete normal ROUTE business validation.
    """
    normalized, body_offset = normalize_model_json_bytes(raw)
    try:
        return _parse_route_body(raw, normalized, body_offset)
    except InvalidJsonBytesError:
        # A JSON root or an already unwrapped complete fence must not be searched
        # again for nested fragments after syntax/semantic/recovery rejection.
        consumed_fence = raw[:body_offset].removeprefix(b"\xef\xbb\xbf").strip(b" \t\r\n")
        if consumed_fence or normalized.startswith((b"{", b"[", b'"')):
            raise
    extraction = extract_model_json_bytes(raw)
    parsed = _parse_route_body(raw, extraction.effective_bytes, extraction.start)
    return RouteJsonParse(parsed.document, parsed.recovery, extraction)


__all__ = [
    "MAX_ROUTE_RECOVERY_BYTES", "MAX_ROUTE_RECOVERY_QUOTES",
    "RouteJsonParse", "RouteQuoteRecovery", "parse_route_json_bytes",
]
