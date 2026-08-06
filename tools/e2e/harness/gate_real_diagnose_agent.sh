#!/bin/sh
set -eu

settings=/run/plagent-claude/settings.json
scan_root=/tmp/pytest-attempt52-real-diagnose-agent
basetemp="$scan_root/work"
junit_tmp="$scan_root/real-diagnose-agent-junit.xml"
stdout_tmp="$scan_root/pytest.stdout.txt"
stderr_tmp="$scan_root/pytest.stderr.txt"
command='/usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'

printf '%s\n' \
  'claude_version=2.1.89' \
  'effective_model=deepseek-v4-flash[1m]' \
  'execution_user=plagent' \
  'execution_uid=10001' \
  'home=/run/plagent-claude' \
  'tools=Bash,Read,Write,Skill(logparse-diagnose)' \
  'safe_mode=omitted_as_unsupported' \
  'max_budget_usd=3.00' \
  'retry_count=0' \
  'skill_source=live-wiki-generated-product' \
  'outcome_source=real-agent-synthesis-from-production-diagnose-context' \
  'agent_execution_count=4' \
  'real_logparse_execution_count=1' \
  'v3_requirement_matrix=rpc-service-takeover,database-deadlock,manual-triage' \
  'requirement_isolation=exact-generated-initial-input-set' \
  'continuation_previous_outcome=not-required-for-first-log-gate' \
  'target_outcome_injection=false' \
  'continuation_expected_result=NEED_INPUT(order_id)' \
  > /evidence/real-diagnose-agent-command-template.txt

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
test ! -e "$scan_root"
install -d -m 0700 -o 10001 -g 10001 "$scan_root"
SECRET_SCAN_SETTINGS_PATH="$settings" \
SECRET_SCAN_BASETEMP="$scan_root" \
SECRET_SCAN_OUTPUT_PREFIX=secret-scan-real-diagnose-agent \
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
  PYTHONPYCACHEPREFIX=/tmp/attempt52-plagent-real-diagnose-pycache \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  S08_REAL_DIAGNOSE_AGENT_V3_MATRIX_GATE=1 \
  S08_REAL_FIRST_LOG_AGENT_GATE=1 \
  "S08_REAL_DIAGNOSE_AGENT_COMMAND=$command" \
  S08_REAL_DIAGNOSE_SKILL_PATH=/opt/e2e-skills/diagnose-service-takeover \
  LOGPARSE_REPO=/opt/src/logparse \
  LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml \
  LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python \
  /opt/venvs/xiaodao/bin/python -m pytest -q \
  tests/e2e/test_real_diagnose_agent_contract_gate.py::test_real_agent_v3_requirement_isolation_gate \
  tests/e2e/test_real_diagnose_agent_contract_gate.py::test_real_first_log_diagnose_agent_produces_valid_continuation \
  -p no:cacheprovider \
  --basetemp="$basetemp" \
  --junitxml="$junit_tmp" \
  >"$stdout_tmp" 2>"$stderr_tmp"
pytest_status=$?
set -e

SECRET_SCAN_SETTINGS_PATH="$settings" \
SECRET_SCAN_BASETEMP="$scan_root" \
SECRET_SCAN_OUTPUT_PREFIX=secret-scan-real-diagnose-agent \
  /opt/venvs/xiaodao/bin/python \
  /evidence/scan_real_agent_secrets_v2.py post
install -m 0644 "$stdout_tmp" /evidence/real-diagnose-agent-pytest.stdout.txt
install -m 0644 "$stderr_tmp" /evidence/real-diagnose-agent-pytest.stderr.txt
if [ -f "$junit_tmp" ]; then
  install -m 0644 "$junit_tmp" /evidence/08-real-diagnose-agent.xml
fi
test -f /evidence/08-real-diagnose-agent.xml
test "$pytest_status" -eq 0
