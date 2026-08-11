#!/bin/sh
# Runtime support shared by the first-party CrossJob adapters.
set -eu
umask 077

instance=${1:?service instance required}
journey_policy=${2:-require-events}
case "$instance" in
  *[!a-z0-9-]*|'') exit 64 ;;
esac
case "$journey_policy" in
  require-events) journey_empty_arg= ;;
  allow-empty) journey_empty_arg=--allow-empty ;;
  *) exit 64 ;;
esac

test "$(id -u)" = 0
test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001
test -n "${E2E_RUN_ID:-}"
test -n "${E2E_PUBLIC_BASE_URL:-}"
test -n "${TEST_FLOW_SERVICE_MODEL:-}"
test -n "${TEST_FLOW_SERVICE_MAX_TURNS:-}"
test -n "${TEST_FLOW_SERVICE_MAX_TOTAL_TOKENS:-}"
test -n "${TEST_FLOW_SERVICE_MAX_BUDGET_USD:-}"
test -n "${TEST_FLOW_SERVICE_HARD_TIMEOUT_SECONDS:-}"
case "$TEST_FLOW_SERVICE_MODEL" in *[!a-zA-Z0-9_.\[\]-]*) exit 64 ;; esac
case "$TEST_FLOW_SERVICE_MAX_TURNS" in *[!0-9]*|'') exit 64 ;; esac
case "$TEST_FLOW_SERVICE_MAX_TOTAL_TOKENS" in *[!0-9]*|'') exit 64 ;; esac
case "$TEST_FLOW_SERVICE_MAX_BUDGET_USD" in *[!0-9.]*) exit 64 ;; esac
case "$TEST_FLOW_SERVICE_HARD_TIMEOUT_SECONDS" in *[!0-9]*|'') exit 64 ;; esac

runtime="/tmp/test-flow-service-$instance"
dfx="/tmp/test-flow-dfx-$instance"
parts=/evidence/events/parts
logs=/evidence/logs
journey_events="$parts/service-linux.$instance.journey.ndjson"
diagnostic_events="$parts/service-linux.$instance.diagnostics.ndjson"
journey_raw="$parts/service-linux.$instance.journey.raw"
diagnostic_raw="$parts/service-linux.$instance.diagnostics.raw"
journey_receipt="/evidence/service-$instance-journey-relay.json"
diagnostic_receipt="/evidence/service-$instance-diagnostics-relay.json"
supervisor_receipt="/evidence/service-$instance-supervisor.json"
stop_marker="$runtime/relay.stop"
service_log="$logs/service-$instance.log"
pid_file="$runtime/service.pid"

test ! -e "$runtime"
test ! -e "$dfx"
test ! -e "$supervisor_receipt"
install -d -m 0700 -o 0 -g 0 "$runtime"
mkdir -p "$parts" "$logs"
install -d -m 0700 -o 10001 -g 10001 "$dfx"
test ! -e "$service_log"
: > "$service_log"
chmod 0600 "$service_log"

service_pid=
journey_relay_pid=
diagnostic_relay_pid=
service_status=70

stop_children() {
  if [ -n "$service_pid" ] && kill -0 "$service_pid" 2>/dev/null; then
    kill -TERM "$service_pid" 2>/dev/null || true
    probe=0
    while kill -0 "$service_pid" 2>/dev/null && [ "$probe" -lt 300 ]; do
      probe=$((probe + 1))
      sleep 0.1
    done
    if kill -0 "$service_pid" 2>/dev/null; then kill -KILL "$service_pid" 2>/dev/null || true; fi
  fi
  : > "$stop_marker"
}

on_signal() {
  stop_children
}
trap on_signal HUP INT TERM

/opt/venvs/xiaodao/bin/python -I /test-flow-runtime/relay_service_journey.py \
  --source "$dfx/journey.jsonl" \
  --events "$journey_events" \
  --raw "$journey_raw" \
  --receipt "$journey_receipt" \
  --stop "$stop_marker" \
  --run-id "$E2E_RUN_ID" \
  --producer-id "service-linux-$instance" \
  $journey_empty_arg &
journey_relay_pid=$!
/opt/venvs/xiaodao/bin/python -I /test-flow-runtime/relay_service_journey.py \
  --mode diagnostics \
  --source "$dfx/debug.jsonl" \
  --events "$diagnostic_events" \
  --raw "$diagnostic_raw" \
  --receipt "$diagnostic_receipt" \
  --stop "$stop_marker" \
  --run-id "$E2E_RUN_ID" \
  --producer-id "service-linux-diagnostics-$instance" &
diagnostic_relay_pid=$!

cd /opt/src/xiaodao
service_claude_command="/usr/bin/timeout --foreground --signal=TERM --kill-after=5s ${TEST_FLOW_SERVICE_HARD_TIMEOUT_SECONDS}s /usr/local/bin/claude -p --output-format stream-json --verbose --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-turns $TEST_FLOW_SERVICE_MAX_TURNS --max-budget-usd $TEST_FLOW_SERVICE_MAX_BUDGET_USD"
/usr/bin/setpriv \
  --reuid=10001 --regid=10001 --clear-groups --no-new-privs -- \
  /usr/bin/env -i \
    HOME=/run/plagent-claude \
    USER=plagent \
    LOGNAME=plagent \
    SHELL=/bin/sh \
    LANG=C.UTF-8 \
    PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX="/tmp/test-flow-service-pycache-$instance" \
    DATA_ROOT=/var/lib/problem-locator \
    DFX_LOG_DIR="$dfx" \
    PUBLIC_BASE_URL="$E2E_PUBLIC_BASE_URL" \
    BIND_HOST=0.0.0.0 \
    PORT=8000 \
    SKILL_DIR=/opt/e2e-skills \
    GENERIC_SKILL_NAME=generic-problem-locator-smoke \
    LOGPARSE_REPO=/opt/src/logparse \
    LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml \
    LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python \
    "CLAUDE_COMMAND=$service_claude_command" \
    /opt/venvs/xiaodao/bin/python -I /test-flow-runtime/test_service_launcher.py serve \
    >>"$service_log" 2>&1 &
service_pid=$!
printf '%s\n' "$service_pid" > "$pid_file"
chmod 0600 "$pid_file"

set +e
wait "$service_pid"
service_status=$?
set -e
: > "$stop_marker"
set +e
wait "$journey_relay_pid"
journey_status=$?
wait "$diagnostic_relay_pid"
diagnostic_status=$?
set -e

status=PASS
code=null
if [ "$service_status" -ne 0 ] && [ "$service_status" -ne 143 ]; then status=FAIL; code='"SERVICE_EXIT"'; fi
if [ "$journey_status" -ne 0 ]; then status=FAIL; code='"JOURNEY_RELAY"'; fi
if [ "$diagnostic_status" -ne 0 ]; then status=FAIL; code='"DIAGNOSTIC_RELAY"'; fi
cat > "$supervisor_receipt" <<EOF
{"schema_version":1,"status":"$status","code":$code,"instance":"$instance","service_exit_code":$service_status,"journey_relay_exit_code":$journey_status,"diagnostic_relay_exit_code":$diagnostic_status}
EOF
chmod 0600 "$supervisor_receipt"
test "$status" = PASS
