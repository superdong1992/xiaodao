from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SKILL_PATH = REPOSITORY_ROOT / ".claude/skills/logparse-diagnose/SKILL.md"


def test_helper_has_closed_server_preprocessing_mode() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    server_mode = text.split("## Invocation modes", 1)[1].split(
        "## Authority and inputs", 1
    )[0]

    assert "`SERVER_PREPROCESS`" in server_mode
    assert "one prewritten request path" in server_mode
    assert "one result path" in server_mode
    assert "do not load or execute another Skill" in server_mode
    assert "do not read or rewrite the prewritten request" in server_mode
    assert "exactly once" in server_mode
    assert "only after\n  this Helper has loaded successfully" in server_mode
    assert "no retry" in server_mode
    assert "direct-Logparse fallback" in server_mode
    assert "do not read the broker result or target log bodies" in server_mode
    assert "The Runtime alone validates the result, freezes target" in server_mode


def test_helper_server_mode_accepts_runtime_prewritten_broker_paths() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    request_contract = text.split("## Request bytes", 1)[1].split(
        "## First parse", 1
    )[0]

    assert "the Runtime has already written the canonical request" in request_contract
    assert "do not read, edit, replace, or recreate it" in request_contract
    assert "--request output/proposals/<proposal_key>/request.json" in text
    assert "--result output/proposals/<proposal_key>/target_logs.json" in text
    assert "Runtime-created job-scoped broker" in text


def test_helper_allows_only_one_unwrapped_bash_client_command() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    client_contract = text.split("## Only allowed client", 1)[1].split(
        "## Request bytes", 1
    )[0]
    normalized = " ".join(client_contract.split())

    assert "exactly one Bash tool call" in normalized
    assert "single unmodified command" in normalized
    assert "shell wrapper such as `sh -c`" in normalized
    for forbidden_form in (
        "command chaining",
        "pipes",
        "redirection",
        "substitutions",
        "environment assignments",
        "alternate executable",
    ):
        assert forbidden_form in normalized
