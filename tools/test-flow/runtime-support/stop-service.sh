#!/bin/sh
# Runtime support shared by the first-party CrossJob adapters.
set -eu

instance=${1:?service instance required}
case "$instance" in
  *[!a-z0-9-]*|'') exit 64 ;;
esac
runtime="/tmp/test-flow-service-$instance"
pid_file="$runtime/service.pid"
receipt="/evidence/service-$instance-supervisor.json"
test -s "$pid_file"
pid=$(cat "$pid_file")
case "$pid" in *[!0-9]*|'') exit 65 ;; esac
kill -TERM "$pid"
probe=0
while [ "$probe" -lt 600 ]; do
  if [ -s "$receipt" ]; then
    /opt/venvs/xiaodao/bin/python -I -c 'import json, pathlib, sys; value=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="ascii")); assert value["schema_version"] == 1 and value["status"] == "PASS" and value["code"] is None and value["instance"] == sys.argv[2] and value["service_exit_code"] in (0, 143) and value["journey_relay_exit_code"] == 0 and value["diagnostic_relay_exit_code"] == 0' "$receipt" "$instance"
    exit 0
  fi
  probe=$((probe + 1))
  sleep 0.1
done
exit 66
