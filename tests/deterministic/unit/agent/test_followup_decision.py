"""The original followup invocation owns the evidence-sufficiency decision."""
import json

import pytest

from problem_locator.followup.context import parse_result
from tests.deterministic.unit.agent.test_report_followup import (
    system, submit, view, original_rows, make_logs, OWNER,
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
def test_answer_and_missing_user_evidence_use_one_invocation_without_a_new_run(system, answer):
    before = original_rows(system)
    system.backend.answer = answer
    submit(system)
    assert system.service.worker.run_once()
    assert len(system.backend.calls) == 1
    assert view(system).items[0].diagnosis_run_id is None
    assert answer in view(system).items[0].answer_markdown
    assert original_rows(system) == before


def test_same_invocation_can_read_logs_then_handoff_without_an_intent_classifier(system):
    make_logs(system)
    assert system.service.observe_report(system.cid, system.rid)
    assert system.service.snapshot_worker.run_once()
    system.backend.decision = DECISION
    handoffs = []
    def handoff(task, decision):
        handoffs.append((task, decision))
        return system.service.store.finish_task(task["followup_id"], answer="正在进一步诊断。")
    system.service.continue_diagnosis = handoff
    submit(system, text="服务端也有异常，这个结论还成立吗？")
    assert system.service.worker.run_once()
    assert len(system.backend.calls) == len(handoffs) == 1
    assert handoffs[0][1] == DECISION
    invocation = system.backend.calls[0]
    assert invocation["file_access"] == "read-search"
    context = json.loads((invocation["workspace_root"] / "inputs/context.json").read_bytes())
    assert context["snapshot_status"] == "READY"
    assert context["question"] == "服务端也有异常，这个结论还成立吗？"
    assert "不要求用户明确说" in invocation["prompt"]


def test_cancel_during_model_execution_cannot_handoff(system):
    system.backend.decision = DECISION
    receipt = submit(system)
    system.backend.action = lambda _: system.service.stop(
        system.cid, system.rid, receipt.followup_id, "stop", owner_key=OWNER)
    system.service.continue_diagnosis = lambda *args: pytest.fail("cancelled question cannot start diagnosis")
    before = original_rows(system)
    assert not system.service.worker.run_once()
    assert view(system).items[0].status == "CANCELLED"
    assert original_rows(system) == before


@pytest.mark.parametrize("result", [
    {**DECISION, "effective_problem_text": " "},
    {**DECISION, "action": "RUN_SHELL"},
    {**DECISION, "attachment_ids": ["unknown"]},
    {**DECISION, "new_run_id": "user-provided"},
])
def test_invalid_model_action_cannot_start_work(system, result):
    system.backend.decision = result
    system.service.continue_diagnosis = lambda *args: pytest.fail("invalid result cannot start diagnosis")
    before = original_rows(system)
    submit(system)
    assert not system.service.worker.run_once()
    assert view(system).items[0].status == "FAILED"
    assert original_rows(system) == before


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
