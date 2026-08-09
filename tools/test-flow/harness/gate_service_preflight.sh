#!/bin/sh
set -eu

/opt/venvs/xiaodao/bin/python -I /evidence/verify_service_process.py record
/opt/venvs/xiaodao/bin/python -I /evidence/service_preflight.py
