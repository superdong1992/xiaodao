"""Offline, non-destructive import of current SQLite histories into PostgreSQL."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from problem_locator.agent.models import AgentAttachment, AgentMessage
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import canonical_json_bytes
from problem_locator.followup.models import FollowupEvent, FollowupItem, FollowupSource
from problem_locator.followup.store import FollowupStore
from problem_locator.memory.store import MemoryStore
from problem_locator.storage.atomic import read_stable_file_bytes, write_synced_file
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.database import table_columns, table_names
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES, StorageLayout
from problem_locator.storage.platform import PlatformFileSync, chmod_no_follow
from problem_locator.storage.postgres_layout import marker_bytes, root_identity
from problem_locator.storage.state_repository import CaseStateRepository
from . import data_upgrade as safety

RECEIPT_FILENAME = "postgresql-import.receipt.json"
_BARRIER = "data-format.json.tmp"
_PENDING_BACKEND = "postgresql-import-pending"
_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
_MEMORY = {"memory_feedback", "memory_feedback_requests", "memory_tasks"}
_FOLLOWUP = {"agent_followup_metadata", "agent_followup_snapshots", "agent_followup_tasks",
    "agent_followup_events", "agent_followup_stops"}
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresImportError(safety.DataUpgradeError):
    pass


def _require(condition, message, code="SOURCE_INVALID"):
    if not condition:
        raise PostgresImportError(code, message)


def _source_schema(db):
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    _require(not any(kind in {"view", "trigger"} for kind, _ in objects), "源数据库包含不受支持的视图或触发器。")
    tables = {name for kind, name in objects if kind == "table"}
    allowed = safety._CORE_TABLES | safety._AGENT_TABLES | safety._AGENT_V2_TABLES | _MEMORY | _FOLLOWUP | {
        "completed_case_retention", "history_cleanup_jobs"}
    _require(safety._CORE_TABLES <= tables and tables <= allowed, "源数据库的表结构不属于当前 V11 r2 格式。", "VERSION_UNSUPPORTED")
    for group in (safety._AGENT_TABLES | safety._AGENT_V2_TABLES, _MEMORY, _FOLLOWUP):
        _require(not tables.intersection(group) or group <= tables, "源数据库的业务表不完整。")
    _require(not tables.intersection(_MEMORY | _FOLLOWUP) or safety._AGENT_TABLES <= tables,
        "源反馈或追问数据缺少会话表。")
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    _require("storage_backend" not in metadata and "data_root_identity" not in metadata,
        "源数据库已经包含其他存储后端标记。", "VERSION_UNSUPPORTED")
    if "agent_conversations" in tables:
        _require(metadata.get("agent_storage_version") == "2", "请先用原版本工具将会话存储升级到 v2。", "VERSION_UNSUPPORTED")
    if _FOLLOWUP <= tables:
        _require(dict(db.execute("SELECT key,value FROM agent_followup_metadata")).get("storage_version") == "1",
            "源报告追问存储版本不受支持。", "VERSION_UNSUPPORTED")
    return tables, metadata


def _validate_drained(db, tables):
    conditions = {
        "agent_dispatches": "status IN ('PENDING','RUNNING')",
        "agent_messages": "json_extract(body,'$.status') IN ('QUEUED','PROCESSING')",
        "agent_generic_restarts": "status IN ('PENDING','COMMITTED')",
        "agent_cleanup_jobs": "status<>'DONE'",
        "history_cleanup_jobs": "1=1",
        "memory_tasks": "status IN ('PENDING','RUNNING')",
        "agent_followup_tasks": "status IN ('QUEUED','RUNNING','CANCELLING')",
        "agent_followup_snapshots": "status IN ('PENDING','BUILDING')",
        "agent_attachments": "json_extract(body,'$.status') IN ('RESERVED','UPLOADING')",
    }
    for table, condition in conditions.items():
        if table in tables:
            _require(db.execute(f'SELECT 1 FROM "{table}" WHERE {condition} LIMIT 1').fetchone() is None,
                "源目录仍有待处理任务、附件上传或清理任务，请排空后停服。", "SOURCE_NOT_DRAINED")
    if "agent_conversation_runs" not in tables:
        return
    for cid, rid, status, raw in db.execute("SELECT conversation_id,run_id,status,body FROM agent_conversation_runs"):
        body = safety._json(raw)
        _require(body.get("run_id") == rid and body.get("conversation_id") == cid and body.get("status") == status,
            "源诊断轮次的标识或状态不一致。")
        idle = status == "INTAKE" and body.get("case_id") is None and not body.get("intake_pending") and not body.get("draft")
        if idle:
            idle = all(db.execute(f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (rid,)).fetchone() is None
                for table in ("agent_messages", "agent_events", "agent_dispatches"))
        _require(status in _CLOSED or idle, "源目录仍有未结束的诊断，请排空后停服。", "SOURCE_NOT_DRAINED")
    _require(db.execute("SELECT 1 FROM agent_conversations c LEFT JOIN agent_conversation_runs r "
        "ON r.conversation_id=c.conversation_id AND r.run_id=c.current_run_id "
        "WHERE c.deleted_at IS NULL AND r.run_id IS NULL LIMIT 1").fetchone() is None,
        "源会话的当前诊断轮次不存在。")
    _require(db.execute("SELECT 1 FROM agent_conversation_runs r LEFT JOIN agent_conversations c USING(conversation_id) "
        "WHERE c.conversation_id IS NULL LIMIT 1").fetchone() is None, "源诊断轮次所属的会话不存在。")


def _validate_attachments(db, tables, source, payload, target):
    mappings = {}
    if "agent_attachments" not in tables:
        return mappings
    for aid, cid, raw, path in db.execute("SELECT attachment_id,conversation_id,body,storage_path FROM agent_attachments"):
        item = AgentAttachment.model_validate_json(raw)
        _require((item.attachment_id, item.conversation_id) == (aid, cid), "源附件记录的归属不一致。")
        _require(db.execute("SELECT 1 FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone() is not None,
            "源附件所属的会话不存在。")
        relative = Path("resources/conversations") / aid / "payload"
        if path is not None:
            _require(path == str(source / relative), "附件路径不属于源数据目录。")
            mappings[aid] = str(target / relative)
        if item.status in {"READY", "IMPORTED"}:
            observed = safety._file(payload / relative)
            _require((observed["size"], observed["sha256"]) == (item.size, item.sha256), "源附件内容校验失败。")
    for raw, in db.execute("SELECT body FROM agent_messages"):
        AgentMessage.model_validate_json(raw)
    return mappings


def _validate_followups(db, tables, payload):
    if not _FOLLOWUP <= tables:
        return
    for table in ("agent_followup_snapshots", "agent_followup_tasks", "agent_followup_events", "agent_followup_stops"):
        _require(db.execute(f"SELECT 1 FROM {table} f LEFT JOIN agent_conversation_runs r "
            "ON f.run_id=r.run_id AND f.conversation_id=r.conversation_id WHERE r.run_id IS NULL LIMIT 1").fetchone() is None,
            "源追问数据所属的诊断轮次不存在。")
    for raw, in db.execute("SELECT body FROM agent_followup_tasks"):
        FollowupItem.model_validate_json(raw)
    for raw, in db.execute("SELECT body FROM agent_followup_events"):
        FollowupEvent.model_validate_json(raw)
    for rid, job, raw, status, manifest_raw in db.execute(
            "SELECT run_id,source_job_id,source_json,status,manifest_json FROM agent_followup_snapshots"):
        source = FollowupSource(**safety._json(raw))
        _require(hashlib.sha256(source.report_markdown.encode()).hexdigest() == source.report_sha256,
            "追问报告正文与保存的校验值不一致。")
        if status != "READY":
            continue
        _require(str(uuid.UUID(job)) == job, "追问快照的 Job 标识无效。")
        manifest = safety._json(manifest_raw)
        _require((manifest.get("run_id"), manifest.get("source_job_id"), manifest.get("report_sha256")) ==
            (rid, job, source.report_sha256), "追问快照清单的归属不一致。")
        root = payload / "jobs" / job / "followup-inputs"
        actual = safety._json(read_stable_file_bytes(root / "manifest.json"))
        _require(actual == manifest, "追问快照清单与数据库不一致。")
        from problem_locator.followup.snapshots import ordinary_path
        for item in manifest["files"]:
            observed = safety._file(ordinary_path(root, item["path"]))
            _require((observed["size"], observed["sha256"]) == (item["size"], item["sha256"]),
                "追问快照文件校验失败。")


def _row_hash(row):
    encoded = [{"bytes": base64.b64encode(bytes(value)).decode()} if isinstance(value, (bytes, memoryview))
        else value for value in row]
    return hashlib.sha256(canonical_json_bytes(encoded)).digest()


def _hashes_digest(hashes):
    return {"rows": len(hashes), "sha256": hashlib.sha256(b"".join(sorted(hashes))).hexdigest()}


def _digest(rows):
    return _hashes_digest([_row_hash(row) for row in rows])


def _empty_postgres_database(database_url):
    import psycopg
    with psycopg.connect(database_url, autocommit=True, connect_timeout=10,
            options="-c search_path=public,pg_catalog") as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            # Require a dedicated empty database, including non-public schemas.
            existing = connection.execute("SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%' LIMIT 1").fetchone()
            _require(existing is None, "目标 PostgreSQL 数据库必须为空，不能覆盖已有数据。", "TARGET_DATABASE_NOT_EMPTY")


def _import_rows(sqlite, repository, tables, source, target, mappings):
    proofs = {}
    with repository.database_transaction() as db:
        expected_tables = table_names(db)
        _require(tables <= expected_tables, "当前程序不支持源数据库中的部分表。", "VERSION_UNSUPPORTED")
        for table in sorted(expected_tables):
            db.execute(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')
            if table not in {"metadata", "agent_followup_metadata"}:
                _require(db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None,
                    "目标数据库在初始化期间出现业务数据，导入已停止。", "TARGET_DATABASE_NOT_EMPTY")
        for table in sorted(tables):
            columns = [row[1] for row in sqlite.execute(f'PRAGMA table_info("{table}")')]
            target_columns = table_columns(db, table)
            _require(set(columns) == target_columns - {"storage_order"},
                "源数据库列定义与当前版本不一致。", "VERSION_UNSUPPORTED")
            kinds = dict(db.execute("SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=?", (table,)))
            selected = [*columns, *(["storage_order"] if "storage_order" in target_columns else [])]
            names = ",".join('"' + column + '"' for column in selected)
            placeholders = ",".join("?" for _ in selected)
            override = " OVERRIDING SYSTEM VALUE" if "storage_order" in target_columns else ""
            insert = f'INSERT INTO "{table}"({names}){override} VALUES ({placeholders})'
            source_hashes, expected_hashes, batch = [], [], []
            maximum = batch_bytes = 0
            db.execute(f'DELETE FROM "{table}"')
            metadata = dict(sqlite.execute("SELECT key,value FROM metadata")) if table == "metadata" else None
            for row in sqlite.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid'):
                source_hashes.append(_row_hash(row[1:]))
                values = list(row[1:])
                for index, (column, value) in enumerate(zip(columns, values, strict=True)):
                    if kinds[column] == "bytea" and isinstance(value, str):
                        values[index] = value.encode("utf-8")
                if table == "agent_attachments":
                    aid = values[columns.index("attachment_id")]
                    if aid in mappings:
                        values[columns.index("storage_path")] = mappings[aid]
                if "storage_order" in target_columns:
                    values.append(row[0])
                    maximum = max(maximum, row[0])
                expected_hashes.append(_row_hash(values))
                batch.append(tuple(values))
                batch_bytes += sum(len(value) for value in values if isinstance(value, (str, bytes)))
                if len(batch) >= 100 or batch_bytes >= 4 * 1024**2:
                    db.executemany(insert, batch)
                    batch, batch_bytes = [], 0
            if batch:
                db.executemany(insert, batch)
            if table == "metadata":
                additions = [("storage_backend", _PENDING_BACKEND),
                    ("data_root_identity", root_identity(StorageLayout.at(target)))]
                if "agent_storage_version" not in metadata:
                    additions.append(("agent_storage_version", "2"))
                db.executemany(insert, additions)
                expected_hashes.extend(_row_hash(row) for row in additions)
            if "storage_order" in target_columns:
                _require(maximum < 2**63 - 1, "源数据库排序编号已耗尽。")
                db.execute(f'ALTER TABLE "{table}" ALTER COLUMN storage_order RESTART WITH {max(1, maximum + 1)}')
            expected = _hashes_digest(expected_hashes)
            with db.raw.cursor(name="import_verify_" + table) as cursor:
                cursor.execute(f'SELECT {names} FROM "{table}"')
                actual = _digest(cursor)
            _require(expected == actual, "导入后的数据库内容与源记录不一致。", "IMPORT_VERIFICATION_FAILED")
            final = _digest((key, "postgresql-v1" if key == "storage_backend" else value)
                for key, value in db.execute("SELECT key,value FROM metadata")) if table == "metadata" else actual
            proofs[table] = {"source": _hashes_digest(source_hashes), "target": final,
                "columns": selected, "attachment_paths_mapped": len(mappings) if table == "agent_attachments" else 0}
        # No JSON payload, receipt or stored model answer is rewritten.
        _require(dict(db.execute("SELECT key,value FROM metadata"))["installation_id"] ==
            dict(sqlite.execute("SELECT key,value FROM metadata"))["installation_id"], "导入后的安装标识不一致。")
    return proofs


def _write_marker(payload, installation_id, sync):
    temporary = payload / "postgresql-marker.pending"
    write_synced_file(temporary, marker_bytes(installation_id), sync)
    os.replace(temporary, payload / "data-format.json")
    sync.sync_directory(payload)


def import_data_root(source_root: Path, target_root: Path, *, database_url_env="DATABASE_URL", execute=False):
    safety._require_linux()
    _require(_ENV.fullmatch(database_url_env) is not None, "数据库环境变量名无效。", "CONFIG_INVALID")
    staging = repository = published = published_identity = None
    sync = PlatformFileSync()
    try:
        source, target = safety._paths(source_root, target_root)
        with safety._source_lock(source) as verify_lock:
            _require(read_stable_file_bytes(source / "data-format.json") == DATA_FORMAT_MARKER_BYTES,
                "源目录必须是当前 V11 r2、会话 v2 的 SQLite 格式。", "VERSION_UNSUPPORTED")
            _require(not (source / _BARRIER).exists() and not (source / RECEIPT_FILENAME).exists(),
                "源目录存在未完成的迁移标记。")
            _require(not (source / "state.json").exists() and not (source / "state.json.prev").exists(),
                "此工具不导入旧 StateFile 目录。", "VERSION_UNSUPPORTED")
            safety.require_ordinary_file(source / "completed.sqlite3")
            inventory = safety._inventory(source)
            result = {"schema_version": 1, "status": "PLANNED", "source_data_root": str(source),
                "target_data_root": str(target), "database_url_env": database_url_env,
                "source_manifest_sha256": hashlib.sha256(canonical_json_bytes(inventory)).hexdigest(),
                "source_wal_present": "completed.sqlite3-wal" in inventory["files"],
                "source_file_count": len(inventory["files"]),
                "source_bytes": sum(item["size"] for item in inventory["files"].values()),
                "database_and_resource_validation": "PENDING_EXECUTE"}
            if not execute:
                verify_lock()
                return result
            database_url = os.environ.get(database_url_env, "")
            _require(database_url.startswith(("postgresql://", "postgres://")),
                "指定环境变量必须包含 PostgreSQL 连接地址。", "CONFIG_INVALID")
            _empty_postgres_database(database_url)
            staging = target.with_name(f".{target.name}.postgres-import-{uuid.uuid4().hex}")
            staging.mkdir(mode=0o700)
            payload, database_copy = staging / "payload", staging / "source-database"
            payload.mkdir(mode=0o700)
            database_copy.mkdir(mode=0o700)
            write_synced_file(payload / _BARRIER, b"PostgreSQL import is incomplete.\n", sync)
            _require(safety._inventory(source, copy_to=payload) == inventory, "复制期间源目录发生变化。")
            for name in safety._DATABASE_FILES:
                if (payload / name).exists():
                    os.rename(payload / name, database_copy / name)
            for key, mode in sorted(inventory["directories"].items(), reverse=True):
                if key != ".":
                    chmod_no_follow(payload / key, mode)
            clock = SimpleNamespace(now=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
            ids = SimpleNamespace(new=lambda kind: str(uuid.uuid4()))
            with closing(sqlite3.connect((database_copy / "completed.sqlite3").as_uri() + "?mode=ro", uri=True)) as sqlite:
                sqlite.execute("PRAGMA trusted_schema=OFF")
                sqlite.execute("PRAGMA query_only=ON")
                _require(sqlite.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                    and not sqlite.execute("PRAGMA foreign_key_check").fetchall(), "源数据库完整性校验失败。")
                tables, metadata = _source_schema(sqlite)
                _validate_drained(sqlite, tables)
                _, _, _, resource_count = safety._upgrade_snapshots(sqlite, payload, safety.TARGET_REVISION)
                mappings = _validate_attachments(sqlite, tables, source, payload, target)
                _validate_followups(sqlite, tables, payload)
                repository = CaseStateRepository(staging / "bootstrap", StorageCoordinationLock(), clock, ids,
                    database_url=database_url)
                AgentStore(repository, clock=clock)
                MemoryStore(repository, clock)
                FollowupStore(repository, clock)
                result["tables"] = _import_rows(sqlite, repository, tables, source, target, mappings)
                installation_id = metadata["installation_id"]
            immutable = {key: value for key, value in inventory["files"].items()
                if key not in safety._DATABASE_FILES | {"data-format.json"}}
            copied = safety._inventory(payload)
            _require({key: value for key, value in copied["files"].items() if key != _BARRIER} ==
                {key: value for key, value in inventory["files"].items() if key not in safety._DATABASE_FILES},
                "复制后的文件字节或权限发生变化。", "HISTORY_CHANGED")
            _require(safety._inventory(source) == inventory, "验证期间源目录发生变化。")
            _write_marker(payload, installation_id, sync)
            result.update(status="IMPORTED", installation_id=installation_id,
                database_and_resource_validation="VERIFIED", verified_resources=resource_count,
                source_manifest=inventory, immutable_manifest_sha256=hashlib.sha256(canonical_json_bytes(immutable)).hexdigest(),
                evidence_root=str(staging), imported_at=clock.now(),
                next_step="请同时切换 DATA_ROOT 和 DATABASE_URL 后启动服务。原目录保持不变，可作为回滚副本。")
            write_synced_file(payload / RECEIPT_FILENAME, canonical_json_bytes(result), sync)
            for key in sorted(inventory["directories"], key=lambda value: len(Path(value).parts), reverse=True):
                sync.sync_directory(payload / key)
            verify_lock()
            published_identity = (payload.stat().st_dev, payload.stat().st_ino)
            safety._publish_directory(payload, target)
            published = target
            sync.sync_directory(target.parent)
            with repository.database_transaction() as db:
                changed = db.execute("UPDATE metadata SET value='postgresql-v1' WHERE key='storage_backend' AND value=?", (_PENDING_BACKEND,)).rowcount
                _require(changed == 1, "数据库迁移标记发生变化，目标目录不会启用。", "IMPORT_VERIFICATION_FAILED")
                _require(_digest(db.execute("SELECT key,value FROM metadata")) == result["tables"]["metadata"]["target"],
                    "数据库的最终迁移标记校验失败。", "IMPORT_VERIFICATION_FAILED")
            (target / _BARRIER).unlink()
            chmod_no_follow(target, inventory["directories"]["."])
            sync.sync_directory(target)
            sync.sync_directory(target.parent)
            return result
    except BaseException as error:
        # The directory barrier and non-production backend marker fence every
        # failure after the SQL commit. Keep all files and database evidence.
        if published is not None:
            try:
                observed = safety.require_real_directory(published)
                _require((observed.st_dev, observed.st_ino) == published_identity,
                    "目标目录身份发生变化，请人工检查发布状态。", "PUBLICATION_UNCERTAIN")
                chmod_no_follow(published, 0o700)
                if not (published / _BARRIER).exists():
                    write_synced_file(published / _BARRIER, b"PostgreSQL import failed; do not use this directory.\n", sync)
                sync.sync_directory(published)
            except OSError:
                pass
        if repository is not None:
            try:
                with repository.database_transaction() as db:
                    db.execute("UPDATE metadata SET value=? WHERE key='storage_backend'", (_PENDING_BACKEND,))
            except Exception:
                pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        code = error.code if isinstance(error, safety.DataUpgradeError) else "IMPORT_FAILED"
        message = str(error) if isinstance(error, safety.DataUpgradeError) else "导入未完成，源目录保持不变；请保留暂存目录和目标数据库以便检查。"
        raise PostgresImportError(code, message, staging_root=staging) from error
    finally:
        if repository is not None:
            repository.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="将当前 SQLite 历史数据离线复制到新的 PostgreSQL 数据库和数据目录。")
    parser.add_argument("--source-data-root", type=Path, required=True)
    parser.add_argument("--target-data-root", type=Path, required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL", help="保存目标连接地址的环境变量名；不要在命令行填写密码。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = import_data_root(args.source_data_root, args.target_data_root,
            database_url_env=args.database_url_env, execute=args.execute)
    except safety.DataUpgradeError as error:
        print(json.dumps({"status": "FAILED", "code": error.code, "message": str(error),
            "staging_root": str(error.staging_root) if error.staging_root is not None else None}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
