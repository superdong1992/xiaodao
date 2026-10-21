"""Structured terminals preserve result authority and filesystem restrictions."""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from problem_locator.contracts import ErrorCode, default_resource_limits
from problem_locator.runtime.agent_backend import AgentBackend
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.failures import RuntimeExecutionError
from tests.deterministic.unit.runtime.test_agent_backend import _limits, _Signal, _sinks, _workspace


ANSWER = {"protocol": "xiaodao.followup.v1", "request_key": "00000000-0000-0000-0000-000000000001",
    "action": "ANSWER", "answer_markdown": '结构化回答。\n保留"引号"和路径 C:\\logs。'}


def terminal(**changes):
    return {"type": "result", "subtype": "success", "is_error": False,
        "structured_output": ANSWER, **changes}


def tool_event(name, *, partial=False, tool_id="tool-1"):
    block = {"type": "tool_use", "id": tool_id, "name": name, "input": ANSWER}
    return ({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
        "content_block": block}} if partial else {"type": "assistant", "message": {"content": [block]}})


def stream(events, *, ending=b"\n", **options):
    telemetry = AgentStreamTelemetry(**options)
    raw = b"\n".join(json.dumps(event, ensure_ascii=False).encode("utf-8") for event in events) + ending
    for offset in range(0, len(raw), 7):
        telemetry.write(raw[offset:offset + 7])
    return telemetry


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
@pytest.mark.parametrize("legacy_result", [None, "", "已按指定格式生成结果。", '{"action":"CONTINUE_DIAGNOSIS"}'])
def test_structured_terminal_is_authoritative_and_content_stays_out_of_telemetry(ending, legacy_result):
    fields = {} if legacy_result is None else {"result": legacy_result}
    telemetry = stream([terminal(**fields)], ending=ending)
    assert json.loads(telemetry.final_result) == ANSWER
    assert telemetry.permits_file_access("none")
    summary = json.dumps(telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS"), ensure_ascii=False)
    assert "answer_markdown" not in summary and "结构化回答" not in summary


@pytest.mark.parametrize("name", ["StructuredOutput", "structuredoutput"])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("policy", ["none", "read-only", "read-search"])
def test_exact_structured_output_tools_have_no_file_access(name, partial, policy):
    telemetry = stream([tool_event(name, partial=partial), terminal()])
    assert telemetry.permits_file_access(policy)
    assert json.loads(telemetry.final_result) == ANSWER
    observed = telemetry.snapshot(diagnosis_mode="GENERIC", backend_status="SUCCESS")["tool_observations"]
    assert observed[0]["name"] == name and observed[0]["call_count"] == 1


@pytest.mark.parametrize("name", ["StructuredOutput", "structuredoutput"])
def test_structured_tool_keeps_legacy_text_terminal_compatible(name):
    text = json.dumps(ANSWER, ensure_ascii=False)
    event = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    telemetry = stream([tool_event(name), event])
    assert telemetry.permits_file_access("none")
    assert telemetry.final_result == text


@pytest.mark.parametrize("name", ["Structuredoutput", "STRUCTUREDOUTPUT", "structured_output",
    "mcp__StructuredOutput", "StructuredOutput.execute", "Bash", "Write"])
def test_structured_terminal_cannot_hide_other_tools_or_similar_names(name):
    telemetry = stream([tool_event("StructuredOutput"), tool_event(name, tool_id="tool-2"), terminal()])
    assert all(not telemetry.permits_file_access(policy) for policy in ("none", "read-only", "read-search"))


@pytest.mark.parametrize("name,allowed", [("Read", (False, True, True)), ("Grep", (False, False, True))])
def test_structured_terminal_keeps_read_and_grep_policy_boundaries(name, allowed):
    telemetry = stream([tool_event("StructuredOutput"), tool_event(name, tool_id="tool-2"), terminal()])
    assert tuple(telemetry.permits_file_access(policy) for policy in ("none", "read-only", "read-search")) == allowed


@pytest.mark.parametrize("value", [None, [], "{}", True, 1, {"bad": float("nan")},
    {"bad": float("inf")}, {"bad": float("-inf")}], ids=["null", "array", "string", "bool", "int", "nan", "inf", "negative-inf"])
def test_invalid_structured_container_cannot_fall_back_to_valid_legacy_result(value):
    telemetry = stream([terminal(structured_output=value, result=json.dumps(ANSWER))])
    assert telemetry.final_result is None


@pytest.mark.parametrize("value", [{"bad": "\ud800"}, {"bad": "\udfff"},
    {"nested": ["valid", {"bad": "\ud800"}]}], ids=["high-surrogate", "low-surrogate", "nested-surrogate"])
def test_invalid_unicode_structured_object_cannot_fall_back_to_legacy_result(value):
    telemetry = AgentStreamTelemetry()
    telemetry.write(json.dumps(terminal(structured_output=value, result=json.dumps(ANSWER)),
        ensure_ascii=True).encode("ascii") + b"\n")
    assert telemetry.final_result is None


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_valid_unicode_and_backslash_semantics_are_preserved(ensure_ascii):
    body = '中文🧭，路径 C:\\new，字面 \\n，实际换行\n引号"内容"。'
    expected = {**ANSWER, "answer_markdown": body}
    telemetry = AgentStreamTelemetry()
    raw = json.dumps(terminal(structured_output=expected), ensure_ascii=ensure_ascii).encode("utf-8") + b"\n"
    for offset in range(0, len(raw), 3):
        telemetry.write(raw[offset:offset + 3])
    assert json.loads(telemetry.final_result) == expected
    assert telemetry.final_result.encode("utf-8")


@pytest.mark.parametrize("changes", [{"subtype": "error"}, {"is_error": True}, {"is_error": "false"}])
def test_error_terminal_cannot_publish_structured_output(changes):
    assert stream([terminal(**changes)]).final_result is None


@pytest.mark.parametrize("first", [terminal(), terminal(subtype="error", is_error=True)])
def test_second_terminal_cannot_publish_structured_output(first):
    assert stream([first, terminal()]).final_result is None


@pytest.mark.parametrize("raw", [
    b'{"type":"result","type":"result","subtype":"success","is_error":false,"structured_output":{}}\n',
    b'{"type":"result","subtype":"success","is_error":false,"structured_output":{"action":"ANSWER","action":"REQUEST_INPUT"}}\n',
    b'{"type":"assistant"\n',
    b'{"type":"assistant","message":{"content":"\xed\xa0\x80"}}\n',
])
def test_malformed_outer_stream_cannot_be_rescued_by_a_later_structured_terminal(raw):
    telemetry = AgentStreamTelemetry()
    telemetry.write(raw)
    telemetry.write(json.dumps(terminal()).encode() + b"\n")
    assert telemetry.final_result is None
    assert not telemetry.permits_file_access("none")


@pytest.mark.parametrize("partial", [False, True])
def test_tool_input_is_not_a_final_result(partial):
    telemetry = stream([tool_event("StructuredOutput", partial=partial)])
    assert telemetry.final_result is None


def test_structured_terminal_obeys_output_budget_and_sampling_does_not_hide_tools():
    events = [tool_event("StructuredOutput"), terminal()]
    telemetry = stream(events, line_limit_bytes=16)
    assert json.loads(telemetry.final_result) == ANSWER
    assert telemetry.permits_file_access("none")
    limited = stream(events, output_limit_bytes=32)
    assert limited.output_limit_exceeded and limited.final_result is None
    assert not limited.permits_file_access("none")


def test_structured_object_is_not_validated_as_a_followup_contract_by_transport():
    value = {"future_protocol": {"list": [1, True, None]}}
    assert json.loads(stream([terminal(structured_output=value)]).final_result) == value


def replay_backend(tmp_path, events, *, returncode=0, output_limit=16384):
    payload = b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events)
    script = tmp_path / "structured_agent.py"
    script.write_text("import sys\nsys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write({payload!r})\nsys.stdout.buffer.flush()\nraise SystemExit({returncode})\n",
        encoding="utf-8")
    backend = AgentBackend(shlex.join([sys.executable, str(script)]), parent_environment={})
    return backend.execute(prompt="deterministic stream replay", workspace_root=_workspace(tmp_path),
        cancellation=_Signal(), log_sinks=_sinks()[0], resource_limits=default_resource_limits("DIAGNOSE"),
        test_limits=_limits(wall_time=5, output=output_limit), backend_phase="REPORT_FOLLOWUP", file_access="none")


@pytest.mark.parametrize("name", ["StructuredOutput", "structuredoutput"])
def test_real_backend_returns_structured_terminal_instead_of_tool_input_or_description(tmp_path, name):
    tool = tool_event(name)
    tool["message"]["content"][0]["input"] = {"untrusted_partial": "must not be returned"}
    result = replay_backend(tmp_path, [tool, terminal(result="结构化结果已生成。")])
    assert json.loads(result.final_result) == ANSWER


@pytest.mark.parametrize("case,code", [
    ("nonzero", ErrorCode.BACKEND_EXIT_FAILED),
    ("forbidden-tool", ErrorCode.OUTCOME_INVALID),
    ("output-limit", ErrorCode.BACKEND_OUTPUT_LIMIT),
    ("malformed", ErrorCode.OUTCOME_INVALID),
])
def test_real_backend_keeps_process_tool_output_and_stream_failures(tmp_path, case, code):
    events = [tool_event("StructuredOutput"), terminal()]
    if case == "forbidden-tool":
        events.insert(1, tool_event("Bash", tool_id="forbidden"))
    elif case == "malformed":
        events.insert(1, {"type": "assistant", "message": {"content": {"type": "tool_use"}}})
    with pytest.raises(RuntimeExecutionError) as caught:
        replay_backend(tmp_path, events, returncode=17 if case == "nonzero" else 0,
            output_limit=32 if case == "output-limit" else 16384)
    assert caught.value.failure.code is code
