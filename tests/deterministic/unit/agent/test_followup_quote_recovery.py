"""Recover only a clearly isolated answer; never guess the control envelope."""
import itertools
import hashlib
import json

import pytest

from problem_locator.followup import context, worker
from problem_locator.followup.context import parse_result
from problem_locator.followup.models import MAX_CONTEXT_BYTES
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from tests.deterministic.unit.agent.test_followup_result_protocol import PROTOCOL, REQUEST_KEY, execute_raw
from tests.deterministic.unit.agent.test_report_followup import OWNER, system, submit, view, original_rows


def raw_answer(body, *, action="ANSWER", request_key=REQUEST_KEY, order=("protocol", "request_key", "action")):
    fields = {"protocol": PROTOCOL, "request_key": request_key, "action": action}
    prefix = ",".join(json.dumps(key) + ":" + json.dumps(fields[key]) for key in order)
    return "{" + prefix + ',"answer_markdown":"' + body + '"}'


@pytest.mark.parametrize("action", ["ANSWER", "REQUEST_INPUT"])
@pytest.mark.parametrize("body,expected", [
    ('这里的"超时"并非根因。', '这里的"超时"并非根因。'),
    ('第一行\n请核对\t"超时"。\r\n第二行。', '第一行\n请核对\t"超时"。\r\n第二行。'),
    (r'路径 C:\\logs\\new；字面 \\n；已有 \"正确\"；还有 "缺失"。',
     '路径 C:\\logs\\new；字面 \\n；已有 "正确"；还有 "缺失"。'),
    ('中文“引号”与 ASCII "引号"共存。', '中文“引号”与 ASCII "引号"共存。'),
    ('结尾是"引用"', '结尾是"引用"'),
], ids=["quotes", "controls-and-quotes", "existing-escapes", "mixed-quotes", "trailing-quote"])
def test_recovers_answer_quotes_without_changing_text_or_existing_escapes(action, body, expected):
    result = parse_result(raw_answer(body, action=action), request_key=REQUEST_KEY)
    assert result.action == action
    assert result.answer_markdown == expected


@pytest.mark.parametrize("order", itertools.permutations(("protocol", "request_key", "action")))
def test_metadata_order_is_irrelevant_when_answer_is_last(order):
    result = parse_result(raw_answer('使用"日志"核对。', order=order), request_key=REQUEST_KEY)
    assert result.answer_markdown == '使用"日志"核对。'


@pytest.mark.parametrize("slashes", [1, 2, 3, 4, 5, 6])
def test_quote_recovery_preserves_backslash_parity(slashes):
    body = "路径 " + "\\" * slashes + '"内容；还有"引用"。'
    result = parse_result(raw_answer(body), request_key=REQUEST_KEY)
    assert result.answer_markdown == "路径 " + "\\" * (slashes // 2) + '"内容；还有"引用"。'


@pytest.mark.parametrize("mutate", [
    lambda raw: raw[:-1] + ',"action":"CONTINUE_DIAGNOSIS"}',
    lambda raw: raw[:-1] + ',"unexpected":"内容"}',
    lambda raw: raw[:-1] + r',"\u0061ction":"ANSWER"}',
    lambda raw: raw + '{"extra":"第二个对象"}',
    lambda raw: raw + '{}',
    lambda raw: raw[:-1],
    lambda raw: raw[:-2] + '}',
    lambda raw: raw[:-1] + ',}',
    lambda raw: raw.replace(PROTOCOL, "unknown"),
    lambda raw: raw.replace(REQUEST_KEY, "00000000-0000-0000-0000-000000000002"),
    lambda raw: raw.replace('"ANSWER"', '"CONTINUE_DIAGNOSIS"'),
    lambda raw: raw.replace('"ANSWER"', '"AN"SWER"'),
    lambda raw: raw.replace('"answer_markdown"', '"answer"_markdown"'),
    lambda raw: raw.replace('"protocol":', '"extra":"value","protocol":'),
    lambda raw: raw.replace('"protocol":', '"action":"ANSWER","protocol":'),
    lambda raw: "```json\n" + raw + "\n```",
    lambda raw: "说明：\n" + raw,
    lambda raw: raw + "\n说明结束。",
    lambda raw: "\ufeff" + raw,
])
def test_quote_recovery_cannot_hide_fields_change_identity_or_complete_a_broken_envelope(mutate):
    with pytest.raises(ValueError):
        parse_result(mutate(raw_answer('请看"日志"。\n保留正文。')), request_key=REQUEST_KEY)


@pytest.mark.parametrize("body", [
    '示例 {"timeout":3000}，还有"引号"。',
    '正文"} {"answer_markdown":"另一段"。',
    '正文", unexpected: 1；还有"引号"。',
    r'路径 C:\logs；还有"引号"。',
    r'错误 \u123x；还有"引号"。',
    r'错误 \ud800；还有"引号"。',
    '"引用"' + "a" * MAX_CONTEXT_BYTES,
], ids=["json-example", "object-boundary", "unquoted-field", "bad-escape", "bad-unicode", "surrogate", "oversize"])
def test_ambiguous_structural_text_invalid_escapes_and_oversize_are_not_recovered(body):
    with pytest.raises(ValueError):
        parse_result(raw_answer(body), request_key=REQUEST_KEY)


@pytest.mark.parametrize("key,value", [
    ("action", "REQUEST_INPUT"), ("request_key", REQUEST_KEY),
    ("protocol", PROTOCOL), ("answer_markdown", "另一份答案"),
])
def test_duplicate_fields_are_rejected_even_without_syntax_errors(key, value):
    raw = raw_answer("普通回答。")[:-1] + "," + json.dumps(key) + ":" + json.dumps(value) + "}"
    with pytest.raises(ValueError):
        parse_result(raw, request_key=REQUEST_KEY)


def test_valid_json_with_json_examples_and_any_field_order_needs_no_recovery():
    answer = '使用 {"action":"CONTINUE_DIAGNOSIS"} 作示例，路径 C:\\new，字面 \\n。'
    raw = json.dumps({"answer_markdown": answer, "action": "ANSWER",
        "request_key": REQUEST_KEY, "protocol": PROTOCOL}, ensure_ascii=False)
    assert parse_result(raw, request_key=REQUEST_KEY).answer_markdown == answer


def test_broken_answer_before_control_fields_is_not_recovered():
    raw = '{"answer_markdown":"请看"日志"。",' + raw_answer("unused")[1:]
    with pytest.raises(ValueError):
        parse_result(raw, request_key=REQUEST_KEY)


@pytest.mark.parametrize("slashes", [1, 2, 3, 4])
def test_closing_delimiter_cannot_be_an_escaped_quote(slashes):
    body = '引用"内容"。' + "\\" * slashes
    if slashes % 2:
        with pytest.raises(ValueError):
            parse_result(raw_answer(body), request_key=REQUEST_KEY)
    else:
        assert parse_result(raw_answer(body), request_key=REQUEST_KEY).answer_markdown == '引用"内容"。' + "\\" * (slashes // 2)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_values_do_not_trigger_quote_recovery(monkeypatch, token):
    monkeypatch.setattr(context, "_recover_answer_quotes", lambda *args: pytest.fail("semantic JSON error"))
    raw = raw_answer("普通回答。")[:-1] + ',"number":' + token + '}'
    with pytest.raises(ValueError):
        parse_result(raw, request_key=REQUEST_KEY)


def test_recovery_records_only_quote_insertions_at_original_utf8_byte_offsets():
    raw = raw_answer(r'中文🧭\u4e2d\n已有 \"引号\"；未转义"引用"。')
    records = []
    parsed = parse_result(raw, request_key=REQUEST_KEY, on_recovery=lambda *args: records.append(args))
    assert parsed.answer_markdown == '中文🧭中\n已有 "引号"；未转义"引用"。'
    assert len(records) == 1
    original, effective, offsets = records[0]
    assert original == raw.encode("utf-8") and len(offsets) == 2
    restored = bytearray(effective)
    for count, offset in reversed(list(enumerate(offsets))):
        assert original[offset:offset + 1] == b'"'
        assert restored[offset + count:offset + count + 2] == b'\\"'
        del restored[offset + count]
    assert restored == original
    assert json.loads(effective)["answer_markdown"] == parsed.answer_markdown


@pytest.mark.parametrize("body", ['合法正文。', r'已转义\"引号\"与\n换行。'])
def test_valid_answers_do_not_create_recovery_records(body):
    parse_result(raw_answer(body), request_key=REQUEST_KEY,
        on_recovery=lambda *args: pytest.fail("valid JSON needs no recovery"))


@pytest.mark.parametrize("body", ['"引用"' + "a" * 65536, r'"引用"\ud800', '"引用":未知结构'],
    ids=["answer-limit", "invalid-unicode", "structure"])
def test_rejected_answers_do_not_create_recovery_records(body):
    with pytest.raises(ValueError):
        parse_result(raw_answer(body), request_key=REQUEST_KEY,
            on_recovery=lambda *args: pytest.fail("invalid answer must not be recorded as recovered"))


def test_quote_recovery_does_not_reparse_each_quote(monkeypatch):
    calls = []
    original = context._load_result
    def load(value):
        calls.append(len(value))
        return original(value)
    monkeypatch.setattr(context, "_load_result", load)
    body = '"引用"。' * 1000
    assert parse_result(raw_answer(body), request_key=REQUEST_KEY).answer_markdown == body
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["ANSWER", "REQUEST_INPUT"])
def test_stream_to_worker_preserves_recovered_answer_and_private_receipts(system, action, monkeypatch):
    before = original_rows(system)
    receipt = submit(system)
    answer = '先核对"超时"。\n字段\t值\r\n下一行。'
    raw = raw_answer(answer, action=action, request_key=receipt.followup_id)
    event = json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "result": raw}, ensure_ascii=False).encode("utf-8") + b"\n"
    stream = AgentStreamTelemetry()
    for offset in range(0, len(event), 7):
        stream.write(event[offset:offset + 7])
    execute_raw(system, stream.final_result)
    events = []
    monkeypatch.setattr(worker, "log_event", lambda event, **fields: events.append((event, fields)))
    system.service.continue_diagnosis = lambda *args: pytest.fail("recovery cannot start diagnosis")
    assert system.service.worker.run_once()
    result = view(system).items[0]
    assert result.status == "COMPLETED" and result.diagnosis_run_id is None
    assert result.answer_markdown.endswith(answer)
    assert len(system.backend.calls) == 1 and original_rows(system) == before
    runtime = system.backend.calls[0]["workspace_root"] / "runtime"
    original = (runtime / "followup-response-original.txt").read_bytes()
    effective = (runtime / "followup-response-effective.txt").read_bytes()
    recorded = json.loads((runtime / "followup-response-recovery.json").read_bytes())
    assert original == raw.encode("utf-8")
    assert json.loads(effective, strict=False)["answer_markdown"] == answer
    assert recorded["raw_sha256"] == hashlib.sha256(original).hexdigest()
    assert recorded["effective_sha256"] == hashlib.sha256(effective).hexdigest()
    assert recorded["effective_size_bytes"] == recorded["raw_size_bytes"] + len(recorded["inserted_escape_offsets"])
    assert events[0][0] == "agent.followup.answer_quotes_recovered"
    assert events[0][1]["followup_id"] == receipt.followup_id
    assert "超时" not in json.dumps(events, ensure_ascii=False)


def test_recovery_receipt_write_failure_cannot_publish_answer_or_retry_model(system, monkeypatch):
    receipt = submit(system)
    execute_raw(system, raw_answer('请看"日志"。', request_key=receipt.followup_id))
    original_write = worker.write_new
    def write(path, data):
        if path.name == "followup-response-recovery.json":
            raise OSError("receipt write failed")
        return original_write(path, data)
    monkeypatch.setattr(worker, "write_new", write)
    assert not system.service.worker.run_once()
    result = view(system).items[0]
    assert result.status == "FAILED" and result.answer_markdown is None
    assert len(system.backend.calls) == 1


@pytest.mark.parametrize("record_name", [
    "followup-response-original.txt", "followup-response-effective.txt", "followup-response-recovery.json",
], ids=["original", "effective", "receipt"])
@pytest.mark.parametrize("stop_kind", ["user", "shutdown"])
def test_cancel_during_recovery_record_write_discards_answer(system, monkeypatch, record_name, stop_kind):
    receipt = submit(system)
    execute_raw(system, raw_answer('请看"日志"。', request_key=receipt.followup_id))
    original_write = worker.write_new
    stopped = []
    def write_then_stop(path, data):
        original_write(path, data)
        if path.name == record_name:
            if stop_kind == "user":
                system.service.stop(system.cid, system.rid, receipt.followup_id,
                    "stop-during-recovery", owner_key=OWNER)
            else:
                system.service.shutdown(0)
            stopped.append(True)
    monkeypatch.setattr(worker, "write_new", write_then_stop)
    system.service.continue_diagnosis = lambda *args: pytest.fail("cancelled recovery cannot start diagnosis")
    system.service.worker.run_once()
    result = view(system).items[0]
    assert stopped == [True]
    assert result.status == ("CANCELLED" if stop_kind == "user" else "INTERRUPTED")
    assert result.answer_markdown is None and result.diagnosis_run_id is None
    assert not system.service.worker.run_once()
    assert len(system.backend.calls) == 1
