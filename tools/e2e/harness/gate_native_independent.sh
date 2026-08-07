#!/bin/sh
set -eu

export HOME=/run/plagent-claude
export PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin
export PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
export S08_NATIVE_STARTUP_GATE=linux
export SKILL_DIR=/opt/e2e-skills
export LOGPARSE_REPO=/opt/src/logparse
export LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml
export LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python
export CLAUDE_COMMAND='/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 600s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'

cd /opt/src/xiaodao
exec /opt/venvs/xiaodao/bin/python -m pytest -q \
  tests/e2e/test_native_startup_gate.py::test_native_linux_startup_gate \
  --basetemp=/tmp/pytest-attempt41-native \
  --junitxml=/evidence/04-native-linux.xml
