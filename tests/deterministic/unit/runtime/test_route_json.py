from __future__ import annotations

import itertools
import json

import pytest

from problem_locator.contracts import InvalidJsonBytesError, bytes_sha256
from problem_locator.runtime.model_json import parse_model_json_bytes
from problem_locator.runtime.route_json import (
    MAX_ROUTE_RECOVERY_BYTES,
    MAX_ROUTE_RECOVERY_QUOTES,
    parse_route_json_bytes,
)


def _route(reason: str) -> bytes:
    return ('{"skill_id":null,"reason":' + reason + ',"confidence":0.9}').encode()


def test_recovers_reason_and_records_only_insertions_in_original_bytes():
    raw = _route('"use "rpc_timeout" because it matches"')
    result = parse_route_json_bytes(raw)
    assert result.document.value == {
        "skill_id": None, "reason": 'use "rpc_timeout" because it matches', "confidence": 0.9,
    }
    recovery = result.recovery
    assert recovery is not None
    assert recovery.raw_bytes == raw
    assert len(recovery.inserted_escape_offsets) == 2
    restored = bytearray(recovery.effective_bytes)
    for number, offset in reversed(list(enumerate(recovery.inserted_escape_offsets))):
        assert raw[offset:offset + 1] == b'"'
        assert restored[offset + number:offset + number + 2] == b'\\"'
        del restored[offset + number]
    assert bytes(restored) == raw
    receipt = recovery.to_receipt()
    assert receipt["raw_sha256"] == bytes_sha256(raw)
    assert receipt["effective_sha256"] == bytes_sha256(recovery.effective_bytes)
    assert receipt["raw_size_bytes"] == len(raw)
    assert receipt["effective_size_bytes"] == len(raw) + 2
    assert receipt["rule"] == "route_reason_unescaped_quotes_v1"
    assert "rpc_timeout" not in json.dumps(receipt)


@pytest.mark.parametrize("order", itertools.permutations(("skill_id", "reason", "confidence")))
def test_all_field_orders_have_the_same_unique_reason(order):
    fields = {"skill_id": '"unknown-but-unchanged"', "reason": '"choose "rpc""', "confidence": "0.9"}
    raw = ("{" + ",".join(json.dumps(key) + ":" + fields[key] for key in order) + "}").encode()
    result = parse_route_json_bytes(raw)
    assert result.recovery is not None
    assert result.document.value == {
        "skill_id": "unknown-but-unchanged", "reason": 'choose "rpc"', "confidence": 0.9,
    }


@pytest.mark.parametrize("prefix,suffix", [(b"", b""), (b"\xef\xbb\xbf", b""),
    (b"```json\n", b"\n```"), (b"\xef\xbb\xbf \r\n```\r\n", b"\r\n``` \r\n")])
def test_unicode_wrapper_offsets_refer_to_original_utf8_bytes(prefix, suffix):
    body = _route('"使用 "方法🦊" 定位"')
    raw = prefix + body + suffix
    result = parse_route_json_bytes(raw)
    assert result.document.value["reason"] == '使用 "方法🦊" 定位'
    recovery = result.recovery
    assert recovery is not None
    assert recovery.effective_bytes.startswith(prefix)
    assert recovery.effective_bytes.endswith(suffix)
    expected = (raw.index('"方法'.encode()), raw.index('" 定位'.encode()))
    assert recovery.inserted_escape_offsets == expected
    assert parse_model_json_bytes(recovery.effective_bytes) == result.document


def test_existing_escape_sequences_and_literal_backslashes_are_immutable():
    raw = _route(r'"path C:\\logs\\new and \"valid\" plus "broken", literal \\n and \u4e2d\n"')
    result = parse_route_json_bytes(raw)
    assert result.document.value["reason"] == 'path C:\\logs\\new and "valid" plus "broken", literal \\n and 中\n'
    assert result.recovery is not None
    assert b'\\"valid\\"' in result.recovery.effective_bytes
    assert b'\\u4e2d\\n' in result.recovery.effective_bytes
    assert len(result.recovery.inserted_escape_offsets) == 2


@pytest.mark.parametrize("slashes", [1, 2, 3, 4, 5, 6])
def test_quote_escape_parity_preserves_existing_backslashes(slashes):
    # The independent final pair forces recovery even when the first quote was
    # already protected by an odd number of backslashes.
    reason = '"a' + "\\" * slashes + '"b and "tail""'
    raw = _route(reason)
    result = parse_route_json_bytes(raw)
    recovery = result.recovery
    assert recovery is not None
    assert result.document.value["reason"] == "a" + "\\" * (slashes // 2) + '"b and "tail"'
    assert len(recovery.inserted_escape_offsets) == (2 if slashes % 2 else 3)
    prefix = b'a' + b"\\" * slashes
    assert prefix in recovery.effective_bytes


@pytest.mark.parametrize("reason", ['normal', '合法 "引号" 与反斜杠 \\ 和换行\n',
    'looks like a field: ", \\"extra\\": 1', '"' * 200])
def test_valid_json_is_never_recovered_or_constrained_by_recovery_budget(reason):
    raw = json.dumps({"skill_id": None, "reason": reason, "confidence": 1}, ensure_ascii=False).encode()
    result = parse_route_json_bytes(raw)
    assert result.recovery is None
    assert result.document.value["reason"] == reason


def test_valid_json_above_recovery_input_budget_keeps_existing_behavior():
    raw = _route(json.dumps("a" * MAX_ROUTE_RECOVERY_BYTES))
    assert parse_route_json_bytes(raw).recovery is None


@pytest.mark.parametrize("raw", [
    b'{"reason":"x"oops","extra":1,"skill_id":null,"confidence":0.9}',
    b'{"reason":"x"oops","reason":"y","skill_id":null,"confidence":0.9}',
    b'{"reason":"x"oops","skill_id":"a","skill_id":null,"confidence":0.9}',
    b'{"reason":"x"oops" "extra":1,"skill_id":null,"confidence":0.9}',
    br'{"reason":"x"oops","\u0073kill_id":"a","skill_id":null,"confidence":0.9}',
    b'{"reason":"x"oops"}{"reason":"y","skill_id":null,"confidence":0.9}',
    b'{"reason":"x"oops","confidence":0.1,"skill_id":null,"confidence":0.9}',
    b'{"skill_id":null,"skill_id":"a","reason":"x"oops","confidence":0.9}',
    b'{"extra":1,"reason":"x"oops","skill_id":null,"confidence":0.9}',
])
def test_recovery_cannot_swallow_control_duplicate_extra_or_missing_comma_fields(raw):
    with pytest.raises(InvalidJsonBytesError):
        parse_route_json_bytes(raw)


@pytest.mark.parametrize("raw", [
    _route('"broken "x" with\nliteral newline"'),
    _route('"broken "x" with\tliteral tab"'),
    _route('"broken "x" with\x00null"'),
    _route(r'"broken "x" with\q"'),
    _route(r'"broken "x" with\u123x"'),
    _route(r'"broken "x" with\ud800"'),
    b'{"skill_id":null,"reason":"broken "x","confidence":0.9',
    b'{"skill_id":null "reason":"broken "x"","confidence":0.9}',
    b'{"skill_id":null,"reason":"broken "x"" "confidence":0.9}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":0.9,}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":0.9}{}',
    b'prefix {"skill_id":null,"reason":"broken "x"","confidence":0.9}',
    b'{"skill_id":"broken "id"","reason":"good","confidence":0.9}',
    b'{"skill_id":null,"reason":3,"confidence":0.9,}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":true}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":NaN}',
    b'{"confidence":NaN,"skill_id":null,"reason":"broken "x""}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":Infinity}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":-1}',
    b'{"confidence":999999999999999999999999999999999999999999999999999,"skill_id":null,"reason":"broken "x""}',
    b'{"skill_id":null,"reason":"broken "x"","confidence":0.9}\xff',
    b'```json\n{"skill_id":null,"reason":"broken "x"","confidence":0.9}\n``` trailing',
])
def test_non_quote_failures_and_non_finite_control_values_remain_rejected(raw):
    with pytest.raises(InvalidJsonBytesError):
        parse_route_json_bytes(raw)


@pytest.mark.parametrize("raw", [
    b'{"skill_id":null,"reason":"good","confidence":NaN}',
    b'{"skill_id":null,"reason":"good","confidence":1,"reason":"duplicate"}',
])
def test_semantic_json_failures_do_not_enter_recovery(raw):
    with pytest.raises(InvalidJsonBytesError) as caught:
        parse_route_json_bytes(raw)
    assert not isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert "recovery" not in str(caught.value)


def test_bounded_recovery_rejects_oversized_input():
    with pytest.raises(InvalidJsonBytesError, match="64 KiB"):
        parse_route_json_bytes(_route('"' + "a" * MAX_ROUTE_RECOVERY_BYTES + '"x""'))


def test_bounded_recovery_rejects_exhausted_quote_search():
    with pytest.raises(InvalidJsonBytesError, match="128-quote"):
        parse_route_json_bytes(_route('"' + 'a"' * (MAX_ROUTE_RECOVERY_QUOTES + 1) + '"'))


def test_excessive_nesting_is_a_controlled_failure_without_quote_recovery():
    raw = b'[' * 10000 + b'0' + b']' * 10000
    with pytest.raises(InvalidJsonBytesError, match="nesting") as caught:
        parse_route_json_bytes(raw)
    assert isinstance(caught.value.__cause__, RecursionError)


def test_excessive_nesting_in_a_recovery_candidate_is_also_controlled():
    raw = b'{"reason":"x"y","skill_id":null,"confidence":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'
    with pytest.raises(InvalidJsonBytesError):
        parse_route_json_bytes(raw)


def test_conflicting_possible_control_field_boundaries_are_not_guessed():
    raw = b'{"reason":"x","confidence":0.1,"skill_id":"a"junk","confidence":0.9,"skill_id":null}'
    with pytest.raises(InvalidJsonBytesError):
        parse_route_json_bytes(raw)
