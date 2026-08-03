#!/bin/sh
set -eu

/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py terminate

probe=0
while [ "$probe" -lt 100 ]; do
  if [ -e /evidence/service-exit-status.txt ]; then
    break
  fi
  probe=$((probe + 1))
  sleep 0.1
done
test -e /evidence/service-exit-status.txt
/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py stop
