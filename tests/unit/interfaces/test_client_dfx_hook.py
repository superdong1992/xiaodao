from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (
    ROOT
    / ".claude"
    / "skills"
    / "problem-locator-client"
    / "scripts"
    / "problem-locator-client-dfx.ps1"
)
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "client_dfx"
    / "v1.0.1-problem-spec-string.jsonl"
)
LEGACY_REPORTED_FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "client_dfx"
    / "v1.0.2-user-reported-legacy-pretooluse.json"
)
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
OFFICIAL_FULL_TOOL = "mcp__problem-locator__problem_locator_create_case"
LEGACY_FULL_TOOL = "problem_locator_problem_locator_create_case"
FULL_TOOLS = (OFFICIAL_FULL_TOOL, LEGACY_FULL_TOOL)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="the client DFX hook is Windows-only",
)


def _payload(
    event: str,
    *,
    problem_spec: object,
    tool_name: str = OFFICIAL_FULL_TOOL,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "session-1",
        "permission_mode": "dontAsk",
        "hook_event_name": event,
        "tool_name": tool_name,
        "tool_use_id": "toolu_01",
        "tool_input": {
            "request_id": "10000000-0000-0000-0000-000000000001",
            "problem_spec": problem_spec,
            "initial_user_facts": [],
            "wait_seconds": 0,
        },
    }
    if event == "PostToolUse":
        payload["tool_response"] = {
            "structuredContent": {"ok": False, "error": {"code": "CONFLICT"}}
        }
        payload["duration_ms"] = 12
    elif event == "PostToolUseFailure":
        payload["error"] = "connection reset"
        payload["is_interrupt"] = False
        payload["duration_ms"] = 13
    return payload


def _run_hook(
    payload: object,
    *,
    project: Path,
    log_file: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    environment = dict(os.environ)
    environment["CLAUDE_PROJECT_DIR"] = str(project)
    if log_file is None:
        environment.pop("PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE", None)
    else:
        environment["PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE"] = os.fspath(log_file)
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(SCRIPT),
        ],
        input=raw,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=environment,
        timeout=20,
    )


def _events(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.mark.parametrize("full_tool", FULL_TOOLS, ids=("official", "legacy"))
def test_hook_preserves_object_and_string_types_from_regression_fixture(
    tmp_path: Path,
    full_tool: str,
) -> None:
    old_event = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert old_event["argument_json_types"]["problem_spec"] == "string"

    nested = {
        "statement": "Unicode：连接失败",
        "goals": ["定位根因"],
        "nested": {"enabled": True, "value": None},
    }
    result = _run_hook(
        _payload("PreToolUse", problem_spec=nested, tool_name=full_tool),
        project=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    event = _events(tmp_path / ".problem-locator" / "client-dfx.jsonl")[0]
    assert event["event"] == "client.hook.tool.started"
    assert event["hook_version"] == "1.0.4"
    assert event["tool_name"] == full_tool
    assert event["logical_tool"] == "problem_locator_create_case"
    assert event["operation_id"] == "10000000-0000-0000-0000-000000000001"
    assert event["argument_json_types"] == {
        "request_id": "string",
        "problem_spec": "object",
        "initial_user_facts": "array",
        "wait_seconds": "number",
    }
    assert event["arguments"]["problem_spec"] == nested


def test_hook_normalizes_user_reported_legacy_name_without_coercing_string(
    tmp_path: Path,
) -> None:
    payload = json.loads(LEGACY_REPORTED_FIXTURE.read_text(encoding="utf-8"))
    result = _run_hook(payload, project=tmp_path)

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    event = _events(tmp_path / ".problem-locator" / "client-dfx.jsonl")[0]
    assert event["tool_name"] == LEGACY_FULL_TOOL
    assert event["logical_tool"] == "problem_locator_create_case"
    assert event["argument_json_types"]["problem_spec"] == "string"
    assert isinstance(event["arguments"]["problem_spec"], str)


@pytest.mark.parametrize("full_tool", FULL_TOOLS, ids=("official", "legacy"))
def test_hook_records_returned_and_failed_without_claiming_success(
    tmp_path: Path,
    full_tool: str,
) -> None:
    log_file = tmp_path / "client.jsonl"
    returned = _run_hook(
        _payload(
            "PostToolUse",
            problem_spec={"statement": "x"},
            tool_name=full_tool,
        ),
        project=tmp_path,
        log_file=log_file,
    )
    failed = _run_hook(
        _payload(
            "PostToolUseFailure",
            problem_spec={"statement": "x"},
            tool_name=full_tool,
        ),
        project=tmp_path,
        log_file=log_file,
    )

    assert returned.returncode == failed.returncode == 0
    events = _events(log_file)
    assert [event["event"] for event in events] == [
        "client.hook.tool.returned",
        "client.hook.tool.failed",
    ]
    assert all(event["tool_name"] == full_tool for event in events)
    assert all(
        event["logical_tool"] == "problem_locator_create_case" for event in events
    )
    assert events[0]["tool_response"]["structuredContent"]["ok"] is False
    assert events[0]["duration_ms"] == 12
    assert not {
        "permissionDecision",
        "updatedInput",
        "updatedToolOutput",
        "updatedMCPToolOutput",
        "additionalContext",
    }.intersection(events[0])
    assert events[1]["error"] == "connection reset"
    assert events[1]["is_interrupt"] is False
    assert events[1]["duration_ms"] == 13


def test_hook_ignores_malformed_events_and_unlisted_tools(tmp_path: Path) -> None:
    malformed = _run_hook("{not-json", project=tmp_path)
    invalid_names = (
        "mcp__problem-locator__unexpected_tool",
        "problem_locator_unexpected_tool",
        "problem_locator_problem_locator_create_case_extra",
        "mcp__problem_locator__problem_locator_create_case",
    )
    unlisted = [
        _run_hook(
            _payload(
                "PreToolUse",
                problem_spec={"statement": "x"},
                tool_name=name,
            ),
            project=tmp_path,
        )
        for name in invalid_names
    ]

    assert malformed.returncode == 0
    assert all(result.returncode == 0 for result in unlisted)
    assert malformed.stdout == malformed.stderr == ""
    assert all(result.stdout == result.stderr == "" for result in unlisted)
    assert not (tmp_path / ".problem-locator" / "client-dfx.jsonl").exists()


def test_hook_reports_invalid_log_path_without_using_blocking_exit_code(
    tmp_path: Path,
) -> None:
    result = _run_hook(
        _payload("PreToolUse", problem_spec={"statement": "x"}),
        project=tmp_path,
        log_file="relative/client.jsonl",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "must be an absolute path" in result.stderr
    assert result.returncode != 2


def test_hook_reports_unwritable_log_target_without_using_blocking_exit_code(
    tmp_path: Path,
) -> None:
    result = _run_hook(
        _payload("PreToolUse", problem_spec={"statement": "x"}),
        project=tmp_path,
        # An existing directory cannot be opened as the JSONL file.
        log_file=tmp_path,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.count("problem-locator client DFX logging failed:") == 1
    assert result.returncode != 2


def test_hook_serializes_32_parallel_processes_as_complete_json_lines(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "parallel.jsonl"

    def invoke(index: int) -> subprocess.CompletedProcess[str]:
        payload = _payload(
            "PreToolUse",
            problem_spec={"statement": str(index)},
            tool_name=FULL_TOOLS[index % len(FULL_TOOLS)],
        )
        payload["tool_use_id"] = f"toolu_{index:02d}"
        payload["tool_input"]["request_id"] = (
            f"10000000-0000-0000-0000-{index + 1:012d}"
        )
        return _run_hook(payload, project=tmp_path, log_file=log_file)

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(invoke, range(32)))

    assert all(result.returncode == 0 for result in results)
    events = _events(log_file)
    assert len(events) == 32
    assert {event["tool_use_id"] for event in events} == {
        f"toolu_{index:02d}" for index in range(32)
    }
