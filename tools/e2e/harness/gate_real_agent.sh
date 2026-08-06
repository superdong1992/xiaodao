#!/bin/sh
set -eu

settings=/run/plagent-claude/settings.json
scan_root=/tmp/pytest-attempt41-real-agent
basetemp="$scan_root/work"
junit_tmp="$scan_root/real-agent-junit.xml"
stdout_tmp="$scan_root/pytest.stdout.txt"
stderr_tmp="$scan_root/pytest.stderr.txt"
command='/usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Write --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 0.10'

printf '%s\n' \
  'claude_version=2.1.89' \
  'execution_user=plagent' \
  'execution_uid=10001' \
  'home=/run/plagent-claude' \
  'tools=Write' \
  'safe_mode=omitted_as_unsupported' \
  'max_budget_usd=0.10' \
  'retry_count=0' \
  > /evidence/real-agent-command-template.txt

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
test ! -e "$scan_root"
install -d -m 0700 -o 10001 -g 10001 "$scan_root"
SECRET_SCAN_SETTINGS_PATH="$settings" \
SECRET_SCAN_BASETEMP="$scan_root" \
  /opt/venvs/xiaodao/bin/python \
  /evidence/scan_real_agent_secrets_v2.py pre

cd /opt/src/xiaodao
set +e
runuser -u plagent -- env -i \
  HOME=/run/plagent-claude \
  USER=plagent \
  LOGNAME=plagent \
  SHELL=/bin/sh \
  LANG=C.UTF-8 \
  PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONPYCACHEPREFIX=/tmp/attempt52-plagent-pycache \
  S08_REAL_AGENT_GATE=1 \
  "S08_REAL_AGENT_COMMAND=$command" \
  /opt/venvs/xiaodao/bin/python -m pytest -q \
  tests/e2e/test_real_agent_backend_gate.py::test_real_claude_code_writes_exact_agent_outcome_through_backend \
  -p no:cacheprovider \
  --basetemp="$basetemp" \
  --junitxml="$junit_tmp" \
  >"$stdout_tmp" 2>"$stderr_tmp"
pytest_status=$?
set -e

SECRET_SCAN_SETTINGS_PATH="$settings" \
SECRET_SCAN_BASETEMP="$scan_root" \
  /opt/venvs/xiaodao/bin/python \
  /evidence/scan_real_agent_secrets_v2.py post
install -m 0644 "$stdout_tmp" /evidence/real-agent-pytest.stdout.txt
install -m 0644 "$stderr_tmp" /evidence/real-agent-pytest.stderr.txt
if [ -f "$junit_tmp" ]; then
  install -m 0644 "$junit_tmp" /evidence/06-real-agent.xml
fi
test -f /evidence/06-real-agent.xml
test "$pytest_status" -eq 0
