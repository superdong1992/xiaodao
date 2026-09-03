from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from problem_locator.entrypoints.settings import Settings, SettingsError


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "DATA_ROOT": str(tmp_path / "data"),
        "PUBLIC_BASE_URL": "http://127.0.0.1:8000/service",
        "SKILL_DIR": str(tmp_path / "skills"),
        "GENERIC_SKILL_NAME": "generic-problem-locator-smoke",
        "LOGPARSE_REPO": str(tmp_path / "logparse-secret-repo"),
        "LOGPARSE_CONFIG_PATH": str(tmp_path / "logparse-secret.toml"),
    }


def test_process_environment_overrides_utf8_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "\n".join(
            (
                f"DATA_ROOT={tmp_path / 'file-data'}",
                "PUBLIC_BASE_URL=https://env.example.test/base",
                f"SKILL_DIR={tmp_path / 'skills'}",
                "GENERIC_SKILL_NAME=generic-problem-locator-smoke",
                f"LOGPARSE_REPO={tmp_path / '日志 repo'}",
                f"LOGPARSE_CONFIG_PATH={tmp_path / '配置.toml'}",
                "PORT=9000",
            )
        ),
        encoding="utf-8",
    )
    process = {"DATA_ROOT": str(tmp_path / "process-data"), "PORT": "8123"}

    settings = Settings.load(env_file=env_file, environ=process)

    assert settings.data_root == tmp_path / "process-data"
    assert settings.port == 8123
    assert settings.public_base_url == "https://env.example.test/base"
    assert settings.bind_host == "127.0.0.1"
    assert settings.claude_command == "claude"
    assert settings.route_claude_command == "claude"
    assert settings.diagnose_claude_command == "claude"
    assert settings.generic_skill_name == "generic-problem-locator-smoke"
    assert settings.logparse_python.is_absolute()


def test_settings_are_frozen_and_sensitive_paths_are_redacted(tmp_path: Path) -> None:
    values = environment(tmp_path)
    values["CLAUDE_COMMAND"] = "secret-agent --token hidden"
    values["ROUTE_CLAUDE_COMMAND"] = "secret-route --token hidden-route"
    values["DIAGNOSE_CLAUDE_COMMAND"] = "secret-diagnose --token hidden-diagnose"
    settings = Settings.load(environ=values)

    with pytest.raises(FrozenInstanceError):
        settings.port = 9000  # type: ignore[misc]
    rendered = repr(settings)
    assert values["LOGPARSE_REPO"] not in rendered
    assert values["LOGPARSE_CONFIG_PATH"] not in rendered
    assert values["CLAUDE_COMMAND"] not in rendered
    assert values["ROUTE_CLAUDE_COMMAND"] not in rendered
    assert values["DIAGNOSE_CLAUDE_COMMAND"] not in rendered


def test_all_fixed_configuration_defaults_are_exact(tmp_path: Path) -> None:
    values = environment(tmp_path)
    settings = Settings.load(environ=values)

    assert settings.data_root == Path(values["DATA_ROOT"])
    assert settings.public_base_url == values["PUBLIC_BASE_URL"]
    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.claude_command == "claude"
    assert settings.route_claude_command == settings.claude_command
    assert settings.diagnose_claude_command == settings.claude_command
    assert settings.skill_dir == Path(values["SKILL_DIR"])
    assert settings.logparse_repo == Path(values["LOGPARSE_REPO"])
    assert settings.logparse_config_path == Path(values["LOGPARSE_CONFIG_PATH"])
    assert settings.logparse_python == Path(sys.executable)
    assert settings.dfx_log_level == "INFO"
    assert settings.dfx_log_dir is None
    assert settings.evidence_v2_reviewer_enabled is False


def test_role_agent_commands_override_the_legacy_fallback_independently(
    tmp_path: Path,
) -> None:
    values = environment(tmp_path)
    values.update(
        {
            "CLAUDE_COMMAND": "default-agent",
            "ROUTE_CLAUDE_COMMAND": "fast-route-agent",
            "DIAGNOSE_CLAUDE_COMMAND": "deep-diagnose-agent",
        }
    )

    settings = Settings.load(environ=values)

    assert settings.claude_command == "default-agent"
    assert settings.route_claude_command == "fast-route-agent"
    assert settings.diagnose_claude_command == "deep-diagnose-agent"


def test_legacy_agent_command_remains_the_role_fallback_when_overrides_are_omitted(
    tmp_path: Path,
) -> None:
    values = environment(tmp_path)
    values["CLAUDE_COMMAND"] = "custom-default-agent --flag"

    settings = Settings.load(environ=values)

    assert settings.route_claude_command == values["CLAUDE_COMMAND"]
    assert settings.diagnose_claude_command == values["CLAUDE_COMMAND"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("true", True)],
)
def test_evidence_v2_reviewer_switch_accepts_only_explicit_boolean_text(
    tmp_path: Path,
    value: str,
    expected: bool,
) -> None:
    values = environment(tmp_path)
    values["EVIDENCE_V2_REVIEWER_ENABLED"] = value

    settings = Settings.load(environ=values)

    assert settings.evidence_v2_reviewer_enabled is expected


@pytest.mark.parametrize("value", ["", "TRUE", "False", "1", "yes"])
def test_evidence_v2_reviewer_switch_rejects_noncanonical_values(
    tmp_path: Path,
    value: str,
) -> None:
    values = environment(tmp_path)
    values["EVIDENCE_V2_REVIEWER_ENABLED"] = value

    with pytest.raises(SettingsError, match="EVIDENCE_V2_REVIEWER_ENABLED"):
        Settings.load(environ=values)


def test_dfx_log_dir_accepts_an_absolute_path(tmp_path: Path) -> None:
    values = environment(tmp_path)
    values["DFX_LOG_DIR"] = str(tmp_path / "logs")

    settings = Settings.load(environ=values)

    assert settings.dfx_log_dir == tmp_path / "logs"


@pytest.mark.parametrize("value", ["", "D:/legacy/service.jsonl"])
def test_legacy_dfx_log_file_is_always_rejected(
    tmp_path: Path,
    value: str,
) -> None:
    values = environment(tmp_path)
    values["DFX_LOG_FILE"] = value
    values["DFX_LOG_DIR"] = str(tmp_path / "logs")

    with pytest.raises(SettingsError, match="DFX_LOG_DIR"):
        Settings.load(environ=values)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATA_ROOT", "relative/data"),
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "+8000"),
        ("PUBLIC_BASE_URL", "https://user:secret@example.test"),
        ("PUBLIC_BASE_URL", "ftp://example.test"),
        ("PUBLIC_BASE_URL", "https://example.test/path?secret=yes"),
        ("PUBLIC_BASE_URL", "http://example.test:not-a-port"),
        ("PUBLIC_BASE_URL", "http://[invalid"),
        ("PUBLIC_BASE_URL", "http://example.test:0"),
        ("PUBLIC_BASE_URL", " https://example.test"),
        ("GENERIC_SKILL_NAME", "Generic_Problem_Locator"),
        ("GENERIC_SKILL_NAME", "generic--problem-locator"),
        ("JOB_CONCURRENCY", "2"),
        ("AGENT_MAX_BYTES", "1"),
        ("FILE_RETENTION_SECONDS", "1"),
        ("RPC_LIMIT_BYTES", "1"),
        ("DFX_LOG_LEVEL", "VERBOSE"),
        ("DFX_LOG_DIR", "relative/logs"),
        ("ROUTE_CLAUDE_COMMAND", ""),
        ("DIAGNOSE_CLAUDE_COMMAND", "   "),
    ],
)
def test_invalid_or_fake_runtime_configuration_is_rejected(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    values = environment(tmp_path)
    values[key] = value
    with pytest.raises(SettingsError):
        Settings.load(environ=values)


def test_every_required_setting_is_enforced(tmp_path: Path) -> None:
    for key in (
        "DATA_ROOT",
        "PUBLIC_BASE_URL",
        "SKILL_DIR",
        "GENERIC_SKILL_NAME",
        "LOGPARSE_REPO",
        "LOGPARSE_CONFIG_PATH",
    ):
        values = environment(tmp_path)
        del values[key]
        with pytest.raises(SettingsError):
            Settings.load(environ=values)


def test_explicit_missing_or_malformed_env_file_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    with pytest.raises(SettingsError):
        Settings.load(
            env_file=tmp_path / "missing.env",
            environ=environment(tmp_path),
        )

    malformed = tmp_path / "malformed.env"
    malformed.write_text("BROKEN='unterminated\n", encoding="utf-8")
    with pytest.raises(SettingsError):
        Settings.load(
            env_file=malformed,
            environ=environment(tmp_path),
        )
