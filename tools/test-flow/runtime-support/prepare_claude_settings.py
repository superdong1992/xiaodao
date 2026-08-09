import json
# Runtime support shared by the first-party CrossJob adapters.
import os
from pathlib import Path
from urllib.parse import urlsplit

source = Path("/run/host-claude-settings.json")
target = Path("/root/.claude/settings.json")
allowed_env_keys = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
payload = json.loads(source.read_text(encoding="utf-8-sig"))
source_env = payload.get("env", {})
missing = [key for key in allowed_env_keys if key not in source_env]
if missing:
    raise SystemExit("missing required Claude settings keys: " + ",".join(missing))
if set(source_env) != set(allowed_env_keys):
    raise SystemExit("Claude settings env allowlist mismatch")
if not all(isinstance(source_env[key], str) for key in allowed_env_keys):
    raise SystemExit("Claude settings env values must be strings")
if not source_env["ANTHROPIC_AUTH_TOKEN"]:
    raise SystemExit("Claude authentication token must be non-empty")
expected_model = "deepseek-v4-flash[1m]"
model_keys = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
)
if any(source_env[key] != expected_model for key in model_keys):
    raise SystemExit("Claude model mapping mismatch")
parsed_base_url = urlsplit(source_env["ANTHROPIC_BASE_URL"])
if parsed_base_url.scheme != "https" or not parsed_base_url.netloc:
    raise SystemExit("Claude base URL must be non-empty HTTPS URL")
safe_payload = {"env": {key: source_env[key] for key in allowed_env_keys}}
target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
target.write_text(
    json.dumps(safe_payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(target, 0o600)
print("copied_env_key_count=" + str(len(allowed_env_keys)))
print("copied_env_keys=" + ",".join(allowed_env_keys))
print("top_level_keys=env")
print("copied_optional_keys=none")
print("model_mapping_expected=deepseek-v4-flash[1m]")
print("haiku_mapping_exact=true")
print("opus_mapping_exact=true")
print("sonnet_mapping_exact=true")
print("base_url_nonempty=true")
print("base_url_https=true")
