"""Bounded, asynchronous copies of the exact logs used by a published report."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path, PurePosixPath

from pydantic import TypeAdapter

from problem_locator.contracts import CancellationReason, OpaqueId
from problem_locator.diagnostics import log_event
from problem_locator.runtime.generic_logs import _stream_verified
from problem_locator.storage.atomic import is_reparse_point, require_real_directory
from problem_locator.storage.paths import ensure_no_symlink_ancestors
from problem_locator.storage.platform import PlatformFileSync

_ID = TypeAdapter(OpaqueId)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_LIMIT = 2_000_000


class InputChanged(ValueError):
    pass


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def ordinary_path(root, relative):
    parts = PurePosixPath(relative)
    if (not isinstance(relative, str) or not relative or parts.is_absolute() or "\\" in relative
            or parts.as_posix() != relative or any(item in {".", ".."} for item in parts.parts)):
        raise InputChanged("invalid relative input path")
    root = Path(root)
    candidate = root.joinpath(*parts.parts)
    ensure_no_symlink_ancestors(root, candidate)
    require_real_directory(root)
    candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    metadata = candidate.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or is_reparse_point(metadata) or metadata.st_nlink != 1:
        raise InputChanged("input is not an ordinary private file")
    return candidate


def read_small(root, relative, limit=_MANIFEST_LIMIT):
    source = ordinary_path(root, relative)
    before = source.stat(follow_symlinks=False)
    if before.st_size > limit:
        raise InputChanged("input metadata exceeds its byte budget")
    with source.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
    after = source.stat(follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if len(raw) != before.st_size or len(raw) > limit or any(
            getattr(before, key) != getattr(opened, key) or getattr(opened, key) != getattr(after, key) for key in fields):
        raise InputChanged("input metadata changed while reading")
    return raw


def write_new(path, raw):
    path = Path(path)
    ensure_no_symlink_ancestors(path.parent, path)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _row(relative, raw):
    return {"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def protect_inputs(root):
    paths = list(Path(root).rglob("*"))
    for path in paths:
        ensure_no_symlink_ancestors(root, path)
        if path.is_file():
            path.chmod(0o444)
    for path in sorted((item for item in paths if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)
    Path(root).chmod(0o555)


def _input_files(root):
    pending, found = [Path(root) / "inputs"], set()
    while pending:
        directory = pending.pop()
        ensure_no_symlink_ancestors(root, directory)
        require_real_directory(directory)
        for entry in os.scandir(directory):
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or is_reparse_point(metadata):
                raise InputChanged("input tree contains a link")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                found.add(Path(entry.path).relative_to(root).as_posix())
            else:
                raise InputChanged("input tree contains a nonordinary file")
    return found


def verify_inputs(root, rows, cancellation):
    if _input_files(root) != {row["path"] for row in rows}:
        raise InputChanged("input tree differs from its receipt")
    try:
        for row in rows:
            if cancellation.is_cancelled():
                raise InputChanged("input verification was cancelled")
            source = ordinary_path(root, row["path"])
            _stream_verified(source, row["size"], row["sha256"], destination=None, cancellation=cancellation)
    except (OSError, ValueError) as exc:
        raise InputChanged("input verification failed") from exc


class SnapshotCancellation:
    def __init__(self, service, task):
        self.service, self.task = service, task

    @property
    def reason(self):
        return CancellationReason.SERVICE_SHUTDOWN if self.service._stop.is_set() else CancellationReason.USER_CANCEL

    def is_cancelled(self):
        if self.service._stop.is_set():
            return True
        with self.service.store.repository.database_read() as db:
            row = db.execute("SELECT status FROM agent_followup_snapshots WHERE run_id=?", (self.task["run_id"],)).fetchone()
            return row is None or row[0] != "BUILDING" or not self.service.store.alive(db, self.task["conversation_id"], self.task["run_id"])

    def wait(self, timeout_seconds):
        self.service._stop.wait(min(0.1, timeout_seconds) if timeout_seconds is not None else 0.1)
        return self.is_cancelled()


def build_snapshot(service, task, cancellation):
    source = service.store._source(task)
    layout = service.layout
    job_id = _ID.validate_python(source.source_job_id)
    original = layout.workspaces / job_id
    job_root = layout.jobs / job_id
    ensure_no_symlink_ancestors(layout.data_root, job_root)
    require_real_directory(layout.jobs)
    job_root.mkdir(mode=0o700, exist_ok=True)
    require_real_directory(job_root)
    destination = job_root / "followup-inputs"
    staging = job_root / "followup-inputs.pending"
    if destination.exists() or destination.is_symlink() or staging.exists() or staging.is_symlink():
        raise InputChanged("snapshot destination already exists")
    generated = {"inputs/problem.txt": source.problem_text.encode("utf-8"),
        "inputs/report.md": source.report_markdown.encode("utf-8")}
    copies, logs = [], []
    if hashlib.sha256(generated["inputs/report.md"]).hexdigest() != source.report_sha256:
        raise InputChanged("published report hash differs")
    if source.has_logs:
        name, audit_name, key, hash_key = (("generic_logs.json", "generic_logs.json", "logs", "sha256")
            if source.source_kind == "GENERIC" else ("target_logs.json", "methods_target_logs.json", "target_logs", "content_sha256"))
        raw = read_small(original, "inputs/" + name)
        if raw != read_small(job_root, audit_name):
            raise InputChanged("log receipt differs from the published execution record")
        payload = json.loads(raw)
        entries = payload[key]
        if not isinstance(entries, list) or len(entries) > 10_000:
            raise InputChanged("invalid log manifest")
        seen = set()
        for ordinal, entry in enumerate(entries, 1):
            path, size, digest = entry.get("log_path"), entry.get("size"), entry.get(hash_key)
            if (not isinstance(path, str) or not path.startswith("inputs/") or path in seen
                    or type(size) is not int or size < 0 or not isinstance(digest, str) or _HASH.fullmatch(digest) is None):
                raise InputChanged("invalid log identity")
            seen.add(path)
            file = ordinary_path(original, path)
            target = f"inputs/logs/log-{ordinal:06d}.log"
            copies.append((file, {"path": target, "size": size, "sha256": digest}))
            logs.append({"log_path": target, "size": size, "sha256": digest,
                "source_label": str(entry.get("label", entry.get("relative_path", path)))[:1024]})
    generated["inputs/logs.json"] = _json_bytes({"schema_version": 1, "logs": logs})
    rows = [_row(path, raw) for path, raw in generated.items()] + [row for _, row in copies]
    manifest = {"schema_version": 1, "case_id": source.case_id, "run_id": task["run_id"],
        "source_job_id": job_id, "report_sha256": source.report_sha256, "source_kind": source.source_kind,
        "files": sorted(rows, key=lambda item: item["path"])}
    raw_manifest = _json_bytes(manifest)
    if len(raw_manifest) > _MANIFEST_LIMIT:
        raise InputChanged("snapshot manifest exceeds its byte budget")
    service.store.reserve_snapshot(task["run_id"], sum(row["size"] for row in rows) + len(raw_manifest),
        service.snapshot_max_bytes, service.snapshot_total_bytes)
    if cancellation.is_cancelled():
        raise InputChanged("snapshot was cancelled")
    staging.mkdir(mode=0o700)
    (staging / "inputs").mkdir(mode=0o700)
    (staging / "inputs" / "logs").mkdir(mode=0o700)
    for relative, raw in generated.items():
        write_new(staging / relative, raw)
    for file, row in copies:
        _stream_verified(file, row["size"], row["sha256"], destination=staging / row["path"], cancellation=cancellation)
    verify_inputs(staging, rows, cancellation)
    write_new(staging / "manifest.json", raw_manifest)
    protect_inputs(staging / "inputs")
    (staging / "manifest.json").chmod(0o444)
    PlatformFileSync().sync_directory(staging)
    if cancellation.is_cancelled():
        raise InputChanged("snapshot was cancelled before publication")
    os.rename(staging, destination)
    PlatformFileSync().sync_directory(job_root)
    return manifest


def copy_snapshot(service, snapshot, workspace, cancellation):
    manifest = json.loads(snapshot["manifest_json"])
    job = _ID.validate_python(snapshot["source_job_id"])
    root = service.layout.jobs / job / "followup-inputs"
    ensure_no_symlink_ancestors(service.layout.data_root, root)
    if read_small(root, "manifest.json") != _json_bytes(manifest):
        raise InputChanged("snapshot manifest changed")
    if (manifest.get("run_id"), manifest.get("source_job_id"), manifest.get("report_sha256")) != (
            snapshot["run_id"], job, snapshot["report_sha256"]):
        raise InputChanged("snapshot identity changed")
    rows = manifest["files"]
    if _input_files(root) != {row["path"] for row in rows}:
        raise InputChanged("snapshot tree changed")
    for row in rows:
        if not row["path"].startswith("inputs/"):
            raise InputChanged("snapshot path is outside inputs")
        source = ordinary_path(root, row["path"])
        target = workspace / row["path"]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        ensure_no_symlink_ancestors(workspace, target)
        _stream_verified(source, row["size"], row["sha256"], destination=target, cancellation=cancellation)
    return list(rows)


class SnapshotWorker:
    def __init__(self, service):
        self.service = service
        self._processing = threading.Lock()

    def run_once(self):
        service = self.service
        if not service.enabled or service._stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        task = None
        try:
            with service._observe_lock:
                cid = service._observe_pending.pop() if service._observe_pending else None
            if cid is not None:
                for rid in service.store.report_run_ids(cid):
                    try:
                        service.observe_report(cid, rid)
                    except Exception as exc:
                        log_event("agent.followup.snapshot_observation_failed", error_type=type(exc).__name__)
            task = service.store.claim_snapshot()
            if task is None:
                return cid is not None
            with service.agent.usage_guard.acquire(task["conversation_id"]):
                signal = SnapshotCancellation(service, task)
                manifest = build_snapshot(service, task, signal)
                service.store.finish_snapshot(task["run_id"], manifest)
            return True
        except Exception as exc:
            if task is not None:
                try:
                    service.store.fail_snapshot(task["run_id"])
                except Exception:
                    pass
            log_event("agent.followup.snapshot_failed", error_type=type(exc).__name__)
            return False
        finally:
            self._processing.release()
