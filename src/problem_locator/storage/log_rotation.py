"""Bounded UTF-8 JSONL files shared by service diagnostics and Journey events."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import weakref
from pathlib import Path
from typing import Any


LOG_FILE_MAX_BYTES = 16 * 1024 * 1024
LOG_BACKUP_COUNT = 4
LOG_EVENT_MAX_BYTES = 64 * 1024
_LOCKS_GUARD = threading.Lock()
_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_IDENTITY_FIELDS = frozenset({
    "schema_version", "sequence", "timestamp", "level", "event", "logger",
    "process_id", "thread", "correlation_id", "request_id", "conversation_id",
    "run_id", "case_id", "job_id", "job_type", "outcome_id", "diagnostic_id",
    "duration_ms", "backend_invocation_id", "operation", "stage", "phase",
    "status", "code", "error_code", "tool", "ok", "retryable",
})


def _path_lock(path: Path):
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _clip(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "…[truncated]"
    if isinstance(value, dict):
        if depth >= 4:
            return {"truncated_object": True, "key_count": len(value)}
        return {key[:128]: _clip(item, depth + 1)
                for key, item in list(value.items())[:16]}
    if isinstance(value, list):
        if depth >= 4:
            return {"truncated_array": True, "item_count": len(value)}
        return [_clip(item, depth + 1) for item in value[:16]]
    return value


def bounded_json_line(line: str, *, max_bytes: int) -> bytes:
    """Keep valid JSON and event identity when a single record exceeds its budget."""
    raw = (line.rstrip("\n") + "\n").encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    original = json.loads(raw)
    if not isinstance(original, dict):
        raise ValueError("JSONL log records must be objects")
    marker = {"truncated": True, "reason": "EVENT_SIZE_LIMIT",
              "original_utf8_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    journey = original.get("schema_version") == 1 and "sequence" in original and "data" in original

    def mark(payload: dict[str, Any]) -> bytes:
        if journey:
            payload.setdefault("data", {})["log_truncation"] = marker
        else:
            payload["log_truncation"] = marker
        return _encode(payload)

    clipped = {key: _clip(value) for key, value in original.items()}
    encoded = mark(clipped)
    if len(encoded) <= max_bytes:
        return encoded
    minimal = {key: (value[:128] if isinstance(value, str) else value)
               for key, value in original.items()
               if key in _IDENTITY_FIELDS and not isinstance(value, (dict, list))}
    if journey:
        minimal["data"] = {}
    encoded = mark(minimal)
    if len(encoded) > max_bytes:
        raise ValueError("log record identity exceeds the configured byte limit")
    return encoded


def jsonl_segment_paths(path: Path, *, backup_count: int | None = None) -> tuple[Path, ...]:
    """Return retained segments from oldest to newest, including the active file."""
    count = LOG_BACKUP_COUNT if backup_count is None else backup_count
    return tuple(candidate for candidate in (
        *(path.with_name(f"{path.name}.{index}") for index in range(count, 0, -1)), path,
    ) if candidate.is_file())


def read_jsonl_segments(path: Path) -> tuple[tuple[Path, bytes], ...]:
    """Reject a changing rotation snapshot instead of silently omitting a segment."""
    with _path_lock(path):
        paths = jsonl_segment_paths(path)
        if not paths:
            raise FileNotFoundError(path)
        before = [(item, item.stat()) for item in paths]
        content = tuple((item, item.read_bytes()) for item in paths)
        if jsonl_segment_paths(path) != paths:
            raise OSError("日志在读取期间发生轮转，请重试。")
        for item, metadata in before:
            after = item.stat()
            if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise OSError("日志在读取期间发生变化，请重试。")
        return content


class BoundedJsonlFile:
    """Rotate exact bytes with one active file and a fixed number of backups."""

    def __init__(self, path: Path | str, *, max_bytes: int | None = None,
                 backup_count: int | None = None) -> None:
        self.path = Path(path)
        self.max_bytes = LOG_FILE_MAX_BYTES if max_bytes is None else max_bytes
        self.backup_count = LOG_BACKUP_COUNT if backup_count is None else backup_count
        if self.max_bytes < 1024 or self.backup_count < 1:
            raise ValueError("JSONL limits require at least 1024 bytes and one backup")
        self._lock = _path_lock(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for segment in jsonl_segment_paths(self.path, backup_count=self.backup_count):
                original_size = segment.stat().st_size
                if original_size > self.max_bytes:
                    # Adopt old unbounded logs without keeping oversized backups.
                    # Only complete trailing lines survive; readers report a lost prefix.
                    with segment.open("rb") as source:
                        source.seek(-self.max_bytes - 1, os.SEEK_END)
                        tail = source.read(self.max_bytes + 1)
                    start = tail.find(b"\n") + 1
                    end = tail.rfind(b"\n") + 1
                    retained = tail[start:end] if start and end >= start else b""
                    if not retained:
                        # A giant legacy event may contain no complete line in
                        # the bounded tail. Preserve explicit loss evidence;
                        # Journey must not invent a new sequence from this marker.
                        retained = _encode({
                            "event": "logs.history.truncated",
                            "log_truncation": {
                                "truncated": True,
                                "reason": "LEGACY_LOG_SIZE_LIMIT",
                                "original_utf8_bytes": original_size,
                                "retained_complete_records": 0,
                                "sequence_unavailable": True,
                            },
                        })
                    with segment.open("wb") as target:
                        target.write(retained)
            # Configuration historically creates the log even before an event
            # is emitted. Do not keep an open handle between writes/rotations.
            with self.path.open("ab"):
                pass

    def last_record(self) -> dict[str, Any] | None:
        with self._lock:
            for segment in reversed(jsonl_segment_paths(self.path, backup_count=self.backup_count)):
                raw = segment.read_bytes()
                if not raw:
                    continue
                if not raw.endswith(b"\n"):
                    raise ValueError("日志末尾不完整，不能继续追加。")
                value = json.loads(raw.rstrip(b"\n").rsplit(b"\n", 1)[-1])
                if not isinstance(value, dict):
                    raise ValueError("日志末行不是 JSON 对象。")
                return value
        return None

    def write_line(self, line: str) -> str:
        raw = bounded_json_line(line, max_bytes=min(self.max_bytes, LOG_EVENT_MAX_BYTES))
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size and size + len(raw) > self.max_bytes:
                for index in range(self.backup_count, 0, -1):
                    destination = self.path.with_name(f"{self.path.name}.{index}")
                    source = (self.path if index == 1 else
                              self.path.with_name(f"{self.path.name}.{index - 1}"))
                    if source.exists():
                        os.replace(source, destination)
            with self.path.open("ab") as target:
                target.write(raw)
                target.flush()
        return raw.decode("utf-8").rstrip("\n")


class JsonlFileHandler(logging.Handler):
    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self.baseFilename = os.path.abspath(os.fspath(path))
        self.file = BoundedJsonlFile(self.baseFilename)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.file.write_line(self.format(record))
        except Exception:
            self.handleError(record)
