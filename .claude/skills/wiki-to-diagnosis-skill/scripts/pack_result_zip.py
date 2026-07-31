from __future__ import annotations

import argparse
from pathlib import Path
import sys
import zipfile


FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def pack_result_zip(work_dir: str | Path, output_zip: str | Path) -> Path:
    work_dir = Path(work_dir)
    output_zip = Path(output_zip)
    if not work_dir.is_dir():
        raise ValueError(f"work_dir is not a directory: {work_dir}")
    if not (work_dir / "result.txt").is_file():
        raise ValueError("work_dir must contain result.txt")

    files: list[Path] = []
    for path in sorted(work_dir.iterdir(), key=lambda item: item.name):
        if path.is_dir():
            raise ValueError(f"result.zip must be flat; directory found: {path.name}")
        if path.name == "manifest.txt":
            raise ValueError("result.zip must not contain manifest.txt")
        files.append(path)

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.name)
            info.date_time = FIXED_DATE_TIME
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return output_zip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a deterministic flat result.zip.")
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args(argv)
    try:
        output = pack_result_zip(args.work_dir, args.output_zip)
    except ValueError as exc:
        print(f"pack_result_zip: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
