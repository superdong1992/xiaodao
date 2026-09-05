"""Process-owned asset bytes; deployment files are read only during startup."""
from __future__ import annotations

import threading
import json
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_assets: dict[str, tuple[Any, Any]] = {}
_files: dict[str, bytes] = {}
_manifests: dict[str, dict[str, Any]] = {}


def register_snapshot(root: Path, ref: Any, specialized: Any = None) -> None:
    files = {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    with _lock:
        _assets[str(root)] = (ref, specialized)
        _files.update(files)
        _manifests.update({name: json.loads(content) for name, content in files.items()
            if Path(name).name == 'asset.json'})


def snapshot_asset(root: Path) -> tuple[Any, Any] | None:
    return _assets.get(str(root))


def snapshot_bytes(path: Path) -> bytes:
    value = _files.get(str(path))
    return path.read_bytes() if value is None else value


def contains_snapshot_file(path: Path) -> bool:
    return str(path) in _files


def snapshot_manifest(path: Path) -> dict[str, Any] | None:
    return _manifests.get(str(path))


def release_snapshots(parent: str) -> None:
    prefix = str(Path(parent)) + str(Path('/'))
    with _lock:
        for mapping in (_assets, _files, _manifests):
            for key in tuple(mapping):
                if key.startswith(prefix):
                    del mapping[key]
