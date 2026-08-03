from __future__ import annotations

import base64
import csv
import hashlib
from pathlib import Path
import zipfile

base = Path("/tmp/pytest-attempt41-installed").resolve()
roots = sorted(
    {
        path.resolve()
        for path in base.glob(
            "test_clean_installed_distribut*/installed-venv/lib/python3.12/site-packages"
        )
    }
)
if len(roots) != 1 or not roots[0].is_relative_to(base):
    raise SystemExit(f"invalid resolved site-packages roots: {roots!r}")
site_packages = roots[0]
installed = site_packages / "problem_locator/runtime/assets"
source = Path("/opt/src/xiaodao/src/problem_locator/runtime/assets")
wheels = sorted(
    {
        path.resolve()
        for path in base.glob(
            "test_clean_installed_distribut*/wheelhouse/problem_locator-*.whl"
        )
    }
)
if len(wheels) != 1 or not wheels[0].is_relative_to(base):
    raise SystemExit(f"invalid resolved wheels: {wheels!r}")
wheel = wheels[0]

source_files = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
installed_files = sorted(path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file())
if source_files != installed_files or len(source_files) != 24:
    raise SystemExit("runtime asset file inventory mismatch")

with zipfile.ZipFile(wheel) as archive:
    wheel_prefix = "problem_locator/runtime/assets/"
    wheel_files = sorted(
        name[len(wheel_prefix):]
        for name in archive.namelist()
        if name.startswith(wheel_prefix) and not name.endswith("/")
    )
    if wheel_files != source_files:
        raise SystemExit("wheel runtime asset inventory mismatch")
    record_name = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
    record_rows = {
        row[0]: (row[1], row[2])
        for row in csv.reader(archive.read(record_name).decode("utf-8").splitlines())
    }
    lines: list[str] = []
    for relative in source_files:
        source_path = source / relative
        installed_path = installed / relative
        source_bytes = source_path.read_bytes()
        installed_bytes = installed_path.read_bytes()
        wheel_name = wheel_prefix + relative
        wheel_bytes = archive.read(wheel_name)
        if source_bytes != installed_bytes or source_bytes != wheel_bytes:
            raise SystemExit(f"asset bytes differ: {relative}")
        source_nlink = source_path.stat().st_nlink
        installed_nlink = installed_path.stat().st_nlink
        if source_nlink != 1 or installed_nlink != 1:
            raise SystemExit(
                f"asset link count is not one: {relative}: "
                f"source={source_nlink} installed={installed_nlink}"
            )
        digest = hashlib.sha256(wheel_bytes).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        expected_record = ("sha256=" + encoded, str(len(wheel_bytes)))
        if record_rows.get(wheel_name) != expected_record:
            raise SystemExit(f"wheel RECORD mismatch: {relative}")
        lines.append(
            f"{relative}\tsize={len(wheel_bytes)}\tsha256={digest.hex()}\t"
            f"source_nlink={source_nlink}\tinstalled_nlink={installed_nlink}\trecord=pass"
        )

output = Path("/evidence/installed-runtime-assets.txt")
output.write_text(
    "uv_link_mode=copy\n"
    + f"site_packages={site_packages}\n"
    + f"wheel={wheel}\n"
    + "asset_count=24\n"
    + "source_nlink_distribution=24x1\n"
    + "installed_nlink_distribution=24x1\n"
    + "wheel_source_installed_bytes=24xmatch\n"
    + "wheel_record=24xpass\n"
    + "\n".join(lines)
    + "\n",
    encoding="utf-8",
)
