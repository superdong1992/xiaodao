from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


root = Path("/var/lib/problem-locator").resolve(strict=True)
output = Path(sys.argv[1]) if len(sys.argv) == 2 else None
assert output is not None and output.is_absolute()
digest = hashlib.sha256()
entries = 0
files = 0
directories = 0
for path in [root, *sorted(root.rglob("*"))]:
    assert not path.is_symlink()
    resolved = path.resolve(strict=True)
    assert resolved.is_relative_to(root)
    relative = "." if resolved == root else resolved.relative_to(root).as_posix()
    info = resolved.stat(follow_symlinks=False)
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        kind = "d"
        directories += 1
        content_hash = ""
    else:
        assert stat.S_ISREG(mode)
        kind = "f"
        files += 1
        content_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    entries += 1
    record = (
        f"{kind}\0{relative}\0{stat.S_IMODE(mode):04o}\0{info.st_uid}\0"
        f"{info.st_gid}\0{info.st_size}\0{info.st_mtime_ns}\0{info.st_ctime_ns}\0"
        f"{info.st_nlink}\0{info.st_dev}\0{info.st_ino}\0{content_hash}\n"
    )
    digest.update(record.encode("utf-8"))
payload = {
    "digest_sha256": digest.hexdigest(),
    "directories": directories,
    "entries": entries,
    "files": files,
    "metadata_fields": [
        "kind",
        "relative_path",
        "mode",
        "uid",
        "gid",
        "size",
        "mtime_ns",
        "ctime_ns",
        "nlink",
        "device",
        "inode",
        "content_sha256",
    ],
    "schema_version": 2,
}
with output.open("x", encoding="utf-8", newline="\n") as stream:
    json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    stream.write("\n")
