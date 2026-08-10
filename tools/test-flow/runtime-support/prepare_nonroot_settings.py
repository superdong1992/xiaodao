from __future__ import annotations

# Runtime support shared by the first-party CrossJob adapters.

import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit


EXPECTED_KEYS = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}
MODEL_KEYS = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
}
EXPECTED_MODEL = "deepseek-v4-flash[1m]"
SOURCE = Path("/root/.claude/settings.json")
TARGET = Path("/run/plagent-claude/settings.json")


def validate(payload: object) -> dict[str, str]:
    assert isinstance(payload, dict) and set(payload) == {"env"}
    environment = payload["env"]
    assert isinstance(environment, dict) and set(environment) == EXPECTED_KEYS
    assert all(isinstance(environment[key], str) for key in EXPECTED_KEYS)
    assert environment["ANTHROPIC_AUTH_TOKEN"]
    assert all(environment[key] == EXPECTED_MODEL for key in MODEL_KEYS)
    parsed = urlsplit(environment["ANTHROPIC_BASE_URL"])
    assert parsed.scheme == "https" and parsed.netloc
    return environment


mode = sys.argv[1] if len(sys.argv) == 2 else ""
assert mode in {"create", "verify"}
if mode == "create":
    assert not SOURCE.is_symlink()
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    environment = validate(source)
    assert not TARGET.exists()
    with TARGET.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            {"env": {key: environment[key] for key in sorted(EXPECTED_KEYS)}},
            stream,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")
    os.chmod(TARGET, 0o600)
    print("top_level_keys=env")
    print("env_key_count=7")
    print("model_mapping_expected=deepseek-v4-flash[1m]")
    print("haiku_mapping_exact=true")
    print("opus_mapping_exact=true")
    print("sonnet_mapping_exact=true")
    print("base_url_nonempty=true")
    print("base_url_https=true")
else:
    assert not TARGET.is_symlink()
    target_stat = TARGET.stat(follow_symlinks=False)
    assert stat.S_ISREG(target_stat.st_mode)
    assert stat.S_IMODE(target_stat.st_mode) == 0o600
    assert target_stat.st_uid == 10001 and target_stat.st_gid == 10001
    validate(json.loads(TARGET.read_text(encoding="utf-8")))
