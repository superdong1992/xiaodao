#!/bin/sh
set -eu

export PYTHONPYCACHEPREFIX=/tmp/attempt52-scanner-harness-pycache
/opt/venvs/xiaodao/bin/python -I \
  /evidence/scan_real_agent_secrets_v2.py harness \
  > /evidence/secret-scanner-v2-harness.json
