#!/bin/sh
set -eu

test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001
test "$(stat -c '%u:%g:%a' /run/plagent-claude)" = 10001:10001:700
test "$(stat -c '%u:%g:%a' /run/plagent-claude/settings.json)" = 10001:10001:600
test "$(stat -c '%u:%g' /var/lib/problem-locator)" = 10001:10001
test -n "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"
runuser -u plagent -- test -w /var/lib/problem-locator
cmp \
  /evidence/nonroot-restart-data-root-before.json \
  /evidence/nonroot-restart-data-root-after.json

xiaodao_launcher=/opt/venvs/xiaodao/bin/python
logparse_launcher=/opt/venvs/logparse/bin/python
xiaodao_resolved=$(readlink -f "$xiaodao_launcher")
logparse_resolved=$(readlink -f "$logparse_launcher")
case "$xiaodao_resolved" in /opt/uv-python/*) ;; *) exit 1 ;; esac
case "$logparse_resolved" in /opt/uv-python/*) ;; *) exit 1 ;; esac
test "$(runuser -u plagent -- "$xiaodao_launcher" -I -c 'import platform; print(platform.python_version())')" = 3.12.13
test "$(runuser -u plagent -- "$logparse_launcher" -I -c 'import platform; print(platform.python_version())')" = 3.12.13

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
runuser -u plagent -- test -r /opt/e2e-skills/diagnose-service-takeover/SKILL.md
runuser -u plagent -- test -x /usr/local/bin/claude
runuser -u plagent -- test -r /run/plagent-claude/settings.json

{
  printf 'restart_container=fresh-fixed-ubuntu-image\n'
  printf 'dependency_rebuild=apt,uv,sources,venvs,python-syntax,fixtures,claude\n'
  printf 'runtime_user=plagent\n'
  printf 'runtime_uid=10001\n'
  printf 'runtime_gid=10001\n'
  printf 'data_root_initial_state=nonempty-persistent\n'
  printf 'data_root_numeric_owner=10001:10001\n'
  printf 'data_root_user_writable=true\n'
  printf 'data_root_content_and_metadata_unchanged_during_runtime_init=true\n'
  printf 'empty_data_root_setup_executed=false\n'
  printf 'managed_python_launchers_nonroot_executable=true\n'
  printf 'source_venv_and_managed_python_any_user_writable_entry=false\n'
  printf 'settings_top_level=env-only\n'
} > /evidence/restart-nonempty-runtime-verification.txt
