#!/bin/sh
set -u

status=0
started=$(date +%s)
run_step() {
  if [ "$status" -ne 0 ]; then
    return
  fi
  "$@" || status=$?
}

run_step bash /evidence/setup_sources.sh
run_step bash /evidence/setup_venvs.sh
run_step bash /evidence/setup_fixtures.sh
run_step bash /evidence/setup_claude.sh
run_step sh /evidence/setup_nonroot_runtime.sh

ended=$(date +%s)
{
  printf 'exit_code=%s\n' "$status"
  printf 'elapsed_seconds=%s\n' "$((ended - started))"
} > /evidence/release-setup.status
exit "$status"
