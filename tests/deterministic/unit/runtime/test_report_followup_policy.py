"""The extra read/search capability belongs only to report follow-ups."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.claude_command import (ClaudeCommand, ClaudeCommandError,
    apply_final_response_policy, supports_read_search)
from problem_locator.runtime.followup_access import permits_tool, prepare_settings
from problem_locator.runtime import followup_access


def test_followup_policy_pins_read_and_grep_without_changing_existing_read_only_policy(tmp_path):
    invocation = ClaudeCommand(("claude", "--dangerously-skip-permissions", "--tools", "Bash", "--allowedTools", "Bash(*)"),
        {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")})
    result = apply_final_response_policy(invocation, file_access="read-search", workspace_root=tmp_path, phase="REPORT_FOLLOWUP")
    assert "--allowedTools" not in result.argv
    assert result.argv[result.argv.index("--setting-sources") + 1] == ""
    assert result.argv[result.argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    policy = json.loads(Path(result.argv[result.argv.index("--settings") + 1]).read_text())
    assert "permissions" not in policy  # Explicit ask rules override hook approval in Claude 2.1.89.
    assert policy["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"
    assert result.argv[result.argv.index("--tools") + 1] == "Read,Grep"
    assert not any("Bash" in token for token in result.argv)
    assert "--dangerously-skip-permissions" not in result.argv
    assert result.environment["PROBLEM_LOCATOR_AGENT_PHASE"] == "REPORT_FOLLOWUP"
    assert result.environment["PROBLEM_LOCATOR_AGENT_FILE_ACCESS"] == "read-search"
    old = apply_final_response_policy(ClaudeCommand(("claude",), {}), file_access="read-only", workspace_root=tmp_path, phase="METHODS_SPECIALIST")
    assert old.argv[-1] == f"Read({tmp_path.as_posix()}/inputs/**)"
    assert "Read,Grep" not in old.argv


def test_unknown_launchers_cannot_receive_log_inputs_but_the_frozen_gate_wrapper_can(tmp_path):
    assert supports_read_search("claude")
    assert not supports_read_search("custom-agent")
    with pytest.raises(ClaudeCommandError):
        apply_final_response_policy(ClaudeCommand(("custom-agent",), {}),
            file_access="read-search", workspace_root=tmp_path, phase="REPORT_FOLLOWUP")
    args = ("node", str(Path("tools/isolated-agent-wrapper.mjs")), "--workflow", "report-followup")
    result = apply_final_response_policy(ClaudeCommand(args, {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}), file_access="read-search", workspace_root=tmp_path, phase="REPORT_FOLLOWUP")
    assert result.argv == args
    assert Path(result.environment["PROBLEM_LOCATOR_FOLLOWUP_SETTINGS"]).is_file()


def test_followup_settings_keep_provider_but_replace_inherited_permissions_hooks_and_plugins(tmp_path):
    settings = {"env": {"ANTHROPIC_BASE_URL": "https://model.invalid", "CLAUDE_CODE_DISABLE_HOOKS": "1"},
        "model": "test-model", "permissions": {"allow": ["Read", "Grep", "Bash"]},
        "hooks": {"SessionStart": [{"command": "unsafe-command"}]}, "enabledPlugins": {"ambient": True}}
    policy = json.loads(prepare_settings(("claude", "--settings", json.dumps(settings)), {}, tmp_path).read_text())
    assert policy["env"] == {"ANTHROPIC_BASE_URL": "https://model.invalid"}
    assert policy["model"] == "test-model"
    assert set(policy["hooks"]) == {"PreToolUse"}
    assert "permissions" not in policy
    assert "enabledPlugins" not in policy


def test_followup_hook_allows_only_explicit_input_paths_and_denies_links(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    log = inputs / "sample.log"
    log.write_text("unique evidence")
    outside = tmp_path / "outside.log"
    outside.write_text("private")
    read = lambda value: {"tool_name": "Read", "tool_input": {"file_path": value}}
    grep = lambda value: {"tool_name": "Grep", "tool_input": {"path": value, "pattern": "evidence"}}
    assert permits_tool(tmp_path, read("inputs/sample.log"))
    assert permits_tool(tmp_path, read(str(log)))
    assert permits_tool(tmp_path, grep("inputs"))
    assert not permits_tool(tmp_path, read("inputs/../outside.log"))
    assert not permits_tool(tmp_path, read(str(outside)))
    assert not permits_tool(tmp_path, grep(None))
    assert not permits_tool(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "cat inputs/sample.log"}})
    (inputs / "linked.log").hardlink_to(outside)
    assert not permits_tool(tmp_path, read("inputs/linked.log"))
    result = subprocess.run([sys.executable, str(Path(followup_access.__file__)), str(tmp_path)],
        input=json.dumps(read("outside.log")), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux Server hook uses a POSIX shell")
def test_followup_hook_command_blocks_if_the_guard_cannot_start(tmp_path, monkeypatch):
    monkeypatch.setattr(followup_access.sys, "executable", str(tmp_path / "missing-python"))
    policy = json.loads(prepare_settings(("claude", "--settings", "{}"), {}, tmp_path).read_text())
    command = policy["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    failed = subprocess.run(command, shell=True, capture_output=True, check=False)
    assert failed.returncode == 2  # Claude blocks tool execution on hook exit 2.


def test_telemetry_accepts_grep_only_for_read_search_and_rejects_write_tools():
    telemetry = AgentStreamTelemetry()
    telemetry.write(b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call1","name":"Grep","input":{"pattern":"probe"}}]}}\n')
    assert telemetry.permits_file_access("read-search")
    assert not telemetry.permits_file_access("read-only")
    assert not telemetry.permits_file_access("none")
    telemetry.write(b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call2","name":"Write","input":{}}]}}\n')
    assert not telemetry.permits_file_access("read-search")
