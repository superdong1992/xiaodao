#!/bin/sh
set -eu

test -s /evidence/service-stop-verification.txt
test ! -e /tmp/attempt52-validate-state.before.json
test ! -e /tmp/attempt52-validate-state.before.stderr
test ! -e /tmp/attempt52-state-export.before.json
test ! -e /tmp/attempt52-state-export.before.stderr

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
  > /tmp/attempt52-validate-state.before.json \
  2> /tmp/attempt52-validate-state.before.stderr
test ! -s /tmp/attempt52-validate-state.before.stderr

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
  --output /tmp/attempt52-state-export.before.json \
  > /tmp/attempt52-state-export.before.stdout \
  2> /tmp/attempt52-state-export.before.stderr
test ! -s /tmp/attempt52-state-export.before.stdout
test ! -s /tmp/attempt52-state-export.before.stderr
test -s /tmp/attempt52-validate-state.before.json
test -s /tmp/attempt52-state-export.before.json
install -m 0644 /tmp/attempt52-validate-state.before.json /evidence/validate-state.before.json
install -m 0644 /tmp/attempt52-state-export.before.json /evidence/state-export.before.json
printf '%s\n' \
  'execution_user=plagent' \
  'environment=env-i' \
  'validate_state_exit=0' \
  'export_state_exit=0' \
  'phase=before-restart' \
  > /evidence/state-admin-before-restart.txt
