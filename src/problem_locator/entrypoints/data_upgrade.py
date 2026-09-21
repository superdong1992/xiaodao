"""Offline copy upgrade of V11 r1/r2 Agent v1 data to Agent storage v2.

Never open the source database through SQLite: even a read-only connection can
create or recover its shared-memory file. Copy the locked database and WAL first.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from problem_locator.agent.models import AgentAttachment, AgentMessage, ConversationView, MessageReceipt
from problem_locator.agent.store import AgentStore, upgrade_agent_storage_v2
from problem_locator.contracts import CONTRACT_REVISION, SCHEMA_VERSION, ArtifactKind, ResourceKind, ResourceRef, StateFile, canonical_json_bytes
from problem_locator.integrations.agent_json import parse_agent_json_bytes
from problem_locator.storage.atomic import is_reparse_point, read_stable_file_bytes, require_ordinary_file, require_real_directory, write_synced_file
from problem_locator.storage.paths import ensure_no_symlink_ancestors
from problem_locator.storage.platform import PlatformFileSync, chmod_no_follow
from problem_locator.storage.resource_files import validate_formal_resource
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES


SOURCE_REVISION = "v11-contract-r1"
TARGET_REVISION = "v11-contract-r2"
RECEIPT_FILENAME = "data-upgrade.agent-v2.receipt.json"
_DATABASE_FILES = {"completed.sqlite3", "completed.sqlite3-wal", "completed.sqlite3-shm"}
_BARRIER_FILENAME = "data-format.json.tmp"
_MUTABLE_FILES = _DATABASE_FILES | {"data-format.json", _BARRIER_FILENAME}
_CORE_TABLES = {"metadata", "completed_cases", "object_index", "request_index", "resource_index", "archive_tasks"}
_AGENT_TABLES = {"agent_conversations", "agent_messages", "agent_events", "agent_dispatches", "agent_message_adoptions", "agent_attachments"}
_AGENT_V2_TABLES = {"agent_conversation_runs", "agent_attachment_imports", "agent_stop_requests", "agent_cleanup_jobs", "agent_deleted_requests", "agent_create_keys", "agent_create_key_times"}
_TERMINAL_CASES = {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED"}
_CLOSED_CONVERSATIONS = {"COMPLETED", "FAILED", "INTERRUPTED"}


class DataUpgradeError(ValueError):
    def __init__(self, code: str, message: str, *, staging_root: Path | None = None):
        super().__init__(message)
        self.code, self.staging_root = code, staging_root


def _require(condition: bool, message: str, code: str = "SOURCE_INVALID") -> None:
    if not condition:
        raise DataUpgradeError(code, message)


def _require_linux() -> None:
    _require(sys.platform == "linux", "数据升级命令只能在 Linux Server 上运行。", "PLATFORM_UNSUPPORTED")


def _marker(revision: str) -> bytes:
    return canonical_json_bytes({"contract_revision": revision, "format_id": "problem-locator-data-v11",
        "schema_version": 11, "state_schema_version": 11})


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_nlink)


@contextmanager
def _source_lock(root: Path):
    import fcntl

    path = root / ".instance.lock"
    before = require_ordinary_file(path)
    _require(before.st_nlink == 1, "源实例锁不能是硬链接。")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _require(_fingerprint(os.fstat(descriptor)) == _fingerprint(before), "源实例锁在打开时发生变化。")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataUpgradeError("SOURCE_LOCKED", "源实例仍在运行，请排空任务并停服后重试。") from None
        try:
            yield lambda: _require(_fingerprint(path.lstat()) == _fingerprint(before), "源实例锁在升级期间发生变化。")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _paths(source_root: Path, target_root: Path) -> tuple[Path, Path]:
    source, target = Path(source_root), Path(target_root)
    _require(source.is_absolute() and target.is_absolute(), "源目录和目标目录必须使用绝对路径。", "PATH_INVALID")
    for path in (source, target):
        ensure_no_symlink_ancestors(Path(path.anchor), path)
    source, target = source.resolve(strict=True), target.resolve(strict=False)
    _require(source != target and source not in target.parents and target not in source.parents,
        "源目录和目标目录必须相互独立。", "PATH_INVALID")
    require_real_directory(source)
    require_real_directory(target.parent)
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    _require(all((parent.stat().st_dev, parent.stat().st_ino) != source_identity
        for parent in (target.parent, *target.parent.parents)), "目标目录不能位于源目录的挂载别名下。", "PATH_INVALID")
    _require(not target.exists(), "目标目录必须尚不存在。", "TARGET_EXISTS")
    return source, target


def _file(path: Path, destination: Path | None = None) -> dict:
    before = require_ordinary_file(path)
    _require(before.st_nlink == 1, "数据目录不能包含硬链接文件。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    digest, size = hashlib.sha256(), 0
    sink = None
    try:
        _require(_fingerprint(os.fstat(descriptor)) == _fingerprint(before), "源文件在打开时发生变化。")
        if destination is not None:
            sink = destination.open("xb")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if sink is not None:
                sink.write(chunk)
        _require(_fingerprint(os.fstat(descriptor)) == _fingerprint(before)
            and _fingerprint(path.lstat()) == _fingerprint(before) and size == before.st_size,
            "源文件在读取期间发生变化。")
        if sink is not None:
            sink.flush()
            os.fsync(sink.fileno())
    finally:
        os.close(descriptor)
        if sink is not None:
            sink.close()
    mode = stat.S_IMODE(before.st_mode)
    if destination is not None:
        chmod_no_follow(destination, mode)
    return {"size": size, "sha256": digest.hexdigest(), "mode": mode}


def _inventory(root: Path, *, copy_to: Path | None = None) -> dict:
    files, directories = {}, {}
    pending = [root]
    while pending:
        directory = pending.pop()
        before = require_real_directory(directory)
        relative = directory.relative_to(root).as_posix()
        directories[relative] = stat.S_IMODE(before.st_mode)
        children = sorted(directory.iterdir())
        for child in children:
            metadata = child.lstat()
            _require(not is_reparse_point(metadata) and not stat.S_ISLNK(metadata.st_mode),
                "数据目录不能包含符号链接或重解析点。")
            key = child.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                if copy_to is not None:
                    (copy_to / key).mkdir(mode=0o700)
                pending.append(child)
            else:
                files[key] = _file(child, None if copy_to is None else copy_to / key)
        _require(_fingerprint(directory.lstat()) == _fingerprint(before), "数据目录在读取期间发生变化。")
    return {"files": dict(sorted(files.items())), "directories": dict(sorted(directories.items()))}


def _table_digests(db: sqlite3.Connection, *, original_columns=None) -> dict:
    schema = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    _require(not any(row[0] in {"trigger", "view"} for row in schema), "源数据库包含不受支持的触发器或视图。")
    tables = {row[1] for row in schema if row[0] == "table"}
    allowed = _CORE_TABLES | _AGENT_TABLES | (_AGENT_V2_TABLES if original_columns is not None else set())
    _require(_CORE_TABLES <= tables and tables <= allowed,
        "源数据库表结构不受支持。")
    _require(not tables.intersection(_AGENT_TABLES) or _AGENT_TABLES <= tables, "历史会话表不完整。")
    result = {"schema": hashlib.sha256(canonical_json_bytes(schema)).hexdigest()}
    for table in sorted(tables if original_columns is None else original_columns):
        columns = ([row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            if original_columns is None else original_columns[table])
        digest = hashlib.sha256()
        selection = ",".join('"' + column + '"' for column in columns)
        for row in db.execute(f'SELECT {selection} FROM "{table}" ORDER BY rowid'):
            if table == "metadata" and row[0] == "agent_storage_version":
                continue
            values = []
            for column, value in zip(columns, row, strict=True):
                if table == "completed_cases" and column == "snapshot":
                    value = _json(value)
                    _require(isinstance(value, dict), "历史 Case 快照不是 JSON 对象。")
                    value = {**value, "contract_revision": "REVISION_ONLY"}
                elif table == "agent_attachments" and column == "storage_path":
                    value = "ROOT_MAPPING_ONLY"
                elif isinstance(value, bytes):
                    value = {"sqlite_blob": base64.b64encode(value).decode("ascii")}
                encoded = canonical_json_bytes(value)
                values.append([type(row[columns.index(column)]).__name__, encoded.decode("utf-8")])
            digest.update(canonical_json_bytes(values))
        result[table] = digest.hexdigest()
    return result


def _json(value):
    return parse_agent_json_bytes(value.encode("utf-8") if isinstance(value, str) else value).value


def _resources(aggregate):
    for attachment in aggregate.attachments.values():
        if attachment.storage_key is not None:
            yield ResourceRef(resource_kind=ResourceKind.FILE, storage_key=attachment.storage_key,
                size=attachment.size, sha256=attachment.sha256)
    for evidence in aggregate.evidence.values():
        if evidence.resource_ref is not None:
            yield evidence.resource_ref
    for artifact in aggregate.artifacts.values():
        yield ResourceRef(resource_kind=artifact.resource_kind, storage_key=artifact.storage_key,
            size=artifact.size, sha256=artifact.sha256)


def _upgrade_snapshots(db, root, source_revision=SOURCE_REVISION):
    objects, requests, resources, cases, artifacts, jobs = {}, {}, {}, set(), {}, {}
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    StateFile(schema_version=11, contract_revision=TARGET_REVISION, generation=1,
        installation_id=metadata["installation_id"], created_at=metadata["created_at"], updated_at=metadata["created_at"],
        runtime_epochs=[], recovery_processing_records={}, cases={}, idempotency_records={})
    for case_id, raw in db.execute("SELECT case_id,snapshot FROM completed_cases ORDER BY case_id"):
        value = _json(raw)
        _require(isinstance(value, dict) and value.get("schema_version") == 11
            and value.get("contract_revision") == source_revision, "源 Case 快照与目录版本不一致。")
        upgraded = {**value, "contract_revision": TARGET_REVISION}
        state = StateFile.model_validate(upgraded)
        _require(set(state.cases) == {case_id} and state.installation_id == metadata.get("installation_id"),
            "历史 Case 快照的安装标识或归属不一致。")
        aggregate = state.cases[case_id]
        _require(aggregate.case.status.value in _TERMINAL_CASES and aggregate.case.active_job_id is None
            and aggregate.case.archive_status != "PENDING", "尚有未结束的 Case 或归档任务。", "SOURCE_NOT_DRAINED")
        cases.add(case_id)
        for name in ("jobs", "attachments", "evidence", "artifacts", "outcomes"):
            for key in getattr(aggregate, name):
                _require(key not in objects, "历史对象标识在多个 Case 中重复。")
                objects[key] = case_id
        for key in state.idempotency_records:
            _require(key not in requests, "历史请求标识在多个 Case 中重复。")
            requests[key] = case_id
        for reference in _resources(aggregate):
            validate_formal_resource(root, reference, require_read_only=True)
            resources[reference.storage_key] = case_id
        artifacts.update(aggregate.artifacts)
        jobs.update({key: case_id for key in aggregate.jobs})
        if source_revision != TARGET_REVISION:
            encoded = canonical_json_bytes(upgraded)
            db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?",
                (encoded.decode("utf-8") if isinstance(raw, str) else encoded, case_id))
    for table, column, expected in (("object_index", "object_id", objects),
        ("request_index", "request_key", requests), ("resource_index", "storage_key", resources)):
        _require(dict(db.execute(f"SELECT {column},case_id FROM {table}")) == expected,
            "历史索引与 Case 快照不一致。")
    for case_id, status, payload in db.execute("SELECT case_id,status,payload FROM archive_tasks"):
        _require(case_id in cases and status in {"READY", "FAILED"}, "尚有未完成的归档任务。", "SOURCE_NOT_DRAINED")
        value = _json(payload)
        report = artifacts.get(value["report_artifact_id"])
        _require(report is not None and report.case_id == case_id and report.kind is ArtifactKind.USER_RESULT
            and jobs.get(value["source_job_id"]) == case_id and report.created_by_job_id == value["source_job_id"],
            "归档任务引用的报告或 Job 类型、归属不一致。")
        _require(report.metadata.archive_plan is not None
            and report.metadata.archive_plan.model_dump(mode="json") == value["plan"],
            "归档任务的计划与历史报告不一致。")
        for key, source in zip(value["source_storage_keys"], value["plan"]["logs"], strict=True):
            _require(isinstance(key, str) and key.startswith("resources/cases/" + case_id + "/")
                and all(part not in {"", ".", ".."} for part in key.split("/")) and "\\" not in key,
                "归档源文件路径无效。")
            observed = _file(root / key)
            _require((observed["size"], observed["sha256"]) == (source["size"], source["sha256"]),
                "归档源文件与历史记录不一致。")
    return cases, objects, artifacts, len(resources)


def _upgrade_conversations(db, source, target, root, cases, objects, artifacts):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_conversations'").fetchone():
        return 0, 0
    conversations, closed_without_case = {}, set()
    for cid, status, body, case_id in db.execute("SELECT conversation_id,status,body,case_id FROM agent_conversations"):
        stored = _json(body)
        view = ConversationView.model_validate({key: value for key, value in stored.items() if key in ConversationView.model_fields})
        idle = (status == "INTAKE" and case_id is None and view.job_id is None
            and not stored.get("report_available", False) and not stored.get("draft") and view.last_event_id == 0
            and all(not db.execute(f"SELECT 1 FROM {table} WHERE conversation_id=? LIMIT 1", (cid,)).fetchone()
                for table in ("agent_messages", "agent_dispatches", "agent_attachments", "agent_events")))
        _require(status in _CLOSED_CONVERSATIONS or idle, "尚有未结束的会话，请排空后停服。", "SOURCE_NOT_DRAINED")
        missing_case = case_id is not None and case_id not in cases
        if missing_case and status in {"FAILED", "INTERRUPTED"} and not stored.get("report_available", False):
            # Active Cases were process-local in r1. Its normal restart recovery
            # preserves opaque IDs on failed/interrupted conversations without reports.
            closed_without_case.add(cid)
        _require((view.conversation_id, view.status, view.case_id) == (cid, status, case_id)
            and (case_id is None or not missing_case or cid in closed_without_case)
            and (not stored.get("report_available", False) or case_id in cases)
            and (status != "COMPLETED" or case_id in cases), "历史会话引用的 Case 不存在或状态不一致。")
        conversations[cid] = view
    dispatches = {}
    for did, cid, status in db.execute("SELECT dispatch_id,conversation_id,status FROM agent_dispatches"):
        _require(cid in conversations and status in _CLOSED_CONVERSATIONS | {"PENDING"}
            and conversations[cid].status in _CLOSED_CONVERSATIONS,
            "尚有未完成的派发记录。", "SOURCE_NOT_DRAINED")
        dispatches[did] = cid
    attachment_ids, mapped = {}, 0
    for aid, cid, body, path in db.execute("SELECT attachment_id,conversation_id,body,storage_path FROM agent_attachments"):
        attachment = AgentAttachment.model_validate(_json(body))
        _require(str(uuid.UUID(aid)) == aid and (attachment.attachment_id, attachment.conversation_id) == (aid, cid)
            and cid in conversations, "历史附件归属无效。")
        _require(conversations[cid].status in _CLOSED_CONVERSATIONS,
            "尚有未完成的附件上传。", "SOURCE_NOT_DRAINED")
        relative = Path("resources/conversations") / aid / "payload"
        if path is not None:
            _require(path == str(source / relative), "历史附件路径与源目录不一致。")
        if attachment.status in {"READY", "IMPORTED"}:
            observed = _file(root / relative)
            _require((observed["size"], observed["sha256"]) == (attachment.size, attachment.sha256),
                "历史会话附件内容校验失败。")
        if attachment.case_attachment_id is not None:
            _require(cid in closed_without_case or (attachment.case_attachment_id in objects
                and objects[attachment.case_attachment_id] == conversations[cid].case_id),
                "已导入附件引用的 Case 附件不存在。")
        if path is not None:
            db.execute("UPDATE agent_attachments SET storage_path=? WHERE attachment_id=?", (str(target / relative), aid))
            mapped += 1
        attachment_ids[aid] = cid
    messages = {}
    for mid, cid, body, receipt in db.execute("SELECT message_id,conversation_id,body,receipt FROM agent_messages"):
        message, accepted = AgentMessage.model_validate(_json(body)), MessageReceipt.model_validate(_json(receipt))
        _require(cid in conversations and message.message_id == mid and accepted.message_id == mid
            and accepted.conversation_id == cid and all(attachment_ids.get(aid) == cid for aid in message.attachment_ids),
            "历史消息或附件引用不一致。")
        _require(message.status not in {"QUEUED", "PROCESSING"}, "尚有未处理的会话消息。", "SOURCE_NOT_DRAINED")
        messages[mid] = cid
    for mid, cid, did in db.execute("SELECT message_id,conversation_id,dispatch_id FROM agent_message_adoptions"):
        _require(messages.get(mid) == cid and dispatches.get(did) == cid, "历史消息采纳记录引用不一致。")
    sequences = {cid: 0 for cid in conversations}
    for cid, sequence, body in db.execute("SELECT conversation_id,sequence,body FROM agent_events ORDER BY conversation_id,sequence"):
        original = _json(body)
        _require(original.get("schema_version") == 1 and "run_id" not in original,
            "旧会话事件的版本与目录标记不一致。")
        # Validate through the same read-only projection used by historical SSE;
        # the legacy event bytes and their sequence remain entirely unchanged.
        event = AgentStore._event(body, cid)
        _require(cid in conversations and (event.conversation_id, event.sequence) == (cid, sequence)
            and sequence == sequences[cid] + 1 and (event.case_id is None or event.case_id == conversations[cid].case_id),
            "历史事件序列或 Case 引用不一致。")
        _require(event.job_id is None or (event.job_id in objects and objects[event.job_id] == event.case_id)
            or (cid in closed_without_case and event.case_id == conversations[cid].case_id), "历史事件引用的 Job 不存在。")
        _require(cid not in closed_without_case or (event.type != "result.available" and not event.data.get("artifacts")),
            "包含报告的历史会话必须有完整的持久化 Case。")
        sequences[cid] = sequence
        for public in event.data.get("artifacts", []):
            artifact = artifacts.get(public["artifact_id"])
            _require(artifact is not None and artifact.case_id == event.case_id
                and (artifact.size, artifact.sha256) == (public["size"], public["sha256"]), "历史报告事件引用不一致。")
    _require(all(view.last_event_id == sequences[cid] for cid, view in conversations.items()), "会话的末尾事件标识不一致。")
    return len(conversations), mapped


def _bind_legacy_intake_workspaces(db, root: Path) -> int:
    """Adopt only server receipts whose identity and two byte hashes agree."""
    workspaces = root / "tmp" / "workspaces"
    if not workspaces.is_dir():
        return 0
    grouped = {}
    for directory in sorted(workspaces.iterdir()):
        try:
            if str(uuid.UUID(directory.name)) != directory.name or not directory.is_dir():
                continue
            receipt = directory / "runtime" / "intake-response-extraction.json"
            if not receipt.is_file() or receipt.stat().st_size > 65536:
                continue
            value = _json(read_stable_file_bytes(receipt))
            cid = value.get("conversation_id")
            if value.get("phase") != "INTAKE" or value.get("diagnostic_id") != directory.name:
                continue
            row = db.execute("SELECT current_run_id FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
            if row is None:
                continue
            for name, prefix in (("intake-response-original.txt", "raw"), ("intake-response-effective.json", "effective")):
                observed = _file(directory / "runtime" / name)
                if (observed["size"], observed["sha256"]) != (value.get(prefix + "_size_bytes"), value.get(prefix + "_sha256")):
                    break
            else:
                grouped.setdefault(row[0], []).append(directory.name)
        except (OSError, ValueError, TypeError, AttributeError):
            # An unproven old scratch workspace remains untouched; it must not
            # become deletion authority merely because its name looks valid.
            continue
    for run_id, workspace_ids in grouped.items():
        raw = db.execute("SELECT body FROM agent_conversation_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        value = {**_json(raw), "legacy_workspace_ids": sorted(workspace_ids)}
        db.execute("UPDATE agent_conversation_runs SET body=? WHERE run_id=?",
            (canonical_json_bytes(value).decode("utf-8"), run_id))
    return sum(map(len, grouped.values()))


def _upgrade_database(root: Path, source: Path, target: Path, *, source_revision=SOURCE_REVISION, owner_map=None) -> dict:
    db = sqlite3.connect(root / "completed.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        _require(list(db.execute("PRAGMA integrity_check")) == [("ok",)], "源数据库完整性校验失败。")
        before = _table_digests(db)
        original_columns = {table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            for table in before if table != "schema"}
        marker = db.execute("SELECT value FROM metadata WHERE key='agent_storage_version'").fetchone()
        _require(marker is None or marker[0] == "1", "源会话存储版本不是 v1。", "VERSION_UNSUPPORTED")
        db.execute("BEGIN IMMEDIATE")
        cases, objects, artifacts, resource_count = _upgrade_snapshots(db, root, source_revision)
        conversations, mappings = _upgrade_conversations(db, source, target, root, cases, objects, artifacts)
        _require(_table_digests(db) == before, "升级改变了历史业务数据，目标副本不会发布。", "HISTORY_CHANGED")
        assigned = owner_map or {}
        workspace_count = 0
        if "agent_conversations" in original_columns:
            ids = {row[0] for row in db.execute("SELECT conversation_id FROM agent_conversations")}
            _require(set(assigned).issubset(ids), "归属映射包含源目录中不存在的会话。", "OWNERSHIP_INVALID")
            upgrade_agent_storage_v2(db, assigned)
            workspace_count = _bind_legacy_intake_workspaces(db, root)
            _require(db.execute("SELECT count(*) FROM agent_conversation_runs").fetchone()[0] == conversations,
                "历史会话与迁移后的诊断轮次不一致。")
            _require(not list(db.execute("SELECT 1 FROM agent_conversations c LEFT JOIN agent_conversation_runs r "
                "ON r.run_id=c.current_run_id AND r.conversation_id=c.conversation_id WHERE r.run_id IS NULL")),
                "迁移后的会话轮次引用无效。")
        else:
            _require(not assigned, "没有历史会话可以导入归属。", "OWNERSHIP_INVALID")
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('agent_storage_version','2')")
        after = _table_digests(db, original_columns=original_columns)
        _require({key: value for key, value in after.items() if key != "schema"} ==
            {key: value for key, value in before.items() if key != "schema"},
            "会话迁移改变了历史消息、事件或回执字节。", "HISTORY_CHANGED")
        for row in db.execute("SELECT snapshot FROM completed_cases"):
            StateFile.model_validate_json(row[0])
        _require(list(db.execute("PRAGMA integrity_check")) == [("ok",)] and not list(db.execute("PRAGMA foreign_key_check")),
            "升级后的数据库校验失败。")
        db.execute("COMMIT")
        _require(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0, "目标数据库 WAL 无法完成同步。")
        return {"completed_cases": len(cases), "conversations": conversations,
            "verified_resources": resource_count, "mapped_attachment_paths": mappings,
            "assigned_conversations": len(assigned), "unassigned_conversations": conversations - len(assigned),
            "linked_legacy_intake_workspaces": workspace_count,
            "preserved_table_digests": before}
    finally:
        db.close()


def _publish_directory(staging: Path, target: Path) -> None:
    """Atomically publish without replacing even an empty concurrent target."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
        raise OSError(ctypes.get_errno(), "目标目录发布失败。")


def _ownership_map(path: Path | None):
    if path is None:
        return {}, None
    _require(Path(path).stat().st_size <= 16 * 1024 * 1024, "归属映射文件过大。", "OWNERSHIP_INVALID")
    raw = read_stable_file_bytes(Path(path))
    _require(len(raw) <= 16 * 1024 * 1024, "归属映射文件过大。", "OWNERSHIP_INVALID")
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "归属映射包含重复会话。", "OWNERSHIP_INVALID")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        _require(isinstance(value, dict), "归属映射必须是会话 UUID 到 owner_key 的 JSON 对象。", "OWNERSHIP_INVALID")
        for key, owner in value.items():
            _require(str(uuid.UUID(key)) == key and isinstance(owner, str)
                and re.fullmatch(r"[0-9a-f]{64}", owner) is not None,
                "归属映射的会话或 owner_key 无效。", "OWNERSHIP_INVALID")
    except (ValueError, TypeError, AttributeError) as error:
        if isinstance(error, DataUpgradeError):
            raise
        raise DataUpgradeError("OWNERSHIP_INVALID", "归属映射不是有效的 JSON 或会话标识。") from error
    return value, hashlib.sha256(raw).hexdigest()


def upgrade_data_root(source_root: Path, target_root: Path, *, execute: bool = False,
                      ownership_map: Path | None = None) -> dict:
    _require_linux()
    _require((SCHEMA_VERSION, CONTRACT_REVISION) == (11, TARGET_REVISION), "当前程序不支持此兼容升级。", "VERSION_UNSUPPORTED")
    staging = None
    try:
        source, target = _paths(source_root, target_root)
        assigned, ownership_sha256 = _ownership_map(ownership_map)
        with _source_lock(source) as verify_lock:
            source_marker = read_stable_file_bytes(source / "data-format.json")
            source_revision = next((revision for revision in (SOURCE_REVISION, TARGET_REVISION)
                if source_marker == _marker(revision)), None)
            _require(source_revision is not None, "源目录必须是 V11 r1 或 r2 的旧版会话存储。", "VERSION_UNSUPPORTED")
            _require(not (source / "data-format.json.tmp").exists() and not (source / RECEIPT_FILENAME).exists(),
                "源目录存在未处理的升级或格式标记文件。")
            require_ordinary_file(source / "completed.sqlite3")
            if execute:
                staging = target.with_name(f".{target.name}.upgrade-{uuid.uuid4().hex}")
                staging.mkdir(mode=0o700)
                write_synced_file(staging / _BARRIER_FILENAME, b"DATA_ROOT upgrade is incomplete.\n", PlatformFileSync())
            inventory = _inventory(source, copy_to=staging)
            result = {"schema_version": 1, "status": "PLANNED", "source_root": str(source),
                "target_root": str(target), "source_revision": source_revision, "target_revision": TARGET_REVISION,
                "source_agent_storage_version": 1, "target_agent_storage_version": 2,
                "ownership_map_sha256": ownership_sha256, "ownership_assignments": len(assigned),
                "state_schema_version": 11, "source_manifest_sha256": hashlib.sha256(canonical_json_bytes(inventory)).hexdigest(),
                "source_file_count": len(inventory["files"]), "source_bytes": sum(item["size"] for item in inventory["files"].values()),
                "source_wal_present": "completed.sqlite3-wal" in inventory["files"],
                "database_and_resource_validation": "PENDING_EXECUTE", "old_exports_and_replay_supported": False}
            if not execute:
                verify_lock()
                return result
            assert staging is not None
            _require(_inventory(source) == inventory, "复制期间源目录发生变化。")
            # Keep source directory modes until validation so formal tree checks
            # see exactly the original permissions; root stays private until publish.
            for key, mode in sorted(inventory["directories"].items(), reverse=True):
                if key != ".":
                    chmod_no_follow(staging / key, mode)
            result.update(_upgrade_database(staging, source, target, source_revision=source_revision, owner_map=assigned))
            chmod_no_follow(staging / "data-format.json", 0o600)
            (staging / "data-format.json").write_bytes(DATA_FORMAT_MARKER_BYTES)
            chmod_no_follow(staging / "data-format.json", inventory["files"]["data-format.json"]["mode"])
            after = _inventory(staging)
            _require(after["directories"] == {**inventory["directories"], ".": stat.S_IMODE(staging.stat().st_mode)},
                "历史目录结构或权限发生变化。", "HISTORY_CHANGED")
            immutable = {key: value for key, value in inventory["files"].items() if key not in _MUTABLE_FILES}
            _require({key: value for key, value in after["files"].items() if key not in _MUTABLE_FILES} == immutable,
                "历史产物字节或权限发生变化，目标副本不会发布。", "HISTORY_CHANGED")
            _require(_inventory(source) == inventory, "验证期间源目录发生变化。")
            result.update(status="UPGRADED", database_and_resource_validation="VERIFIED",
                source_manifest=inventory, immutable_manifest_sha256=hashlib.sha256(canonical_json_bytes(immutable)).hexdigest(),
                upgraded_at=datetime.now(timezone.utc).isoformat(),
                next_step="请显式切换 DATA_ROOT 后启动新版本；回滚时重新使用原目录。")
            with (staging / RECEIPT_FILENAME).open("xb") as handle:
                handle.write(canonical_json_bytes(result))
                handle.flush()
                os.fsync(handle.fileno())
            sync = PlatformFileSync()
            for path in (staging / "completed.sqlite3", staging / "data-format.json"):
                sync.sync_file(path)
            for key in sorted(inventory["directories"], key=lambda value: len(Path(value).parts), reverse=True):
                sync.sync_directory(staging / key)
            verify_lock()
            published_identity = (staging.stat().st_dev, staging.stat().st_ino)
            _publish_directory(staging, target)
            try:
                sync.sync_directory(target.parent)
                (target / _BARRIER_FILENAME).unlink()
                chmod_no_follow(target, inventory["directories"]["."])
                sync.sync_directory(target)
                sync.sync_directory(target.parent)
            except BaseException:
                # Configuration has not switched. Block service startup again
                # before restoring the unpublished name, including fsync errors.
                observed = require_real_directory(target)
                _require((observed.st_dev, observed.st_ino) == published_identity,
                    "目标目录身份已变化，请人工检查发布状态。", "PUBLICATION_UNCERTAIN")
                chmod_no_follow(target, 0o700)
                if not (target / _BARRIER_FILENAME).exists():
                    write_synced_file(target / _BARRIER_FILENAME, b"DATA_ROOT upgrade is incomplete.\n", sync)
                _publish_directory(target, staging)
                raise
            return result
    except DataUpgradeError as exc:
        exc.staging_root = staging
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, AttributeError) as exc:
        raise DataUpgradeError("UPGRADE_FAILED", "数据升级未完成，源目录保持不变；请检查停服状态、目录和数据完整性。",
            staging_root=staging) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线复制并升级 V11 r1/r2 会话存储，保留历史消息和报告。")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--ownership-map", type=Path, help="可选的会话 UUID 到可信 owner_key 的 JSON 映射。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = upgrade_data_root(args.source_root, args.target_root, execute=args.execute, ownership_map=args.ownership_map)
    except DataUpgradeError as exc:
        print(json.dumps({"status": "FAILED", "code": exc.code, "message": str(exc),
            "staging_root": str(exc.staging_root) if exc.staging_root is not None else None}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
