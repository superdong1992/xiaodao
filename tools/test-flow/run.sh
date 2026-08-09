#!/bin/sh
set -eu
tool_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec node "$tool_root/run.mjs" "$@"

