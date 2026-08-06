from __future__ import annotations

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
    / "problem-locator-client-compat.ps1"
)
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
OFFICIAL_PREFIX = "mcp__problem-locator__"
LEGACY_PREFIX = "problem_locator_"
CREATE_CASE = "problem_locator_create_case"
SUBMIT_SUPPLEMENT = "problem_locator_submit_supplement"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="the client compatibility Hook is Windows-only",
)


def _payload(tool_name: str, tool_input: object) -> dict[str, object]:
    return {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_use_id": "toolu_01",
        "tool_input": tool_input,
    }


def _run_hook(payload: object) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
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
        timeout=20,
    )


def _updated_input(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0
    assert result.stderr == ""
    output = json.loads(result.stdout)
    assert set(output) == {"hookSpecificOutput"}
    specific = output["hookSpecificOutput"]
    assert set(specific) == {"hookEventName", "updatedInput"}
    assert specific["hookEventName"] == "PreToolUse"
    assert not {
        "permissionDecision",
        "permissionDecisionReason",
        "additionalContext",
        "updatedToolOutput",
    }.intersection(specific)
    return specific["updatedInput"]


@pytest.mark.parametrize("prefix", (OFFICIAL_PREFIX, LEGACY_PREFIX))
def test_create_case_converts_the_whitelisted_object_once(prefix: str) -> None:
    problem = {
        "statement": "连接失败",
        "nested": {"unicode": "你好"},
        "goals": ["定位"],
    }
    original = {
        "request_id": "10000000-0000-0000-0000-000000000001",
        "problem_spec": json.dumps(problem, ensure_ascii=False),
        "initial_user_facts": [],
        "wait_seconds": 0,
    }

    updated = _updated_input(
        _run_hook(_payload(prefix + CREATE_CASE, original))
    )

    assert updated == {**original, "problem_spec": problem}


@pytest.mark.parametrize("prefix", (OFFICIAL_PREFIX, LEGACY_PREFIX))
def test_create_case_converts_fact_array_and_string_members(prefix: str) -> None:
    facts = [
        json.dumps({"name": "主机", "value": "节点一"}, ensure_ascii=False),
        {"name": "region", "value": "cn-north"},
    ]
    original = {
        "request_id": "10000000-0000-0000-0000-000000000001",
        "problem_spec": {"statement": "x"},
        "initial_user_facts": json.dumps(facts, ensure_ascii=False),
    }

    updated = _updated_input(
        _run_hook(_payload(prefix + CREATE_CASE, original))
    )

    assert updated["initial_user_facts"] == [
        {"name": "主机", "value": "节点一"},
        {"name": "region", "value": "cn-north"},
    ]


@pytest.mark.parametrize("prefix", (OFFICIAL_PREFIX, LEGACY_PREFIX))
def test_submit_supplement_converts_inputs_object_once(prefix: str) -> None:
    original = {
        "request_id": "10000000-0000-0000-0000-000000000001",
        "case_id": "case-1",
        "expected_case_revision": 2,
        "inputs": '{"订单号":"order-1"}',
        "attachment_ids": [],
    }

    updated = _updated_input(
        _run_hook(_payload(prefix + SUBMIT_SUPPLEMENT, original))
    )

    assert updated["inputs"] == {"订单号": "order-1"}


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    (
        (
            OFFICIAL_PREFIX + CREATE_CASE,
            {"problem_spec": {"statement": "already object"}, "initial_user_facts": []},
        ),
        (
            LEGACY_PREFIX + SUBMIT_SUPPLEMENT,
            {"inputs": {"name": "already object"}},
        ),
        (OFFICIAL_PREFIX + CREATE_CASE, {"problem_spec": "{invalid"}),
        (OFFICIAL_PREFIX + CREATE_CASE, {"problem_spec": "[]"}),
        (OFFICIAL_PREFIX + CREATE_CASE, {"problem_spec": '"{\\"statement\\":\\"twice\\"}"'}),
        (OFFICIAL_PREFIX + CREATE_CASE, {"initial_user_facts": "{}"}),
        (OFFICIAL_PREFIX + CREATE_CASE, {"initial_user_facts": '"[]"'}),
        (
            OFFICIAL_PREFIX + CREATE_CASE,
            {"initial_user_facts": ['"{\\"name\\":\\"n\\",\\"value\\":\\"v\\"}"']},
        ),
        (OFFICIAL_PREFIX + SUBMIT_SUPPLEMENT, {"inputs": "[]"}),
        (OFFICIAL_PREFIX + SUBMIT_SUPPLEMENT, {"inputs": '"{\\"name\\":\\"twice\\"}"'}),
        ("mcp__problem-locator__problem_locator_create_case_extra", {"problem_spec": "{}"}),
        ("problem_locator_problem_locator_create", {"problem_spec": "{}"}),
        ("mcp__problem_locator__problem_locator_create_case", {"problem_spec": "{}"}),
    ),
)
def test_hook_is_silent_for_correct_invalid_repeated_or_unlisted_input(
    tool_name: str,
    tool_input: object,
) -> None:
    result = _run_hook(_payload(tool_name, tool_input))

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_hook_ignores_non_pretooluse_and_malformed_json() -> None:
    post = _payload(OFFICIAL_PREFIX + CREATE_CASE, {"problem_spec": "{}"})
    post["hook_event_name"] = "PostToolUse"

    results = (_run_hook(post), _run_hook("{not-json"), _run_hook(""))

    assert all(result.returncode == 0 for result in results)
    assert all(result.stdout == result.stderr == "" for result in results)
