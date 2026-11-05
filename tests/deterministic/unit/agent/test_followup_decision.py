"""Each question starts Generic diagnosis without an evidence-sufficiency model."""
import json

import pytest

from problem_locator.followup.context import parse_result
from tests.deterministic.unit.agent.test_report_followup import (
    system, diagnostic_system, submit, view, original_rows, make_logs, OWNER,
)


DECISION = {
    "action": "CONTINUE_DIAGNOSIS", "effective_problem_text": "10:05 出现请求超时，服务端也有异常。",
    "diagnosis_goal": "核对服务端连接池告警是否解释本次超时。",
    "reason": "旧报告只检查了客户端，需诊断服务端的日志与时间关系。", "attachment_ids": [],
}
REQUEST_KEY = "00000000-0000-0000-0000-000000000001"


def envelope(value):
    return json.dumps({"protocol": "xiaodao.followup.v1", "request_key": REQUEST_KEY, **value}, ensure_ascii=False)


@pytest.mark.parametrize("answer", ["报告中的 3 秒是请求等待时间。", "还缺故障时段的服务端日志，请在这里补充。"])
def test_every_question_hands_off_to_diagnosis_without_consulting_old_answer(diagnostic_system, answer):
    s = diagnostic_system
    s.backend.answer = answer
    s.engine.prepare_continuation = lambda **kwargs: pytest.fail("no intent classifier")
    receipt = submit(s)
    assert s.service.worker.run_once()
    result = view(s).items[0]
    assert result.status == "COMPLETED" and result.diagnosis_run_id is not None
    assert result.diagnosis_run_id != s.rid
    assert answer not in result.answer_markdown
    assert not s.backend.calls and not s.engine.calls
    assert submit(s) == receipt and not s.service.worker.run_once()
    assert s.store.get_run(s.cid, result.diagnosis_run_id)["force_generic"] is True


def test_unrelated_question_is_the_new_problem_and_report_is_only_background(diagnostic_system):
    s = diagnostic_system
    text = "另一个问题：交换机端口频繁 flap，请分析。"
    submit(s, text=text)
    assert s.service.worker.run_once()
    result = view(s).items[0]
    run = s.store.get_run(s.cid, result.diagnosis_run_id)
    assert run["effective_problem_text"] == text
    assert run["force_generic"] is True
    context = run["continuation_context"]
    assert context["parent_report_markdown"] == s.report.markdown
    assert json.loads(context["followup_context_json"])["original_problem"] == run["source_problem_text"]
    assert not s.backend.calls and not s.engine.calls


def test_cancel_before_dispatch_cannot_create_a_diagnosis_run(diagnostic_system):
    s = diagnostic_system
    before = original_rows(s)
    receipt = submit(s)
    s.service.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=OWNER)
    assert not s.service.worker.run_once()
    assert view(s).items[0].status == "CANCELLED"
    assert original_rows(s) == before and not s.backend.calls


@pytest.mark.parametrize("result", [
    {**DECISION, "effective_problem_text": " "},
    {**DECISION, "action": "RUN_SHELL"},
    {**DECISION, "attachment_ids": ["unknown"]},
    {**DECISION, "new_run_id": "user-provided"},
])
def test_handoff_never_uses_old_classifier_output(diagnostic_system, result):
    s = diagnostic_system
    s.backend.decision = result
    s.backend.execute = lambda **kwargs: pytest.fail("retired classifier must not execute")
    submit(s)
    assert s.service.worker.run_once()
    value = view(s).items[0]
    assert value.status == "COMPLETED" and value.diagnosis_run_id is not None
    assert not s.backend.calls


@pytest.mark.parametrize("value", ['示例：{"action":"CONTINUE_DIAGNOSIS"}',
    '{"timeout":3000}', '{"action":"CONTINUE_DIAGNOSIS",'])
def test_json_example_or_prose_is_not_an_execution_envelope(value):
    with pytest.raises(ValueError):
        parse_result(value, request_key=REQUEST_KEY)


def test_handoff_envelope_can_carry_a_maximum_size_problem_without_truncation():
    problem = "a" * 65536
    result = parse_result(envelope({**DECISION, "effective_problem_text": problem}), request_key=REQUEST_KEY)
    assert result.effective_problem_text == problem
    with pytest.raises(ValueError):
        parse_result(envelope({**DECISION, "effective_problem_text": "问" * 21846}), request_key=REQUEST_KEY)
