from __future__ import annotations

import pytest

from problem_locator.contracts import InvalidJsonBytesError
from problem_locator.integrations.agent_json import parse_agent_json_bytes
from problem_locator.runtime.model_json import parse_model_json_bytes


@pytest.mark.parametrize("prefix,suffix", [
    (b"", b""),
    (b"\xef\xbb\xbf", b""),
    (b"```json\n", b"\n```"),
    (b"```\r\n", b"\r\n```"),
    (b"\xef\xbb\xbf \r\n```json\r\n", b"\r\n```\r\n"),
])
def test_model_json_accepts_only_presentation_wrappers(prefix, suffix):
    result = parse_model_json_bytes(prefix + b'{ "reason": "literal ``` inside JSON" }' + suffix)
    assert result.value == {"reason": "literal ``` inside JSON"}
    assert result.canonical_bytes == parse_agent_json_bytes(b'{"reason":"literal ``` inside JSON"}').canonical_bytes


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbf\xef\xbb\xbf{}",
    b"```json\n\xef\xbb\xbf{}\n```",
    b"```json\n```json\n{}\n```\n```",
    b"The answer is:\n```json\n{}\n```",
    b"```json\n{}\n```\nDone.",
    b"```json\n{}\n```\n```json\n{}\n```",
    b"```json\n{}",
    b"```javascript\n{}\n```",
    b"{} {}",
    b'{"a":1,"a":2}',
    b'```json\n{"a":1,"a":2}\n```',
    b'{"number":NaN}',
    b'{"number":Infinity}',
    b'{"trailing":1,}',
    b'"\xff"',
])
def test_model_json_does_not_extract_repair_or_unwrap_ambiguous_content(raw):
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_bytes(raw)


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf{}", b"```json\n{}\n```"])
def test_public_agent_json_input_remains_strict_when_model_output_accepts_wrapper(raw):
    assert parse_model_json_bytes(raw).value == {}
    with pytest.raises(InvalidJsonBytesError):
        parse_agent_json_bytes(raw)
