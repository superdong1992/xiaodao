#!/bin/bash
set -euo pipefail

export HOME=/root
export PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin
export PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
export S08_INSTALLED_DISTRIBUTION_GATE=1
export S08_UV=/usr/local/bin/uv
export S08_PYTHON_312=/opt/venvs/xiaodao/bin/python
export S08_UV_OFFLINE=0
export UV_LINK_MODE=copy
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
export SKILL_DIR=/opt/e2e-skills
export LOGPARSE_REPO=/opt/src/logparse
export LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml
export LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python
export CLAUDE_COMMAND='/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 240s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'

{
  printf 'cwd=/opt/src/xiaodao\n'
  printf 'pytest=python -m pytest -q tests/e2e/test_installed_distribution_gate.py::test_clean_installed_distribution_import_cli_and_server_gate --basetemp=/tmp/pytest-attempt41-installed --junitxml=/evidence/03-installed-distribution.xml\n'
  printf 'UV_LINK_MODE=copy\n'
  printf 'UV_PYTHON_INSTALL_DIR=/opt/uv-python\n'
  printf 'S08_UV=/usr/local/bin/uv\n'
  printf 'S08_PYTHON_312=/opt/venvs/xiaodao/bin/python\n'
  printf 'PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache\n'
} > /evidence/installed-gate-command-template.txt

cd /opt/src/xiaodao
python -m pytest -q \
  tests/e2e/test_installed_distribution_gate.py::test_clean_installed_distribution_import_cli_and_server_gate \
  --basetemp=/tmp/pytest-attempt41-installed \
  --junitxml=/evidence/03-installed-distribution.xml
python /evidence/verify_installed_assets.py
