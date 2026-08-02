#!/bin/sh
set -eu
umask 077

test "$(id -u)" = 0
test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001
test -x /usr/bin/setpriv
test -x /opt/venvs/xiaodao/bin/python
test "$(stat -c '%u:%g:%a' /run/plagent-claude/settings.json)" = 10001:10001:600

runtime=/tmp/attempt52-service-supervisor
log="$runtime/service.log"
pid_file="$runtime/service.pid"
starttime_file="$runtime/service.starttime"
test ! -e "$runtime"
install -d -m 0700 -o 0 -g 0 "$runtime"
: > "$log"
chmod 0600 "$log"
chown 0:0 "$log"

service_started=false
service_reaped=false
service_pid=

cleanup_service() {
  if [ "$service_started" != true ] || [ "$service_reaped" = true ]; then
    return
  fi
  set +e
  kill -TERM "$service_pid" 2>/dev/null
  set -e
  cleanup_probe=0
  while [ "$cleanup_probe" -lt 300 ]; do
    if [ ! -r "/proc/$service_pid/status" ]; then
      set +e
      wait "$service_pid" 2>/dev/null
      set -e
      service_reaped=true
      return
    fi
    set +e
    cleanup_state=$(awk '$1 == "State:" { print $2 }' "/proc/$service_pid/status")
    cleanup_state_status=$?
    set -e
    if [ "$cleanup_state_status" -ne 0 ]; then
      set +e
      wait "$service_pid" 2>/dev/null
      set -e
      service_reaped=true
      return
    fi
    if [ "$cleanup_state" = Z ]; then
      set +e
      wait "$service_pid" 2>/dev/null
      set -e
      service_reaped=true
      return
    fi
    cleanup_probe=$((cleanup_probe + 1))
    sleep 0.1
  done
  set +e
  kill -KILL "$service_pid" 2>/dev/null
  wait "$service_pid" 2>/dev/null
  set -e
  service_reaped=true
}

on_supervisor_exit() {
  supervisor_status="$1"
  trap - EXIT HUP INT TERM
  cleanup_service
  exit "$supervisor_status"
}

on_supervisor_signal() {
  supervisor_status="$1"
  trap - EXIT HUP INT TERM
  cleanup_service
  exit "$supervisor_status"
}

/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py launch

cd /opt/src/xiaodao
service_claude_command='/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 240s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'
/usr/bin/setpriv \
  --reuid=10001 \
  --regid=10001 \
  --clear-groups \
  --no-new-privs \
  -- \
  /usr/bin/env -i \
    HOME=/run/plagent-claude \
    USER=plagent \
    LOGNAME=plagent \
    SHELL=/bin/sh \
    LANG=C.UTF-8 \
    PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONNOUSERSITE=1 \
    PYTHONPYCACHEPREFIX=/tmp/attempt52-service-pycache \
    DATA_ROOT=/var/lib/problem-locator \
    PUBLIC_BASE_URL=http://127.0.0.1:18000 \
    BIND_HOST=0.0.0.0 \
    PORT=8000 \
    SKILL_DIR=/opt/e2e-skills \
    LOGPARSE_REPO=/opt/src/logparse \
    LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml \
    LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python \
    "CLAUDE_COMMAND=$service_claude_command" \
    /opt/venvs/xiaodao/bin/python -m problem_locator serve \
    >>"$log" 2>&1 &
service_pid=$!
service_started=true
trap 'on_supervisor_exit $?' EXIT
trap 'on_supervisor_signal 129' HUP
trap 'on_supervisor_signal 130' INT
trap 'on_supervisor_signal 143' TERM

printf '%s\n' "$service_pid" > "$pid_file"
chmod 0600 "$pid_file"
chown 0:0 "$pid_file"
test "$(stat -c '%u:%g:%a' "$pid_file")" = 0:0:600

probe=0
while [ "$probe" -lt 100 ]; do
  if [ -r "/proc/$service_pid/stat" ]; then
    awk '{print $22}' "/proc/$service_pid/stat" > "$starttime_file"
    break
  fi
  probe=$((probe + 1))
  sleep 0.1
done
test -s "$starttime_file"
chmod 0600 "$starttime_file"
chown 0:0 "$starttime_file"
test "$(stat -c '%u:%g:%a' "$starttime_file")" = 0:0:600

set +e
wait "$service_pid"
service_status=$?
set -e
service_reaped=true
trap - EXIT HUP INT TERM

/opt/venvs/xiaodao/bin/python -I /evidence/scan_service_log_secrets.py
awk -v pid="$service_pid" '
  BEGIN {
    started = "INFO:     Started server process [" pid "]"
    startup = "INFO:     Application startup complete."
    shutting = "INFO:     Shutting down"
    shutdown = "INFO:     Application shutdown complete."
    finished = "INFO:     Finished server process [" pid "]"
  }
  $0 == started { started_count += 1; started_line = NR }
  $0 == startup { startup_count += 1; startup_line = NR }
  $0 == shutting { shutting_count += 1; shutting_line = NR }
  $0 == shutdown { shutdown_count += 1; shutdown_line = NR }
  $0 == finished { finished_count += 1; finished_line = NR }
  END {
    if (started_count != 1 || startup_count != 1 || shutting_count != 1 ||
        shutdown_count != 1 || finished_count != 1) exit 1
    if (!(started_line < startup_line && startup_line < shutting_line &&
          shutting_line < shutdown_line && shutdown_line < finished_line)) exit 1
  }
' "$log"
test "$service_status" -eq 143
/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py archive-log
/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py exit "$service_status"
