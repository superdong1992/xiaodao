#!/bin/sh
set -eu

export HOME=/run/plagent-claude
export PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin
export PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
export LOGPARSE_REPO=/opt/src/logparse
export LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml
export LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python

cd /opt/src/xiaodao
/opt/venvs/xiaodao/bin/python /evidence/verify_real_logparse_inputs_v2.py
exec /usr/sbin/runuser -u plagent -- env \
  HOME="$HOME" PATH="$PATH" PYTHONNOUSERSITE="$PYTHONNOUSERSITE" \
  PYTHONPYCACHEPREFIX="$PYTHONPYCACHEPREFIX" \
  LOGPARSE_REPO="$LOGPARSE_REPO" \
  LOGPARSE_CONFIG_PATH="$LOGPARSE_CONFIG_PATH" \
  LOGPARSE_PYTHON="$LOGPARSE_PYTHON" \
  /opt/venvs/xiaodao/bin/python -m pytest -q \
  tests/unit/integrations/test_logparse_real_e2e.py::test_real_parse_then_parameter_b_continuation_parses_once \
  --run-real-logparse \
  -p no:cacheprovider \
  --basetemp=/tmp/pytest-attempt41-real-logparse \
  --junitxml=/evidence/05-real-logparse.xml
