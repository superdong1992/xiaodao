#!/bin/sh
set -u

group=${1:-}
status=0
started=$(date +%s)

run_gate() {
  if [ "$status" -ne 0 ]; then
    return
  fi
  "$@" || status=$?
}

case "$group" in
  deterministic)
    run_gate sh /evidence/gate_preclean.sh
    run_gate sh /evidence/gate_target.sh
    run_gate sh /evidence/gate_full.sh
    run_gate sh /evidence/gate_post.sh
    run_gate bash /evidence/gate_installed_distribution.sh
    run_gate sh /evidence/gate_native_independent.sh
    run_gate sh /evidence/verify_nonroot_python_launchers.sh
    ;;
  agents)
    run_gate sh /evidence/gate_secret_scanner_harness.sh
    run_gate sh /evidence/gate_real_agent.sh
    run_gate sh /evidence/gate_real_route_agent.sh
    run_gate sh /evidence/gate_real_diagnose_agent.sh
    ;;
  *)
    status=2
    ;;
esac

ended=$(date +%s)
{
  printf 'group=%s\n' "$group"
  printf 'exit_code=%s\n' "$status"
  printf 'elapsed_seconds=%s\n' "$((ended - started))"
} > "/evidence/release-${group}.status"
exit "$status"
