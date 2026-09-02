"""Immutable S06 startup settings and validation."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .env_file import EnvFileError, merged_environment


_REQUIRED = (
    "DATA_ROOT",
    "PUBLIC_BASE_URL",
    "SKILL_DIR",
    "LOGPARSE_REPO",
    "LOGPARSE_CONFIG_PATH",
    "GENERIC_SKILL_NAME",
)
_PATH_KEYS = (
    "DATA_ROOT",
    "SKILL_DIR",
    "LOGPARSE_REPO",
    "LOGPARSE_CONFIG_PATH",
)
_FORBIDDEN_LIMIT_KEY = re.compile(r".+_(?:LIMIT|MAX|RETENTION)_.+")
_DFX_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_GENERIC_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class SettingsError(ValueError):
    """Configuration is missing or violates the frozen S06 boundary."""


@dataclass(frozen=True, slots=True)
class Settings:
    data_root: Path
    public_base_url: str
    bind_host: str
    port: int
    claude_command: str
    generic_skill_name: str
    skill_dir: Path
    logparse_repo: Path
    logparse_config_path: Path
    logparse_python: Path
    dfx_log_level: str
    dfx_log_dir: Path | None
    evidence_v2_reviewer_enabled: bool = False

    @classmethod
    def load(
        cls,
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        try:
            values = merged_environment(env_file, environ)
        except EnvFileError as exc:
            raise SettingsError(str(exc)) from exc

        forbidden = sorted(
            key
            for key in values
            if key == "JOB_CONCURRENCY" or _FORBIDDEN_LIMIT_KEY.fullmatch(key)
        )
        if forbidden:
            raise SettingsError("runtime limit overrides are not supported")

        if "DFX_LOG_FILE" in values:
            raise SettingsError(
                "DFX_LOG_FILE is no longer supported; use DFX_LOG_DIR"
            )

        missing = [key for key in _REQUIRED if not values.get(key)]
        if missing:
            raise SettingsError("required configuration is missing")

        paths: dict[str, Path] = {}
        for key in _PATH_KEYS:
            path = Path(values[key])
            if not path.is_absolute():
                raise SettingsError(f"{key} must be an absolute path")
            paths[key] = path

        base_url = values["PUBLIC_BASE_URL"]
        try:
            parsed_url = urlsplit(base_url)
            public_port = parsed_url.port
        except ValueError as exc:
            raise SettingsError("PUBLIC_BASE_URL is malformed") from exc
        if (
            base_url != base_url.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in base_url)
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.hostname is None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or public_port == 0
        ):
            raise SettingsError(
                "PUBLIC_BASE_URL must be an absolute HTTP(S) URL without userinfo, query, or fragment"
            )

        raw_port = values.get("PORT", "8000")
        if re.fullmatch(r"[1-9][0-9]{0,4}", raw_port) is None:
            raise SettingsError("PORT must be a decimal integer from 1 through 65535")
        port = int(raw_port)
        if port > 65_535:
            raise SettingsError("PORT must be a decimal integer from 1 through 65535")

        bind_host = values.get("BIND_HOST", "127.0.0.1")
        claude_command = values.get("CLAUDE_COMMAND", "claude")
        if not bind_host or bind_host.isspace() or not claude_command or claude_command.isspace():
            raise SettingsError("BIND_HOST and CLAUDE_COMMAND must be non-empty")

        generic_skill_name = values["GENERIC_SKILL_NAME"]
        if (
            len(generic_skill_name) > 64
            or _GENERIC_SKILL_NAME.fullmatch(generic_skill_name) is None
        ):
            raise SettingsError(
                "GENERIC_SKILL_NAME must be a standard lowercase hyphen Skill name"
            )

        raw_logparse_python = values.get("LOGPARSE_PYTHON", sys.executable)
        logparse_python = Path(raw_logparse_python)
        if not logparse_python.is_absolute():
            raise SettingsError("LOGPARSE_PYTHON must be an absolute path")

        dfx_log_level = values.get("DFX_LOG_LEVEL", "INFO").upper()
        if dfx_log_level not in _DFX_LOG_LEVELS:
            raise SettingsError(
                "DFX_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )

        raw_dfx_log_dir = values.get("DFX_LOG_DIR")
        dfx_log_dir = Path(raw_dfx_log_dir) if raw_dfx_log_dir else None
        if dfx_log_dir is not None and not dfx_log_dir.is_absolute():
            raise SettingsError("DFX_LOG_DIR must be an absolute path")

        raw_reviewer_enabled = values.get(
            "EVIDENCE_V2_REVIEWER_ENABLED",
            "false",
        )
        if raw_reviewer_enabled not in {"true", "false"}:
            raise SettingsError(
                "EVIDENCE_V2_REVIEWER_ENABLED must be true or false"
            )

        return cls(
            data_root=paths["DATA_ROOT"],
            public_base_url=base_url.rstrip("/"),
            bind_host=bind_host,
            port=port,
            claude_command=claude_command,
            generic_skill_name=generic_skill_name,
            skill_dir=paths["SKILL_DIR"],
            logparse_repo=paths["LOGPARSE_REPO"],
            logparse_config_path=paths["LOGPARSE_CONFIG_PATH"],
            logparse_python=logparse_python,
            dfx_log_level=dfx_log_level,
            dfx_log_dir=dfx_log_dir,
            evidence_v2_reviewer_enabled=raw_reviewer_enabled == "true",
        )

    def __repr__(self) -> str:
        return (
            "Settings(data_root=<configured>, public_base_url="
            f"{self.public_base_url!r}, bind_host={self.bind_host!r}, port={self.port}, "
            "claude_command=<configured>, skill_dir=<configured>, "
            f"generic_skill_name={self.generic_skill_name!r}, "
            "logparse_repo=<redacted>, logparse_config_path=<redacted>, "
            "logparse_python=<redacted>, "
            f"dfx_log_level={self.dfx_log_level!r}, "
            f"dfx_log_dir={self.dfx_log_dir!r}, "
            "evidence_v2_reviewer_enabled="
            f"{self.evidence_v2_reviewer_enabled!r})"
        )


__all__ = ["Settings", "SettingsError"]
