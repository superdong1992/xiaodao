from __future__ import annotations

import json
from pathlib import Path
import stat


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def ordinary(path: Path, expected_mode: int | None = None) -> None:
    info = path.stat(follow_symlinks=False)
    require(stat.S_ISREG(info.st_mode), "SERVICE_LOG_FILE_TYPE")
    require(not path.is_symlink(), "SERVICE_LOG_SYMLINK")
    if expected_mode is not None:
        require(stat.S_IMODE(info.st_mode) == expected_mode, "SERVICE_LOG_MODE")


def contains_any(path: Path, needles: tuple[bytes, ...]) -> tuple[bool, int]:
    overlap_size = max(len(needle) for needle in needles) - 1
    overlap = b""
    scanned = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return False, scanned
            scanned += len(chunk)
            candidate = overlap + chunk
            if any(needle in candidate for needle in needles):
                return True, scanned
            overlap = candidate[-overlap_size:] if overlap_size else b""


def main() -> None:
    settings_path = Path("/run/plagent-claude/settings.json")
    log_path = Path("/tmp/attempt52-service-supervisor/service.log")
    ordinary(settings_path, 0o600)
    ordinary(log_path, 0o600)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    require(set(settings) == {"env"}, "SERVICE_SETTINGS_TOP_LEVEL")
    token = settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    base_url = settings["env"]["ANTHROPIC_BASE_URL"]
    require(isinstance(token, str) and len(token) >= 16, "SERVICE_TOKEN_VALUE")
    require(isinstance(base_url, str) and base_url, "SERVICE_BASE_URL_VALUE")
    needles = (token.encode("utf-8"), base_url.encode("utf-8"))
    require(all(b"\0" not in needle and b"\n" not in needle and b"\r" not in needle for needle in needles), "SERVICE_NEEDLE_FORMAT")
    found, scanned = contains_any(log_path, needles)
    require(not found, "SERVICE_LOG_SENSITIVE_VALUE")
    payload = {
        "bytes_scanned": scanned,
        "schema_version": 1,
        "sensitive_value_occurrences": 0,
        "sensitive_values_checked": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"],
    }
    output = Path("/evidence/service-log-secret-scan.json")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        stream.write("\n")


try:
    main()
except Exception:
    raise SystemExit("SERVICE_LOG_SECRET_SCAN_FAILED") from None
