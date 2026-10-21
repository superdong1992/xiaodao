"""The pre-tool guard and command must enforce the invocation's exact policy."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from problem_locator.runtime import followup_access
from problem_locator.runtime.claude_command import ClaudeCommand, apply_final_response_policy


def _hook(workspace, payload, policy="read-search"):
    return subprocess.run([sys.executable, followup_access.__file__, str(workspace), policy],
        input=payload, text=True, capture_output=True, check=False)


@pytest.mark.parametrize("payload", [
    '{"tool_name":"Bash","tool_name":"StructuredOutput","tool_input":{}}',
    '{"tool_name":"StructuredOutput","tool_input":{},"tool_input":{"answer":"other"}}',
    '{"tool_name":"StructuredOutput","tool_input":{"answer":"one","answer":"two"}}',
    '{"tool_name":"StructuredOutput","tool_input":{"answer":NaN}}',
    '{"tool_name":"StructuredOutput","tool_input":{"answer":Infinity}}',
    '{"tool_name":[],"tool_input":{}}',
    '{"tool_name":{},"tool_input":{}}',
    '[]',
    '{"tool_name":"StructuredOutput","tool_input":{}} trailing',
])
def test_hook_returns_deny_for_ambiguous_or_malformed_envelopes(tmp_path, payload):
    result = _hook(tmp_path, payload)
    assert result.returncode == 0
    assert not result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("policy,tool,allowed", [
    ("none", "Read", False), ("none", "Grep", False),
    ("read-only", "Read", True), ("read-only", "Grep", False),
    ("read-search", "Read", True), ("read-search", "Grep", True),
    ("read_only", "Read", False), ("", "StructuredOutput", False),
    ("invalid", "structuredoutput", False),
    ("none", "StructuredOutput", True), ("read-only", "structuredoutput", True),
])
def test_hook_binds_file_capabilities_to_the_invocation(tmp_path, policy, tool, allowed):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "sample.log").write_text("evidence", encoding="utf-8")
    arguments = ({"file_path": "inputs/sample.log"} if tool == "Read" else
        {"path": "inputs", "pattern": "evidence"} if tool == "Grep" else {"answer_markdown": "答复"})
    result = _hook(tmp_path, json.dumps({"tool_name": tool, "tool_input": arguments}), policy)
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == ("allow" if allowed else "deny")


def test_hook_requires_explicit_policy(tmp_path):
    result = subprocess.run([sys.executable, followup_access.__file__, str(tmp_path)],
        input='{"tool_name":"StructuredOutput","tool_input":{}}', text=True, capture_output=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("phase", ["REPORT_FOLLOWUP", "MEMORY_CORRECTION"])
@pytest.mark.parametrize("executable", ["claude", "codeagent"])
@pytest.mark.parametrize("policy", ["none", "read-only", "read-search"])
def test_generated_hook_enforces_policy_and_removes_bare_mode(tmp_path, phase, executable, policy):
    invocation = ClaudeCommand((executable, "--bare", "--settings", "{}"), {})
    result = apply_final_response_policy(invocation, file_access=policy, workspace_root=tmp_path, phase=phase)
    assert "--bare" not in result.argv
    settings = json.loads(Path(result.argv[result.argv.index("--settings") + 1]).read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    tokens = shlex.split(command)
    assert tokens[-3:] == ["||", "exit", "2"]
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"path": "inputs", "pattern": "probe"}})
    (tmp_path / "inputs").mkdir()
    guarded = subprocess.run(tokens[:-3], input=payload, text=True, capture_output=True, check=True)
    decision = json.loads(guarded.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert decision == ("allow" if policy == "read-search" else "deny")
