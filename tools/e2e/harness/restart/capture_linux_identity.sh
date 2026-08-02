#!/bin/sh
set -eu

python=/opt/venvs/xiaodao/bin/python
output=/evidence/linux-identity.json
test -x "$python"
test ! -e "$output"

"$python" -I - "$output" <<'PY'
import json
import os
import platform
import sys
from pathlib import Path

output = Path(sys.argv[1])
if output.exists() or output.is_symlink():
    raise SystemExit("LINUX_IDENTITY_OUTPUT_EXISTS")

release = platform.freedesktop_os_release()
identity = {
    "schema_version": 1,
    "status": "PASS",
    "id": release.get("ID", ""),
    "version_id": release.get("VERSION_ID", ""),
    "pretty_name": release.get("PRETTY_NAME", ""),
    "uname_machine": platform.machine(),
}
if identity["id"] != "ubuntu":
    raise SystemExit("LINUX_IDENTITY_ID")
if identity["uname_machine"] != "x86_64":
    raise SystemExit("LINUX_IDENTITY_MACHINE")
for key in ("version_id", "pretty_name"):
    value = identity[key]
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise SystemExit(f"LINUX_IDENTITY_{key.upper()}")

payload = (json.dumps(identity, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(output, flags, 0o644)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "wb", closefd=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
finally:
    os.close(fd)

decoded = json.loads(output.read_text(encoding="utf-8"))
if list(decoded) != ["schema_version", "status", "id", "version_id", "pretty_name", "uname_machine"]:
    raise SystemExit("LINUX_IDENTITY_PROPERTIES")
if decoded != identity:
    raise SystemExit("LINUX_IDENTITY_ROUNDTRIP")
PY

test -f "$output"
test ! -L "$output"
test "$(stat -c '%u:%g:%a' "$output")" = 0:0:644
