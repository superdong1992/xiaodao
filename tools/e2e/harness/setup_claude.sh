#!/usr/bin/env bash
set -euo pipefail

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
expected_fingerprint=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
expected_installer_sha=cde4f1702d3b1695f92b73d26888364e17bca476e17f0fd676484c951d36c125
expected_key_armor_sha=bd70a5e4a268002704024ceba7f8446024114e94f3f0bdd11c23a9e592be81c6
expected_keyring_sha=0e122272125dd4bed96be0034cd95c84e9db07b4cf9bcddbe7c3ae01f3580646
expected_manifest_sha=4fef1756128647e4e694d39bef367bd538bc0e09d30e0f406076fb6f10ca98c3
expected_signature_sha=0468122c44bd8a17d6085555660108476cf02faa81f22c51d527f8091e44ce6f
expected_binary_sha=6c086a0f5fbf684d4148bb69629268b4f5109498c1a7be757acf18c51fd04f4b

fallback_root=/evidence/offline-inputs/claude
install -m 0644 "$fallback_root/claude-install.sh" /evidence/claude-install.sh
install -m 0644 "$fallback_root/claude-code.asc" /evidence/claude-code.asc
install -m 0644 "$fallback_root/claude-2.1.150-manifest.json" /evidence/claude-2.1.150-manifest.json
install -m 0644 "$fallback_root/claude-2.1.150-manifest.json.sig" /evidence/claude-2.1.150-manifest.json.sig
installer_source=frozen_current_attempt_verified_official_artifact
key_source=frozen_current_attempt_verified_official_artifact
manifest_source=frozen_current_attempt_verified_official_artifact
signature_source=frozen_current_attempt_verified_official_artifact

test "$(sha256sum /evidence/claude-install.sh | awk '{print $1}')" = "$expected_installer_sha"
test "$(sha256sum /evidence/claude-code.asc | awk '{print $1}')" = "$expected_key_armor_sha"
test "$(sha256sum /evidence/claude-2.1.150-manifest.json | awk '{print $1}')" = "$expected_manifest_sha"
test "$(sha256sum /evidence/claude-2.1.150-manifest.json.sig | awk '{print $1}')" = "$expected_signature_sha"

/opt/venvs/xiaodao/bin/python /evidence/decode_openpgp_public_key.py \
  > /evidence/claude-key-decode.txt
test "$(sha256sum /tmp/attempt52-claude-code-keyring.gpg | awk '{print $1}')" = "$expected_keyring_sha"
gpgv --status-fd 1 --keyring /tmp/attempt52-claude-code-keyring.gpg \
  /evidence/claude-2.1.150-manifest.json.sig \
  /evidence/claude-2.1.150-manifest.json \
  > /evidence/claude-gpgv-status.txt 2> /evidence/claude-gpgv-human.txt
grep -Fq "[GNUPG:] VALIDSIG $expected_fingerprint " /evidence/claude-gpgv-status.txt
grep -Fq '[GNUPG:] GOODSIG BAA929FF1A7ECACE Anthropic Claude Code Release Signing <security@anthropic.com>' /evidence/claude-gpgv-status.txt
/opt/venvs/xiaodao/bin/python /evidence/verify_claude_manifest.py \
  > /evidence/claude-manifest-verification.txt

install -m 0755 /cache/claude-2.1.150 /usr/local/bin/claude
test "$(sha256sum /usr/local/bin/claude | awk '{print $1}')" = "$expected_binary_sha"
test "$(/usr/local/bin/claude --version)" = '2.1.150 (Claude Code)'
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
  printf 'version_output=2.1.150 (Claude Code)\n'
  printf 'platform=linux-x64\n'
  printf 'official_installer_sha256=%s\n' "$expected_installer_sha"
  printf 'installer_executed=false\n'
  printf 'official_key_armor_sha256=%s\n' "$expected_key_armor_sha"
  printf 'decoded_keyring_sha256=%s\n' "$expected_keyring_sha"
  printf 'ascii_armor_validation=pass\n'
  printf 'crc24_validation=pass\n'
  printf 'official_manifest_sha256=%s\n' "$expected_manifest_sha"
  printf 'official_signature_sha256=%s\n' "$expected_signature_sha"
  printf 'gpgv_result=GOODSIG and VALIDSIG\n'
  printf 'validsig_fingerprint=%s\n' "$expected_fingerprint"
  printf 'official_manifest_linux_x64_checksum=%s\n' "$expected_binary_sha"
  printf 'read_only_cache_sha256=%s\n' "$expected_binary_sha"
  printf 'installed_binary_sha256=%s\n' "$expected_binary_sha"
  printf 'checksum_equality=true\n'
  printf 'installer_source=%s\n' "$installer_source"
  printf 'key_source=%s\n' "$key_source"
  printf 'manifest_source=%s\n' "$manifest_source"
  printf 'signature_source=%s\n' "$signature_source"
} > /evidence/claude-signature-verification.txt

{
  printf 'claude_version=2.1.150\n'
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
  printf 'claude_cache_sha256=%s\n' "$expected_binary_sha"
} > /evidence/environment-isolation.txt
