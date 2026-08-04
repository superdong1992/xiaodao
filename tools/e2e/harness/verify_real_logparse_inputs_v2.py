from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


EXPECTED_ARCHIVE_SHA256 = (
    "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064"
)
EXPECTED_ARCHIVE_SIZE = 2367
EXPECTED_PYTHON_VERSION = "Python 3.12.13"

repo = Path("/opt/src/logparse")
config = repo / "config.yaml"
python_launcher = Path("/opt/venvs/logparse/bin/python")
managed_python_root = Path("/opt/uv-python")
cli = repo / "cli.py"
fixture = Path(
    "/opt/src/xiaodao/tests/fixtures/components/logparse/real/"
    "synthetic-rpc-service-takeover.zip.b64"
)
archive = Path("/evidence/synthetic-rpc-service-takeover.zip")
output = Path("/evidence/real-logparse-input-verification.json")

assert repo.is_absolute() and not repo.is_symlink() and repo.is_dir()
for path in (config, cli, fixture, archive):
    assert path.is_absolute()
    assert not path.is_symlink()
    assert stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)

assert python_launcher.is_absolute()
assert python_launcher.parent.resolve(strict=True) == Path(
    "/opt/venvs/logparse/bin"
).resolve(strict=True)
resolved_python = python_launcher.resolve(strict=True)
resolved_managed_root = managed_python_root.resolve(strict=True)
assert resolved_python.is_relative_to(resolved_managed_root)
assert stat.S_ISREG(resolved_python.stat(follow_symlinks=False).st_mode)
assert os.access(resolved_python, os.X_OK)

version_result = subprocess.run(
    [os.fspath(python_launcher), "--version"],
    check=True,
    capture_output=True,
    text=True,
)
python_version = version_result.stdout.strip() or version_result.stderr.strip()
assert python_version == EXPECTED_PYTHON_VERSION

commit = subprocess.run(
    ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
status = subprocess.run(
    ["git", "-C", os.fspath(repo), "status", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
).stdout
assert status == ""

archive_bytes = archive.read_bytes()
fixture_bytes = base64.b64decode(b"".join(fixture.read_bytes().split()), validate=True)
assert archive_bytes == fixture_bytes
archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
assert archive_sha256 == EXPECTED_ARCHIVE_SHA256
assert len(archive_bytes) == EXPECTED_ARCHIVE_SIZE

payload = {
    "archive_matches_source_fixture": True,
    "archive_sha256": archive_sha256,
    "archive_size": len(archive_bytes),
    "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    "logparse_cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
    "logparse_commit": commit,
    "logparse_python_launcher_symlink": python_launcher.is_symlink(),
    "logparse_python_resolved_under_managed_root": True,
    "logparse_python_version": python_version,
    "logparse_tree_clean": True,
    "schema_version": 1,
}
rendered = json.dumps(
    payload,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
) + "\n"
if os.environ.get("VERIFY_REAL_LOGPARSE_NO_WRITE") == "1":
    print(rendered, end="")
else:
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
