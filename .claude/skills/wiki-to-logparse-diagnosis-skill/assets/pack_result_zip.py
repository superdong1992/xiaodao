#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import stat
import sys
import zipfile


FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def _ordinary_file(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


def pack_result_zip(work_dir: str | Path, output_zip: str | Path) -> Path:
    work_dir = Path(work_dir).resolve()
    output_zip = Path(output_zip).resolve()
    if not work_dir.is_dir() or work_dir.is_symlink():
        raise ValueError("work_dir must be one ordinary directory")
    if output_zip.exists():
        raise ValueError("output_zip must not already exist")
    if output_zip.parent == work_dir or work_dir in output_zip.parents:
        raise ValueError("output_zip must be outside work_dir")

    entries: list[Path] = []
    for path in work_dir.iterdir():
        if path.name in {".", "..", "manifest.txt", "result.zip"}:
            raise ValueError(f"unsupported result entry: {path.name}")
        if not _ordinary_file(path):
            raise ValueError(f"result.zip accepts ordinary flat files only: {path.name}")
        if Path(path.name).name != path.name or "/" in path.name or "\\" in path.name:
            raise ValueError(f"result entry name is not flat: {path.name}")
        entries.append(path)

    report = work_dir / "result.txt"
    if report not in entries or report.stat().st_size == 0:
        raise ValueError("result.txt must exist and be non-empty")
    logs = [item for item in entries if item != report]
    if not logs:
        raise ValueError("result.zip must contain at least one used log")
    if any(item.suffix != ".log" for item in logs):
        raise ValueError("result.zip accepts only result.txt and used .log files")
    ordered = [report, *sorted(logs, key=lambda item: item.name)]

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in ordered:
            info = zipfile.ZipInfo(path.name)
            info.date_time = FIXED_DATE_TIME
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, path.read_bytes())
    return output_zip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args(argv)
    try:
        output = pack_result_zip(args.work_dir, args.output_zip)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"pack_result_zip: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
