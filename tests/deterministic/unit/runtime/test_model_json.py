from __future__ import annotations

import json

import pytest

from problem_locator.contracts import InvalidJsonBytesError, bytes_sha256
from problem_locator.integrations.agent_json import parse_agent_json_bytes
from problem_locator.runtime import model_json
from problem_locator.runtime.model_json import (
    MAX_MODEL_JSON_EXTRACTION_BYTES,
    extract_model_json_bytes,
    parse_model_json_bytes,
    parse_model_json_response,
)


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
def test_model_json_does_not_repair_or_unwrap_ambiguous_content(raw):
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_bytes(raw)


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf{}", b"```json\n{}\n```"])
def test_public_agent_json_input_remains_strict_when_model_output_accepts_wrapper(raw):
    assert parse_model_json_bytes(raw).value == {}
    with pytest.raises(InvalidJsonBytesError):
        parse_agent_json_bytes(raw)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("fence", [None, b"```json", b"```"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_markdown_followed_by_one_final_object_is_extracted_without_changing_bytes(newline, fence, bom):
    body = json.dumps({
        "reason": '保留引号 " 和花括号 { }、反斜杠 \\、```',
        "nested": [{"value": [1, True, None]}, {}],
    }, ensure_ascii=False, indent=2).encode().replace(b"\n", newline)
    prefix = bom + "## 诊断说明\n已完成初步分析，最终结果如下。\n".encode().replace(b"\n", newline)
    raw = prefix + (fence + newline if fence is not None else b"") + body
    raw += (newline + b"```" if fence is not None else b"") + newline + b" \t"
    result = parse_model_json_response(raw)
    assert result.document == parse_agent_json_bytes(body)
    extraction = result.extraction
    assert extraction is not None
    assert extraction.raw_bytes == raw
    assert extraction.effective_bytes == body
    assert raw[extraction.start:extraction.end] == body
    assert extraction.start == raw.index(body)
    assert extraction.rule == ("final_json_fence_v1" if fence is not None else "final_json_object_v1")
    assert parse_model_json_bytes(raw) == result.document
    assert extraction.to_receipt() == {
        "rule": extraction.rule,
        "raw_sha256": bytes_sha256(raw),
        "raw_size_bytes": len(raw),
        "effective_sha256": bytes_sha256(body),
        "effective_size_bytes": len(body),
        "start_byte": extraction.start,
        "end_byte": extraction.end,
    }
    assert "诊断说明" not in json.dumps(extraction.to_receipt(), ensure_ascii=False)


@pytest.mark.parametrize("raw", [
    b"Explanation.\n{}\n{}",
    b"Example: {}\nFinal:\n{}",
    b"Explanation.\n{}\nDone.",
    b"Explanation.\n```json\n{}\n```\nDone.",
    b"Explanation.\n```json\n{}\n```\n```json\n{}\n```",
    b"Explanation.\n```json\n{}\n{}\n```",
    b"Explanation.\n```json\n{}\n```\n{}",
    b"Explanation.\n```javascript\n{}\n```",
    b"Explanation.\n```JSON\n{}\n```",
    b"Explanation.\n```json\n{}",
    b"Explanation.\n```json\n```json\n{}\n```\n```",
    b"Explanation.\n[\n{}\n]",
    b"[\n{}\n] trailing",
    b'{"outer":\n{}',
    b'"damaged string\n{}',
    b'Explanation.\n{"outer":\n{}',
    b'Explanation.\n{"outer":[}\n{}',
    b'Explanation.\n"damaged string\n{}',
    b"Explanation.\nnull\n{}",
    b"Explanation.\n123\n{}",
    b"Explanation.\n-12\n{}",
    b"Explanation.\n12.5e-2\n{}",
    b"Explanation with array [1, 2].\n{}",
    b"Explanation with damaged array [\n{}",
    b"Explanation. {}",
    b"Explanation.\n```json\n[]\n```",
    b"Explanation.\n```json\n\xef\xbb\xbf{}\n```",
    b"\xef\xbb\xbf\xef\xbb\xbfExplanation.\n{}",
    b"Explanation\xff.\n{}",
    b"Explanation.\n{}\xff",
    b"No final object.",
])
def test_mixed_json_rejects_ambiguous_damaged_or_non_json_envelopes(raw):
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_response(raw)


@pytest.mark.parametrize("body", [
    b'{"a":1,"a":2}',
    b'{"number":NaN}',
    b'{"number":Infinity}',
    b'{"trailing":1,}',
    b'{"reason":"unescaped "word""}',
    b'{"reason":"invalid\\q"}',
])
@pytest.mark.parametrize("fence", [False, True])
def test_extracted_body_still_requires_strict_json(body, fence):
    raw = b"Explanation.\n" + (b"```json\n" + body + b"\n```" if fence else body)
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_response(raw)


def test_explicit_envelope_extraction_does_not_perform_route_specific_quote_repair():
    body = b'{"reason":"choose "rpc"","skill_id":null,"confidence":1}'
    raw = b"Explanation.\n```json\n" + body + b"\n```"
    assert extract_model_json_bytes(raw).effective_bytes == body
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_response(raw)


@pytest.mark.parametrize("raw", [b"Explanation.\n{}", b"Explanation.\n```json\n{}\n```"])
def test_public_inputs_do_not_gain_mixed_model_compatibility(raw):
    assert parse_model_json_bytes(raw).value == {}
    with pytest.raises(InvalidJsonBytesError):
        parse_agent_json_bytes(raw)


@pytest.mark.parametrize("wrapper", [False, True])
def test_valid_json_fast_path_never_scans_and_keeps_existing_size_budget(monkeypatch, wrapper):
    body = json.dumps({"reason": "x" * MAX_MODEL_JSON_EXTRACTION_BYTES}).encode()
    raw = b"```json\n" + body + b"\n```" if wrapper else body
    calls = []
    original = model_json.parse_agent_json_bytes

    def parse(data):
        calls.append(len(data))
        return original(data)

    def unexpected_scan(*args):
        pytest.fail("valid JSON must never enter the extraction scanner")

    monkeypatch.setattr(model_json, "parse_agent_json_bytes", parse)
    monkeypatch.setattr(model_json, "_scan_mixed_model_json", unexpected_scan)
    result = parse_model_json_response(raw)
    assert result.extraction is None
    assert result.document.value["reason"] == "x" * MAX_MODEL_JSON_EXTRACTION_BYTES
    assert len(calls) == 1


def test_mixed_extraction_limit_is_explicit_and_checked_before_scanning(monkeypatch):
    raw = b"x" * MAX_MODEL_JSON_EXTRACTION_BYTES + b"\n{}"

    def unexpected_scan(*args):
        pytest.fail("oversized fallback must not enter the scanner")

    monkeypatch.setattr(model_json, "_scan_mixed_model_json", unexpected_scan)
    with pytest.raises(InvalidJsonBytesError, match="1048576-byte fallback limit"):
        parse_model_json_response(raw)


@pytest.mark.parametrize("fence", [False, True])
def test_fallback_scans_once_and_parses_at_most_twice(monkeypatch, fence):
    body = json.dumps({"items": [{"value": index} for index in range(1000)]}).encode()
    raw = b"Explanation.\n" + (b"```json\n" + body + b"\n```" if fence else body)
    calls = {"parse": 0, "scan": 0, "object": 0}

    def count(name, function):
        def counted(*args):
            calls[name] += 1
            return function(*args)
        return counted

    monkeypatch.setattr(model_json, "parse_agent_json_bytes", count("parse", model_json.parse_agent_json_bytes))
    monkeypatch.setattr(model_json, "_scan_mixed_model_json", count("scan", model_json._scan_mixed_model_json))
    monkeypatch.setattr(model_json, "_object_end", count("object", model_json._object_end))
    assert len(parse_model_json_response(raw).document.value["items"]) == 1000
    assert calls == {"parse": 2, "scan": 1, "object": 1}


def test_ambiguous_first_candidate_never_searches_or_parses_later_candidates(monkeypatch):
    raw = b"Explanation.\n" + b' {"a": 1}\n' * 1000
    calls = []
    original = model_json._object_end

    def scan(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(model_json, "_object_end", scan)
    with pytest.raises(InvalidJsonBytesError):
        parse_model_json_response(raw)
    assert calls == [raw.index(b"{")]


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("fence", [False, True])
def test_excessive_json_nesting_is_a_controlled_failure_without_retry(monkeypatch, mixed, fence):
    # Python 3.12's C JSON decoder has a separate recursion limit. This also
    # exceeds that limit while keeping the mixed response below its byte cap.
    depth = 10000
    body = b'{"nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    raw = b"```json\n" + body + b"\n```" if fence else body
    if mixed:
        raw = b"Explanation.\n" + raw
    calls = {"parse": 0, "scan": 0}
    original_parse = model_json.parse_agent_json_bytes
    original_scan = model_json._scan_mixed_model_json

    def parse(data):
        calls["parse"] += 1
        return original_parse(data)

    def scan(*args):
        calls["scan"] += 1
        return original_scan(*args)

    monkeypatch.setattr(model_json, "parse_agent_json_bytes", parse)
    monkeypatch.setattr(model_json, "_scan_mixed_model_json", scan)
    with pytest.raises(InvalidJsonBytesError, match="nesting exceeds") as error:
        parse_model_json_response(raw)
    assert isinstance(error.value.__cause__, RecursionError)
    assert calls == {"parse": 2 if mixed else 1, "scan": 1 if mixed else 0}
