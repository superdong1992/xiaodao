"""Control output must never be mistaken for user-visible report content."""
import json

import pytest

from tests.deterministic.unit.agent.test_report_followup import system, submit, view, original_rows

PROTOCOL = "xiaodao.followup.v1"
DIAGNOSIS = {"action": "CONTINUE_DIAGNOSIS", "effective_problem_text": "10:05 请求超时。",
    "diagnosis_goal": "检查尚未核实的服务端排队。", "reason": "需要解析服务端日志。", "attachment_ids": []}


def execute_raw(state, value):
    def execute(**kwargs):
        state.backend.calls.append(kwargs)
        return type("Result", (), {"final_result": value})()
    state.backend.execute = execute


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
