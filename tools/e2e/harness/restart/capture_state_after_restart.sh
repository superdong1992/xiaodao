#!/bin/sh
set -eu

test -s /evidence/service-stop-verification.txt
test ! -e /tmp/attempt52-validate-state.after.json
test ! -e /tmp/attempt52-validate-state.after.stderr
test ! -e /tmp/attempt52-state-export.after.json
test ! -e /tmp/attempt52-state-export.after.stderr

runuser -u plagent -- /usr/bin/env -i \
  HOME=/run/plagent-claude \
  USER=plagent \
  LOGNAME=plagent \
  LANG=C.UTF-8 \
  PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONPYCACHEPREFIX=/tmp/attempt52-state-admin-pycache \
  /opt/venvs/xiaodao/bin/python -m problem_locator validate-state \
  --data-root /var/lib/problem-locator \
  > /tmp/attempt52-validate-state.after.json \
  2> /tmp/attempt52-validate-state.after.stderr
test ! -s /tmp/attempt52-validate-state.after.stderr

runuser -u plagent -- /usr/bin/env -i \
  HOME=/run/plagent-claude \
  USER=plagent \
  LOGNAME=plagent \
  LANG=C.UTF-8 \
  PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONPYCACHEPREFIX=/tmp/attempt52-state-admin-pycache \
  /opt/venvs/xiaodao/bin/python -m problem_locator export-state \
  --data-root /var/lib/problem-locator \
  --output /tmp/attempt52-state-export.after.json \
  > /tmp/attempt52-state-export.after.stdout \
  2> /tmp/attempt52-state-export.after.stderr
test ! -s /tmp/attempt52-state-export.after.stdout
test ! -s /tmp/attempt52-state-export.after.stderr
test -s /tmp/attempt52-validate-state.after.json
test -s /tmp/attempt52-state-export.after.json
install -m 0644 /tmp/attempt52-validate-state.after.json /evidence/validate-state.after.json
install -m 0644 /tmp/attempt52-state-export.after.json /evidence/state-export.after.json
printf '%s\n' \
  'execution_user=plagent' \
  'environment=env-i' \
  'validate_state_exit=0' \
  'export_state_exit=0' \
  'phase=after-restart' \
  > /evidence/state-admin-after-restart.txt
