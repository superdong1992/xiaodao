"""Structured terminal objects still pass the complete follow-up contract."""
import json
from types import SimpleNamespace

import pytest

from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from tests.deterministic.unit.agent.test_report_followup import original_rows, submit, system, view
from tests.deterministic.unit.agent.test_followup_result_protocol import PROTOCOL, execute_raw


def structured_terminal(value, *, result="结构化结果已提交。", tool="StructuredOutput"):
    stream = AgentStreamTelemetry()
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "output-1", "name": tool, "input": value}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "output-1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
            "structured_output": value, "result": result},
    ]
    raw = ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()
    for offset in range(0, len(raw), 7):
        stream.write(raw[offset:offset + 7])
    assert stream.permits_file_access("read-search")
    assert stream.permits_file_access("none")
    return stream.final_result


@pytest.mark.parametrize("action", ["ANSWER", "REQUEST_INPUT"])
@pytest.mark.parametrize("tool", ["StructuredOutput", "structuredoutput"])
@pytest.mark.parametrize("text_result", ["", "结构化结果已提交。", '{"action":"CONTINUE_DIAGNOSIS"}'])
def test_structured_answers_preserve_text_and_never_execute_text_result(system, action, tool, text_result):
    before = original_rows(system)
    receipt = submit(system)
    answer = '引用"超时"。\n路径 C:\\logs\\new；字面量 \\n。'
    value = {"protocol": PROTOCOL, "request_key": receipt.followup_id,
        "action": action, "answer_markdown": answer}
    execute_raw(system, structured_terminal(value, result=text_result, tool=tool))
    system.service.continue_diagnosis = lambda *args: pytest.fail("output text cannot start diagnosis")
    assert system.service.worker.run_once()
    item = view(system).items[0]
    assert item.status == "COMPLETED" and item.answer_markdown.endswith(answer)
    assert item.diagnosis_run_id is None and original_rows(system) == before
    assert len(system.backend.calls) == 1
    assert not list((system.backend.calls[0]["workspace_root"] / "runtime").glob("followup-response-*"))


@pytest.mark.parametrize("override", [
    {"request_key": "00000000-0000-0000-0000-000000000099"},
    {"protocol": "unknown"}, {"action": "RUN_SHELL"},
    {"answer_markdown": {"text": "不能直接显示"}}, {"extra": "unexpected"},
    {"answer_markdown": "x" * 65537},
], ids=["wrong-task", "wrong-protocol", "wrong-action", "wrong-type", "extra-field", "oversized"])
def test_structured_answers_cannot_bypass_business_validation(system, override):
    receipt = submit(system)
    value = {"protocol": PROTOCOL, "request_key": receipt.followup_id,
        "action": "ANSWER", "answer_markdown": "正文", **override}
    execute_raw(system, structured_terminal(value))
    system.service.continue_diagnosis = lambda *args: pytest.fail("invalid output cannot start diagnosis")
    assert not system.service.worker.run_once()
    item = view(system).items[0]
    assert item.status == "FAILED" and item.answer_markdown is None and item.diagnosis_run_id is None
    assert not system.service.worker.run_once() and len(system.backend.calls) == 1


def test_structured_feedback_review_uses_existing_evidence_checks_and_budget(system):
    from tests.deterministic.unit.agent.test_feedback_comment_review import (
        analysis_response, feedback_task, review, CARD,
    )
    from tests.deterministic.unit.agent.test_report_followup import Backend

    backend = Backend(analysis_response())
    original_execute = backend.execute
    def execute(**kwargs):
        raw = original_execute(**kwargs).final_result
        return SimpleNamespace(final_result=structured_terminal(json.loads(raw)))
    backend.execute = execute
    card, receipt = review(system, feedback_task(system), backend)
    assert json.loads(card) == CARD and json.loads(receipt)["origin"] == "FEEDBACK_COMMENT"
    assert len(backend.calls) == 1 and backend.calls[0]["resource_limits"].wall_time_seconds == 180
    assert view(system).items == []
