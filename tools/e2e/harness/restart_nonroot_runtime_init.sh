#!/bin/sh
set -eu

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
if getent passwd plagent >/dev/null; then
  test "$(id -u plagent)" = 10001
  test "$(id -g plagent)" = 10001
  test "$(getent passwd 10001 | cut -d: -f1)" = plagent
  test "$(getent group 10001 | cut -d: -f1)" = plagent
else
  test -z "$(getent passwd 10001)"
  test -z "$(getent group plagent)"
  test -z "$(getent group 10001)"
  groupadd --gid 10001 plagent
  useradd --uid 10001 --gid 10001 --no-create-home \
    --home-dir /run/plagent-claude --shell /bin/sh plagent
fi
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
test "$(stat -c '%u:%g:%a' /run/plagent-claude)" = 10001:10001:700
for directory in /run/plagent-claude/.claude /run/plagent-claude/.claude/skills; do
  test -d "$directory"
  test ! -L "$directory"
  chown 10001:10001 "$directory"
  chmod 0700 "$directory"
  runuser -u plagent -- test -w "$directory"
done

test "$(stat -c '%u:%g' /var/lib/problem-locator)" = 10001:10001
runuser -u plagent -- test -w /var/lib/problem-locator
/opt/venvs/xiaodao/bin/python /evidence/snapshot_data_root.py \
  /tmp/attempt52-data-root-before.json

/opt/venvs/xiaodao/bin/python /evidence/prepare_claude_settings.py \
  > /tmp/attempt52-restart-root-settings.txt
/opt/venvs/xiaodao/bin/python /evidence/prepare_nonroot_settings.py create \
  > /tmp/attempt52-restart-nonroot-settings.txt
chown 10001:10001 /run/plagent-claude/settings.json
/opt/venvs/xiaodao/bin/python /evidence/prepare_nonroot_settings.py verify
helper_skill=/run/plagent-claude/.claude/skills/logparse-diagnose/SKILL.md
test -f "$helper_skill"
test ! -L "$helper_skill"
test "$(sha256sum "$helper_skill" | awk '{print $1}')" = 73b1d84458f0baf86c848f1e3a49aab21eafd2f67a3a6746971e7ae893bad3ad
runuser -u plagent -- test -r "$helper_skill"
test -z "$(runuser -u plagent -- find /run/plagent-claude/.claude/skills/logparse-diagnose -xdev -writable -print -quit)"

/opt/venvs/xiaodao/bin/python /evidence/snapshot_data_root.py \
  /tmp/attempt52-data-root-after.json
cmp /tmp/attempt52-data-root-before.json /tmp/attempt52-data-root-after.json
install -m 0644 /tmp/attempt52-data-root-before.json \
  /evidence/nonroot-restart-data-root-before.json
install -m 0644 /tmp/attempt52-data-root-after.json \
  /evidence/nonroot-restart-data-root-after.json
printf '%s\n' \
  'uid=10001' \
  'gid=10001' \
  'data_root_numeric_owner=10001:10001' \
  'data_root_user_writable=true' \
  'data_root_before_and_after_snapshots_preserved=true' \
  'data_root_content_and_metadata_unchanged=true' \
  'settings_top_level=env-only' \
  'settings_owner=10001:10001' \
  'settings_mode=0600' \
  'claude_runtime_parents=10001:10001,0700,writable' \
  'logparse_diagnose_skill=read-only-bind,hash-verified' \
  > /evidence/nonroot-restart-init.txt
