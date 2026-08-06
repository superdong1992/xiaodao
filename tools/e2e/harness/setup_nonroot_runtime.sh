#!/bin/sh
set -eu

test -z "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"
command -v groupadd >/dev/null
command -v useradd >/dev/null
command -v runuser >/dev/null
test -z "$(getent passwd 10001)"
test -z "$(getent group 10001)"
groupadd --gid 10001 plagent
useradd --uid 10001 --gid 10001 --no-create-home \
  --home-dir /run/plagent-claude --shell /bin/sh plagent
test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001

mount_line=$(awk '$2 == "/run/plagent-claude" && $3 == "tmpfs" {print; found=1} END {if (!found) exit 1}' /proc/mounts)
mount_options=$(printf '%s\n' "$mount_line" | awk '{print $4}')
for option in rw noexec nosuid nodev; do
  case ",$mount_options," in
    *",$option,"*) ;;
    *) exit 1 ;;
  esac
done
chown 10001:10001 /run/plagent-claude
chmod 0700 /run/plagent-claude
for directory in /run/plagent-claude/.claude /run/plagent-claude/.claude/skills; do
  test -d "$directory"
  test ! -L "$directory"
  chown 10001:10001 "$directory"
  chmod 0700 "$directory"
  runuser -u plagent -- test -w "$directory"
done
/opt/venvs/xiaodao/bin/python /evidence/prepare_nonroot_settings.py create \
  > /evidence/nonroot-settings-allowlist.txt
chown 10001:10001 /run/plagent-claude/settings.json
/opt/venvs/xiaodao/bin/python /evidence/prepare_nonroot_settings.py verify
test "$(stat -c '%u:%g:%a' /run/plagent-claude)" = 10001:10001:700
test "$(stat -c '%u:%g:%a' /run/plagent-claude/settings.json)" = 10001:10001:600
helper_skill=/run/plagent-claude/.claude/skills/logparse-diagnose/SKILL.md
test -f "$helper_skill"
test ! -L "$helper_skill"
test "$(sha256sum "$helper_skill" | awk '{print $1}')" = e9ec1984c8144c1f09d350fc97fb964659464f29171407deb212e2b20d1503ea
runuser -u plagent -- test -r "$helper_skill"
test -z "$(runuser -u plagent -- find /run/plagent-claude/.claude/skills/logparse-diagnose -xdev -writable -print -quit)"

chown 10001:10001 /var/lib/problem-locator
chmod 0700 /var/lib/problem-locator
test "$(stat -c '%u:%g:%a' /var/lib/problem-locator)" = 10001:10001:700
test -z "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"

runuser -u plagent -- test -r /opt/src/xiaodao/pyproject.toml
runuser -u plagent -- test -r /opt/src/logparse/config.yaml
runuser -u plagent -- test -r /opt/e2e-skills/diagnose-service-takeover/SKILL.md
runuser -u plagent -- test -x /opt/venvs/xiaodao/bin/python
runuser -u plagent -- test -x /opt/venvs/logparse/bin/python
runuser -u plagent -- test -x /usr/local/bin/claude
for tree in \
  /opt/src/xiaodao \
  /opt/src/logparse \
  /opt/src/problem-locator-mcp \
  /opt/e2e-skills \
  /opt/venvs/xiaodao \
  /opt/venvs/logparse \
  /opt/uv-python
do
  writable_entry=$(runuser -u plagent -- find "$tree" -xdev -writable -print -quit)
  test -z "$writable_entry"
done
runuser -u plagent -- test -w /var/lib/problem-locator
runuser -u plagent -- test -r /run/plagent-claude/settings.json

{
  printf 'user=plagent\n'
  printf 'uid=10001\n'
  printf 'gid=10001\n'
  printf 'home=/run/plagent-claude\n'
  printf 'path=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin\n'
  printf 'settings_owner=10001:10001\n'
  printf 'settings_mode=0600\n'
  printf 'settings_top_level=env-only\n'
  printf 'model_mapping_expected=deepseek-v4-flash[1m]\n'
  printf 'haiku_mapping_exact=true\n'
  printf 'opus_mapping_exact=true\n'
  printf 'sonnet_mapping_exact=true\n'
  printf 'base_url_nonempty=true\n'
  printf 'base_url_https=true\n'
  printf 'settings_tmpfs=rw,noexec,nosuid,nodev\n'
  printf 'claude_runtime_parents=10001:10001,0700,writable\n'
  printf 'logparse_diagnose_skill=read-only-bind,sha256:e9ec1984c8144c1f09d350fc97fb964659464f29171407deb212e2b20d1503ea\n'
  printf 'service_tools=Bash,Read,Write,Skill(logparse-diagnose)\n'
  printf 'source_venv_and_managed_python_tree_scan=fail-closed\n'
  printf 'source_venv_and_managed_python_any_user_writable_entry=false\n'
  printf 'data_root_owner=10001:10001\n'
  printf 'data_root_mode=0700\n'
  printf 'data_root_empty=true\n'
} > /evidence/nonroot-runtime-isolation.txt
