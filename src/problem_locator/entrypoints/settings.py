"""Immutable S06 startup settings and validation."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .env_file import EnvFileError, merged_environment
from problem_locator.interfaces.session_auth import WebsiteAuthConfig


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


def load_website_auth_configuration(values: Mapping[str, str]) -> WebsiteAuthConfig:
    mode = values.get("WEBSITE_AUTH_MODE", "redis")
    if mode not in {"redis", "trusted_header"}:
        raise SettingsError("WEBSITE_AUTH_MODE 必须是 redis 或 trusted_header")
    try:
        port = int(values.get("WEBSITE_REDIS_PORT", "6379"))
        database = int(values.get("WEBSITE_REDIS_DB", "0"))
    except ValueError:
        raise SettingsError("WEBSITE_REDIS_PORT 和 WEBSITE_REDIS_DB 必须是整数") from None
    return WebsiteAuthConfig(
        mode=mode,
        redis_host=values.get("WEBSITE_REDIS_HOST", "").strip(),
        redis_port=port,
        redis_db=database,
        redis_username=values.get("WEBSITE_REDIS_USERNAME") or None,
        redis_password=values.get("WEBSITE_REDIS_PASSWORD") or None,
        redis_ssl=values.get("WEBSITE_REDIS_SSL", "false").lower() == "true",
        cookie_name=values.get("WEBSITE_SESSION_COOKIE_NAME", "sessionid"),
        owner_namespace=values.get("WEBSITE_OWNER_NAMESPACE", "xiaodao-website"),
    )


def load_database_configuration(values: Mapping[str, str]) -> tuple[str, int]:
    """Validate PostgreSQL configuration without exposing connection credentials."""

    database_url = values.get("DATABASE_URL", "")
    if not database_url:
        raise SettingsError("必须配置 DATABASE_URL，服务不会回退到 SQLite")
    try:
        parsed = urlsplit(database_url)
        port = parsed.port
        valid = (
            database_url == database_url.strip()
            and not any(ord(character) < 32 or ord(character) == 127 for character in database_url)
            and parsed.scheme in {"postgresql", "postgres"}
            and parsed.hostname is not None
            and bool(parsed.path.strip("/"))
            and not parsed.fragment
            and port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError(
            "DATABASE_URL 必须是包含服务器地址和数据库名的 postgresql:// 或 postgres:// 连接地址"
        ) from None

    raw_pool_size = values.get("DATABASE_POOL_SIZE", "8")
    if re.fullmatch(r"[1-9][0-9]?", raw_pool_size) is None or not 2 <= int(raw_pool_size) <= 32:
        raise SettingsError("DATABASE_POOL_SIZE 必须是 2–32 之间的整数")
    return database_url, int(raw_pool_size)


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
    specialized_reviewer_enabled: bool = False
    route_claude_command: str | None = None
    diagnose_claude_command: str | None = None
    intake_claude_command: str | None = None
    route_workers: int = 1
    diagnose_workers: int = 2
    logparse_concurrency: int = 1
    archive_workers: int = 1
    methods_evidence_validation: str = "off"
    generic_logparse_product: str = "default"
    generic_memory_enabled: bool = False
    report_followup_enabled: bool = False
    report_followup_snapshot_bytes: int = 1024 ** 3
    report_followup_storage_bytes: int = 5 * 1024 ** 3
    database_url: str | None = field(default=None, repr=False)
    database_pool_size: int = 8
    website_auth: WebsiteAuthConfig = field(default_factory=WebsiteAuthConfig, repr=False)

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

        database_url, database_pool_size = load_database_configuration(values)
        website_auth = load_website_auth_configuration(values)

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
        route_claude_command = values.get(
            "ROUTE_CLAUDE_COMMAND",
            claude_command,
        )
        diagnose_claude_command = values.get(
            "DIAGNOSE_CLAUDE_COMMAND",
            claude_command,
        )
        intake_claude_command = values.get("INTAKE_CLAUDE_COMMAND", route_claude_command)
        if not bind_host or bind_host.isspace() or not claude_command or claude_command.isspace():
            raise SettingsError("BIND_HOST and CLAUDE_COMMAND must be non-empty")
        role_commands = (
            route_claude_command,
            diagnose_claude_command,
            intake_claude_command,
        )
        if any(not command or command.isspace() for command in role_commands):
            raise SettingsError("Agent role command settings must be non-empty")

        generic_logparse_product = values.get("GENERIC_LOGPARSE_PRODUCT", "default")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", generic_logparse_product) is None:
            raise SettingsError("GENERIC_LOGPARSE_PRODUCT 必须是有效的产品标识")
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

        if "EVIDENCE_V2_REVIEWER_ENABLED" in values:
            raise SettingsError(
                "EVIDENCE_V2_REVIEWER_ENABLED is not supported; use SPECIALIZED_REVIEWER_ENABLED"
            )
        raw_reviewer_enabled = values.get("SPECIALIZED_REVIEWER_ENABLED", "false")
        if raw_reviewer_enabled not in {"true", "false"}:
            raise SettingsError(
                "SPECIALIZED_REVIEWER_ENABLED must be true or false"
            )
        methods_evidence_validation = values.get("METHODS_EVIDENCE_VALIDATION", "off")
        if methods_evidence_validation not in {"off", "advisory", "strict"}:
            raise SettingsError("METHODS_EVIDENCE_VALIDATION 必须是 off、advisory 或 strict")
        raw_memory_enabled = values.get("GENERIC_MEMORY_ENABLED", "false")
        if raw_memory_enabled not in {"true", "false"}:
            raise SettingsError("GENERIC_MEMORY_ENABLED 必须是 true 或 false")
        raw_followup_enabled = values.get("REPORT_FOLLOWUP_ENABLED", "false")
        if raw_followup_enabled not in {"true", "false"}:
            raise SettingsError("REPORT_FOLLOWUP_ENABLED 必须是 true 或 false")
        followup_sizes = {}
        for key, default in (("REPORT_FOLLOWUP_SNAPSHOT_BYTES", 1024 ** 3),
                             ("REPORT_FOLLOWUP_STORAGE_BYTES", 5 * 1024 ** 3)):
            raw = values.get(key, str(default))
            if re.fullmatch(r"[1-9][0-9]{0,18}", raw) is None or int(raw) > 2 ** 63 - 1:
                raise SettingsError(f"{key} 必须是有效的正整数字节数")
            followup_sizes[key.lower()] = int(raw)
        if followup_sizes["report_followup_snapshot_bytes"] > 1024 ** 3:
            raise SettingsError("REPORT_FOLLOWUP_SNAPSHOT_BYTES 不能超过 1 GiB")
        if followup_sizes["report_followup_snapshot_bytes"] > followup_sizes["report_followup_storage_bytes"]:
            raise SettingsError("追问快照总量不能小于单份上限")

        workers = {}
        for key, default in (("ROUTE_WORKERS", 1), ("DIAGNOSE_WORKERS", 2), ("LOGPARSE_CONCURRENCY", 1), ("ARCHIVE_WORKERS", 1)):
            raw = values.get(key, str(default))
            if re.fullmatch(r"[1-9][0-9]*", raw) is None:
                raise SettingsError(f"{key} 必须是正整数")
            workers[key.lower()] = int(raw)

        return cls(
            **workers,
            **followup_sizes,
            data_root=paths["DATA_ROOT"],
            database_url=database_url,
            database_pool_size=database_pool_size,
            website_auth=website_auth,
            public_base_url=base_url.rstrip("/"),
            bind_host=bind_host,
            port=port,
            claude_command=claude_command,
            generic_skill_name=generic_skill_name,
            generic_logparse_product=generic_logparse_product,
            skill_dir=paths["SKILL_DIR"],
            logparse_repo=paths["LOGPARSE_REPO"],
            logparse_config_path=paths["LOGPARSE_CONFIG_PATH"],
            logparse_python=logparse_python,
            dfx_log_level=dfx_log_level,
            dfx_log_dir=dfx_log_dir,
            specialized_reviewer_enabled=(raw_reviewer_enabled == "true" and methods_evidence_validation != "off"),
            methods_evidence_validation=methods_evidence_validation,
            generic_memory_enabled=raw_memory_enabled == "true",
            report_followup_enabled=raw_followup_enabled == "true",
            route_claude_command=route_claude_command,
            diagnose_claude_command=diagnose_claude_command,
            intake_claude_command=intake_claude_command,
        )

    def __repr__(self) -> str:
        return (
            "Settings(data_root=<configured>, public_base_url="
            f"{self.public_base_url!r}, bind_host={self.bind_host!r}, port={self.port}, "
            "database_url=<redacted>, "
            f"database_pool_size={self.database_pool_size}, "
            "claude_command=<configured>, "
            "route_claude_command=<configured>, "
            "diagnose_claude_command=<configured>, "
            "intake_claude_command=<configured>, "
            "skill_dir=<configured>, "
            f"generic_skill_name={self.generic_skill_name!r}, "
            "logparse_repo=<redacted>, logparse_config_path=<redacted>, "
            "logparse_python=<redacted>, "
            f"dfx_log_level={self.dfx_log_level!r}, "
            f"dfx_log_dir={self.dfx_log_dir!r}, "
            "specialized_reviewer_enabled="
            f"{self.specialized_reviewer_enabled!r}, "
            f"methods_evidence_validation={self.methods_evidence_validation!r})"
        )


__all__ = ["Settings", "SettingsError", "load_database_configuration"]
