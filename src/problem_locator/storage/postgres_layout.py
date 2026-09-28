"""PostgreSQL DATA_ROOT identity, separate from the legacy SQLite format."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from uuid import UUID

from problem_locator.contracts import CONTRACT_REVISION, SCHEMA_VERSION
from .atomic import (is_reparse_point, read_stable_file_bytes, require_real_directory,
                     require_ordinary_file, write_synced_file)
from .layout import StorageLayout, UnsupportedDataFormatError, _is_empty_fixed_layout


_FORMAT = {
    "format_id": "problem-locator-postgresql-v1",
    "schema_version": 1,
    "state_schema_version": SCHEMA_VERSION,
    "contract_revision": CONTRACT_REVISION,
    "agent_storage_version": 2,
    "storage_backend": "postgresql",
}


def root_identity(layout: StorageLayout) -> str:
    """Bind database ownership to this resource root without storing a DSN."""
    return hashlib.sha256(str(layout.data_root.resolve()).encode("utf-8")).hexdigest()


def marker_bytes(installation_id: str) -> bytes:
    UUID(installation_id)
    return (json.dumps({**_FORMAT, "installation_id": installation_id},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def preflight_postgres_root(layout: StorageLayout) -> dict | None:
    """Reject legacy or mismatched roots without changing their bytes."""
    root = layout.data_root
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        require_real_directory(root.parent)
        return None
    if not stat.S_ISDIR(metadata.st_mode) or is_reparse_point(metadata):
        raise ValueError("DATA_ROOT 必须是普通目录。")
    for name in ("completed.sqlite3", "completed.sqlite3-wal", "completed.sqlite3-shm", "state.json", "state.json.prev"):
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            raise UnsupportedDataFormatError("DATA_ROOT 含有旧数据库，请先显式迁移到新的 PostgreSQL 数据目录。")
    temporary = root / "data-format.json.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise UnsupportedDataFormatError("数据目录包含未完成的格式标记，请保留目录并检查初始化结果。")
    marker = layout.data_format_marker
    try:
        metadata = require_ordinary_file(marker)
    except FileNotFoundError:
        entries = {entry.name for entry in root.iterdir()}
        if entries <= {layout.instance_lock.name}:
            return None
        try:
            empty = _is_empty_fixed_layout(layout, allowed_root_files=frozenset({layout.instance_lock.name}))
        except FileNotFoundError:
            empty = False
        if not empty:
            raise UnsupportedDataFormatError("未标记的数据目录已有内容，请使用新的 DATA_ROOT。")
        return None
    try:
        if metadata.st_nlink != 1 or metadata.st_size > 4096:
            raise ValueError("invalid marker file")
        raw = read_stable_file_bytes(marker)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.keys() != (_FORMAT.keys() | {"installation_id"}):
            raise ValueError("invalid marker keys")
        if any(value[key] != expected for key, expected in _FORMAT.items()):
            raise ValueError("unsupported marker version")
        if raw != marker_bytes(value["installation_id"]):
            raise ValueError("noncanonical marker")
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise UnsupportedDataFormatError("数据目录不是当前 PostgreSQL 格式，请保留原目录并显式迁移。") from exc


def validate_postgres_root(layout: StorageLayout) -> dict:
    marker = preflight_postgres_root(layout)
    if marker is None:
        raise UnsupportedDataFormatError("DATA_ROOT 尚未绑定 PostgreSQL 数据库。")
    return marker


def initialize_postgres_root(layout: StorageLayout, installation_id: str, file_sync) -> None:
    marker = preflight_postgres_root(layout)
    if marker is not None:
        if marker["installation_id"] != installation_id:
            raise UnsupportedDataFormatError("DATA_ROOT 与 PostgreSQL 的 installation_id 不一致。")
        layout.ensure_directories(file_sync)
        return
    if not layout.data_root.exists():
        require_real_directory(layout.data_root.parent)
        layout.data_root.mkdir(mode=0o700)
        file_sync.sync_directory(layout.data_root.parent)
    temporary = layout.data_root / "data-format.json.tmp"
    write_synced_file(temporary, marker_bytes(installation_id), file_sync)
    os.replace(temporary, layout.data_format_marker)
    file_sync.sync_directory(layout.data_root)
    layout.ensure_directories(file_sync)


__all__ = ["preflight_postgres_root", "validate_postgres_root", "initialize_postgres_root",
           "marker_bytes", "root_identity"]
