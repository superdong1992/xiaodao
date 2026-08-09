#!/bin/sh
set -eu
umask 077

test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001

xiaodao_launcher=/opt/venvs/xiaodao/bin/python
logparse_launcher=/opt/venvs/logparse/bin/python
test -L "$xiaodao_launcher"
test -L "$logparse_launcher"

xiaodao_resolved=$(readlink -f "$xiaodao_launcher")
logparse_resolved=$(readlink -f "$logparse_launcher")
case "$xiaodao_resolved" in
  /opt/uv-python/*) ;;
  *) exit 1 ;;
esac
case "$logparse_resolved" in
  /opt/uv-python/*) ;;
  *) exit 1 ;;
esac
test -f "$xiaodao_resolved"
test -x "$xiaodao_resolved"
test -f "$logparse_resolved"
test -x "$logparse_resolved"

xiaodao_version=$(runuser -u plagent -- "$xiaodao_launcher" -I -c 'import platform; print(platform.python_version())')
logparse_version=$(runuser -u plagent -- "$logparse_launcher" -I -c 'import platform; print(platform.python_version())')
test "$xiaodao_version" = 3.12.13
test "$logparse_version" = 3.12.13

runuser -u plagent -- test -r /opt/src/xiaodao/pyproject.toml
runuser -u plagent -- test -r /opt/src/logparse/config.yaml
runuser -u plagent -- test -r /opt/e2e-skills/diagnose-service-takeover/SKILL.md
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
runuser -u plagent -- test ! -w "$xiaodao_resolved"
runuser -u plagent -- test ! -w "$logparse_resolved"
runuser -u plagent -- test -w /var/lib/problem-locator
runuser -u plagent -- test -r /run/plagent-claude/settings.json
test -z "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"

catalog_receipt_tmp=/tmp/attempt52-nonroot-logparse-catalog.json
catalog_stderr_tmp=/tmp/attempt52-nonroot-logparse-catalog.stderr
catalog_receipt=/evidence/nonroot-logparse-catalog-verification.json
test ! -e "$catalog_receipt_tmp"
test ! -e "$catalog_stderr_tmp"
test ! -e "$catalog_receipt"
runuser -u plagent -- /usr/bin/env -i \
  HOME=/run/plagent-claude \
  USER=plagent \
  LOGNAME=plagent \
  SHELL=/bin/sh \
  LANG=C.UTF-8 \
  PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  /opt/venvs/xiaodao/bin/python -B -I \
  /evidence/verify_nonroot_logparse_catalog.py \
  > "$catalog_receipt_tmp" 2> "$catalog_stderr_tmp"
test ! -s "$catalog_stderr_tmp"
test "$(stat -c '%u:%g:%a' "$catalog_receipt_tmp")" = 0:0:600
test "$(wc -l < "$catalog_receipt_tmp")" -eq 1
test "$(wc -c < "$catalog_receipt_tmp")" -le 4096
grep -Fq '"asset_runtime_build":"PASS"' "$catalog_receipt_tmp"
grep -Fq '"catalog_startup_scan":"PASS"' "$catalog_receipt_tmp"
grep -Fq '"logparse_git_owner":"0:0"' "$catalog_receipt_tmp"
grep -Fq '"logparse_repo_owner":"0:0"' "$catalog_receipt_tmp"
grep -Fq '"logparse_tree_writable_entries":0' "$catalog_receipt_tmp"
grep -Fq '"status":"PASS"' "$catalog_receipt_tmp"
install -m 0600 -o 0 -g 0 "$catalog_receipt_tmp" "$catalog_receipt"
test "$(stat -c '%u:%g:%a' "$catalog_receipt")" = 0:0:600

{
  printf 'uid=10001\n'
  printf 'gid=10001\n'
  printf 'xiaodao_launcher_is_symlink=true\n'
  printf 'xiaodao_launcher_resolved_under_opt_uv_python=true\n'
  printf 'xiaodao_python_version=3.12.13\n'
  printf 'logparse_launcher_is_symlink=true\n'
  printf 'logparse_launcher_resolved_under_opt_uv_python=true\n'
  printf 'logparse_python_version=3.12.13\n'
  printf 'resolved_targets_regular_and_executable=true\n'
  printf 'source_readable=true\n'
  printf 'tree_scan=fail-closed\n'
  printf 'source_venv_and_managed_python_any_user_writable_entry=false\n'
  printf 'resolved_python_targets_user_writable=false\n'
  printf 'claude_executable=true\n'
  printf 'settings_readable=true\n'
  printf 'data_root_empty=true\n'
  printf 'data_root_user_writable=true\n'
  printf 'nonroot_logparse_asset_build=pass\n'
  printf 'nonroot_versioned_asset_catalog=pass\n'
  printf 'logparse_repo_and_git_owner=0:0\n'
  printf 'logparse_tree_user_writable=false\n'
} > /evidence/nonroot-python-launcher-regression.txt
