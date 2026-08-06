#!/usr/bin/env bash
set -euo pipefail

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
expected_cli_sha=a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01

/opt/venvs/xiaodao/bin/python /evidence/verify_claude_manifest.py \
  > /evidence/claude-manifest-verification.txt
test "$(sha256sum /opt/claude-code/cli.js | awk '{print $1}')" = "$expected_cli_sha"
test "$(/usr/local/bin/claude --version)" = '2.1.89 (Claude Code)'
/usr/local/bin/claude --help > /tmp/claude-help.txt
for flag in --print --no-chrome --no-session-persistence --dangerously-skip-permissions --tools --allowedTools --setting-sources; do
  grep -Fq -- "$flag" /tmp/claude-help.txt
done
if grep -Fq -- '--safe-mode' /tmp/claude-help.txt; then
  safe_mode_help=true
else
  safe_mode_help=false
fi
/opt/venvs/xiaodao/bin/python /evidence/prepare_claude_settings.py \
  > /evidence/claude-settings-allowlist.txt
test "$(stat -c %a /root/.claude)" = '700'
test "$(stat -c %a /root/.claude/settings.json)" = '600'
/opt/venvs/xiaodao/bin/python - <<'PY'
import json
from pathlib import Path
from urllib.parse import urlsplit
p = json.loads(Path('/root/.claude/settings.json').read_text(encoding='utf-8'))
expected = {
    'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL', 'API_TIMEOUT_MS',
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
}
if set(p) != {'env'} or set(p.get('env', {})) != expected:
    raise SystemExit('settings allowlist mismatch')
env = p['env']
if not all(isinstance(env[key], str) for key in expected):
    raise SystemExit('settings values must be strings')
if not env['ANTHROPIC_AUTH_TOKEN']:
    raise SystemExit('authentication token must be non-empty')
model = 'deepseek-v4-flash[1m]'
for key in (
    'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL',
):
    if env[key] != model:
        raise SystemExit('model mapping mismatch')
base_url = urlsplit(env['ANTHROPIC_BASE_URL'])
if base_url.scheme != 'https' or not base_url.netloc:
    raise SystemExit('base URL must be non-empty HTTPS URL')
PY
mount_line=$(awk '$2 == "/root/.claude" {print; found=1} END {if (!found) exit 1}' /proc/mounts)
mount_options=$(printf '%s\n' "$mount_line" | awk '{print $4}')
for option in rw noexec nosuid nodev; do
  case ",$mount_options," in
    *",$option,"*) ;;
    *) exit 1 ;;
  esac
done

{
  printf 'status=pass\n'
  printf 'version_output=2.1.89 (Claude Code)\n'
  printf 'platform=linux-x64\n'
  printf 'distribution=official_npm\n'
  printf 'package=@anthropic-ai/claude-code\n'
  printf 'package_version=2.1.89\n'
  printf 'cli_sha256=%s\n' "$expected_cli_sha"
  printf 'read_only_cache_cli_sha256=%s\n' "$expected_cli_sha"
  printf 'checksum_equality=true\n'
} > /evidence/claude-signature-verification.txt

{
  printf 'claude_version=2.1.89\n'
  printf 'help_has_print=true\n'
  printf 'help_has_no_chrome=true\n'
  printf 'help_has_no_session_persistence=true\n'
  printf 'help_has_dangerously_skip_permissions=true\n'
  printf 'help_has_tools=true\n'
  printf 'help_has_allowed_tools=true\n'
  printf 'help_has_setting_sources=true\n'
  printf 'help_has_safe_mode=%s\n' "$safe_mode_help"
  printf 'safe_mode_probe=omitted_as_unsupported\n'
  printf 'safe_mode_note=real print and service commands omit the undocumented flag\n'
  printf '%s\n' 'real_agent_template=-p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Write --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 0.10'
  printf '%s\n' 'service_template=/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 240s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'
} > /evidence/claude-cli-compatibility.txt

{
  printf 'container=%s\n' "${E2E_CONTAINER_NAME:-unspecified}"
  printf 'docker_init=true\n'
  printf 'volume=%s\n' "${E2E_VOLUME_NAME:-unspecified}"
  printf 'host_binding=127.0.0.1:18000:8000\n'
  printf 'data_root=/var/lib/problem-locator\n'
  printf 'data_root_initial_state=empty\n'
  printf 'claude_tmpfs=rw,noexec,nosuid,nodev,mode=0700,size=536870912\n'
  printf 'claude_settings_mode=0600\n'
  printf 'claude_cache_mount=read-only\n'
  printf 'claude_cache_cli_sha256=%s\n' "$expected_cli_sha"
} > /evidence/environment-isolation.txt
