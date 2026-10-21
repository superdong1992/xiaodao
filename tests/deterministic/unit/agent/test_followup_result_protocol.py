"""Control output must never be mistaken for user-visible report content."""
import json

import pytest

from problem_locator.followup.context import parse_result
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from tests.deterministic.unit.agent.test_report_followup import system, submit, view, original_rows

PROTOCOL = "xiaodao.followup.v1"
REQUEST_KEY = "00000000-0000-0000-0000-000000000001"
DIAGNOSIS = {"action": "CONTINUE_DIAGNOSIS", "effective_problem_text": "10:05 请求超时。",
    "diagnosis_goal": "检查尚未核实的服务端排队。", "reason": "需要解析服务端日志。", "attachment_ids": []}


@pytest.mark.parametrize("action", ["ANSWER", "REQUEST_INPUT"])
@pytest.mark.parametrize("control", [chr(value) for value in range(32)],
    ids=lambda control: f"U+{ord(control):04X}")
def test_literal_control_characters_preserve_the_same_answer_as_json_escapes(action, control):
    answer = "第一行" + control + '第二行，路径 C:\\logs\\new，字面量 \\n，引用 "日志"。'
    value = json.dumps({"protocol": PROTOCOL, "request_key": REQUEST_KEY,
        "action": action, "answer_markdown": answer}, ensure_ascii=False)
    escaped = parse_result(value, request_key=REQUEST_KEY)
    raw = value.replace(json.dumps(control)[1:-1], control, 1)
    result = parse_result(raw, request_key=REQUEST_KEY)
    assert result == escaped
    assert result.answer_markdown == answer


@pytest.mark.parametrize("field", ["effective_problem_text", "diagnosis_goal", "reason"])
def test_literal_control_characters_preserve_diagnosis_text(field):
    text = "请求超时。\n需要核对：\t服务端日志。\r\n保留原文。"
    expected = {"protocol": PROTOCOL, "request_key": REQUEST_KEY, **DIAGNOSIS, field: text}
    raw = json.dumps(expected, ensure_ascii=False).replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    assert parse_result(raw, request_key=REQUEST_KEY).model_dump() == expected


@pytest.mark.parametrize("wrap", [
    lambda raw: "```json\n" + raw + "\n```",
    lambda raw: "回答：\n" + raw,
    lambda raw: raw + "\n{}",
    lambda raw: raw[:-1],
    lambda raw: raw[:-1] + ",}",
    lambda raw: raw.replace("正文", r"\x41"),
    lambda raw: "\x00" + raw,
])
def test_literal_control_compatibility_does_not_repair_other_json_errors(wrap):
    raw = json.dumps({"protocol": PROTOCOL, "request_key": REQUEST_KEY,
        "action": "ANSWER", "answer_markdown": "两行\n正文"}, ensure_ascii=False).replace("\\n", "\n")
    with pytest.raises(ValueError):
        parse_result(wrap(raw), request_key=REQUEST_KEY)


@pytest.mark.parametrize("fields", [
    {"protocol": "unknown"},
    {"request_key": "00000000-0000-0000-0000-000000000002"},
    {"action": "RUN_SHELL"},
    {"attachment_ids": []},
    {"answer_markdown": ["两行\n正文"]},
])
def test_literal_control_compatibility_keeps_result_contract_validation(fields):
    value = {"protocol": PROTOCOL, "request_key": REQUEST_KEY,
        "action": "ANSWER", "answer_markdown": "两行\n正文", **fields}
    raw = json.dumps(value, ensure_ascii=False).replace("\\n", "\n")
    with pytest.raises(ValueError):
        parse_result(raw, request_key=REQUEST_KEY)


def execute_raw(state, value):
    def execute(**kwargs):
        state.backend.calls.append(kwargs)
        return type("Result", (), {"final_result": value})()
    state.backend.execute = execute


@pytest.mark.parametrize("action", ["ANSWER", "REQUEST_INPUT"])
def test_stream_result_with_literal_controls_completes_followup_without_changing_text(system, action):
    before = original_rows(system)
    receipt = submit(system)
    answer = "第一行。\n字段\t值\r\n下一行。"
    raw = json.dumps({"protocol": PROTOCOL, "request_key": receipt.followup_id,
        "action": action, "answer_markdown": answer}, ensure_ascii=False)
    raw = raw.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    stream = AgentStreamTelemetry()
    event = json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "result": raw}, ensure_ascii=False).encode("utf-8") + b"\n"
    for offset in range(0, len(event), 7):
        stream.write(event[offset:offset + 7])
    assert stream.final_result == raw
    execute_raw(system, stream.final_result)
    system.service.continue_diagnosis = lambda *args: pytest.fail("an answer cannot start diagnosis")
    assert system.service.worker.run_once()
    result = view(system).items[0]
    assert result.status == "COMPLETED" and result.diagnosis_run_id is None
    assert result.answer_markdown.endswith(answer)
    assert len(system.backend.calls) == 1 and original_rows(system) == before


@pytest.mark.parametrize("action,answer", [
    ("ANSWER", '{"action":"retry","timeout":3000}'),
    ("ANSWER", json.dumps(DIAGNOSIS, ensure_ascii=False)),
    ("REQUEST_INPUT", "请补充故障时间，当前日志无法确定对应时段。"),
])
def test_only_answer_field_is_shown_even_when_it_contains_a_control_example(system, action, answer):
    before = original_rows(system)
    receipt = submit(system, text="请解释这个配置，并给出 JSON 示例。")
    value = {"protocol": PROTOCOL, "request_key": receipt.followup_id, "action": action, "answer_markdown": answer}
    execute_raw(system, json.dumps(value, ensure_ascii=False))
    system.service.continue_diagnosis = lambda *args: pytest.fail("an answer cannot start diagnosis")
    assert system.service.worker.run_once()
    result = view(system).items[0]
    assert result.status == "COMPLETED" and result.diagnosis_run_id is None
    assert result.answer_markdown.endswith(answer) and PROTOCOL not in result.answer_markdown
    assert len(system.backend.calls) == 1 and original_rows(system) == before


@pytest.mark.parametrize("wrap", [lambda value: value, lambda value: "```json\n" + value + "\n```",
    lambda value: "我将继续诊断：\n" + value])
def test_unframed_control_objects_cannot_execute_or_leak_into_the_answer(system, wrap):
    submit(system)
    # The previous interface accepted this bare object as executable control.
    legacy = {key: value for key, value in DIAGNOSIS.items() if key != "attachment_ids"}
    legacy["log_mode"] = "APPEND"
    execute_raw(system, wrap(json.dumps(legacy, ensure_ascii=False)))
    handoffs = []
    system.service.continue_diagnosis = lambda *args: handoffs.append(args)
    assert not system.service.worker.run_once()
    result = view(system).items[0]
    assert result.status == "FAILED" and result.answer_markdown is None
    assert result.diagnosis_run_id is None and handoffs == []
    assert len(system.backend.calls) == 1


def test_a_valid_result_for_another_question_cannot_start_diagnosis(system):
    submit(system)
    execute_raw(system, json.dumps({"protocol": PROTOCOL,
        "request_key": "00000000-0000-0000-0000-000000000001", **DIAGNOSIS}))
    handoffs = []
    system.service.continue_diagnosis = lambda *args: handoffs.append(args)
    assert not system.service.worker.run_once()
    assert view(system).items[0].status == "FAILED" and handoffs == []
    assert len(system.backend.calls) == 1
