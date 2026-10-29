"""Conversation-owned native sessions, independent of follow-up retention."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from problem_locator.agent.models import AgentStoreError
from problem_locator.storage.atomic import require_real_directory
from problem_locator.storage.database import dialect_for, lock_conversation, lock_key, table_columns, table_names
from problem_locator.storage.paths import ensure_no_symlink_ancestors

from .snapshots import InputChanged

_TABLES = {"agent_followup_session_metadata", "agent_followup_sessions"}
_COLUMNS = {"conversation_id", "session_id", "active_followup_id", "last_followup_id", "last_order",
    "report_run_id", "report_sha256"}
_CREATE = """CREATE TABLE agent_followup_sessions (
    conversation_id TEXT PRIMARY KEY, session_id TEXT, active_followup_id TEXT,
    last_followup_id TEXT, last_order BIGINT NOT NULL DEFAULT 0, report_run_id TEXT, report_sha256 TEXT)"""


def _one(db, query, args=()):
    cursor = db.execute(query, args)
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def _unavailable(message):
    return AgentStoreError("AGENT_FOLLOWUP_SESSION_UNAVAILABLE", message, 409)


class FollowupSessions:
    def __init__(self, followup_store):
        self.store = followup_store
        self.repository = followup_store.repository
        with self.repository.database_transaction() as db:
            lock_key(db, "schema", "agent-followup-sessions")
            existing = table_names(db).intersection(_TABLES)
            if not existing:
                db.execute("CREATE TABLE agent_followup_session_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute(_CREATE)
                db.execute("INSERT INTO agent_followup_session_metadata(key,value) VALUES ('storage_version','1')")
            else:
                marker = None if "agent_followup_session_metadata" not in existing else db.execute(
                    "SELECT value FROM agent_followup_session_metadata WHERE key='storage_version'").fetchone()
                if (existing != _TABLES or marker is None or marker[0] != "1"
                        or not _COLUMNS.issubset(table_columns(db, "agent_followup_sessions"))):
                    raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "追问会话数据版本不受支持，请保留原目录。", 503)

    @staticmethod
    def _get(db, cid):
        return _one(db, "SELECT s.*,t.status AS previous_status FROM agent_followup_sessions s "
            "LEFT JOIN agent_followup_tasks t ON t.followup_id=s.active_followup_id "
            "WHERE s.conversation_id=?", (cid,))

    def get(self, conversation_id):
        with self.repository.database_read() as db:
            return self._get(db, conversation_id)

    def _task(self, db, task, status="RUNNING"):
        row = _one(db, f"SELECT t.*,{dialect_for(db).order_column('t')} AS task_order "
            "FROM agent_followup_tasks t WHERE t.followup_id=? AND t.conversation_id=? AND t.run_id=?",
            (task["followup_id"], task["conversation_id"], task["run_id"]))
        if (row is None or row["status"] != status or row["epoch"] != self.store.epoch
                or task["epoch"] != self.store.epoch
                or not self.store.alive(db, row["conversation_id"], row["run_id"])):
            raise _unavailable("本次追问已停止或失效，不能继续更新会话。")
        return row

    def begin(self, task):
        """Record launch before invoking the CLI and return its previous checkpoint."""
        cid = task["conversation_id"]
        with self.repository.database_transaction() as db:
            lock_conversation(db, cid)
            self._task(db, task)
            previous = self._get(db, cid)
            if previous is None:
                db.execute("INSERT INTO agent_followup_sessions(conversation_id) VALUES (?)", (cid,))
                previous = self._get(db, cid)
            elif previous["active_followup_id"] == task["followup_id"]:
                raise _unavailable("本次追问已经启动，不能重复执行。")
            elif previous["active_followup_id"] is not None and previous["session_id"] is None:
                raise _unavailable("上次追问未能保存会话标识，暂时无法恢复上下文。")
            db.execute("UPDATE agent_followup_sessions SET active_followup_id=? WHERE conversation_id=?",
                (task["followup_id"], cid))
            return previous

    def capture_session(self, task, session_id):
        """Bind the CLI init identity; stale callbacks must stop the invocation."""
        try:
            session_id = str(uuid.UUID(session_id))
        except (ValueError, TypeError, AttributeError):
            raise _unavailable("模型返回的会话标识无效，无法保存上下文。") from None
        cid = task["conversation_id"]
        with self.repository.database_transaction() as db:
            lock_conversation(db, cid)
            self._task(db, task)
            current = self._get(db, cid)
            if current is None or current["active_followup_id"] != task["followup_id"]:
                raise _unavailable("本次追问的会话已失效，不能保存迟到的结果。")
            if current["session_id"] not in {None, session_id}:
                raise _unavailable("模型返回的会话标识与原会话不一致。")
            db.execute("UPDATE agent_followup_sessions SET session_id=? WHERE conversation_id=?", (session_id, cid))

    def checkpoint(self, db, task):
        """Called inside the transaction that has accepted a COMPLETED answer."""
        current = self._get(db, task["conversation_id"])
        if current is None or current["active_followup_id"] != task["followup_id"]:
            return
        row = self._task(db, task, "COMPLETED")
        if current["session_id"] is None:
            raise _unavailable("本次追问未能保存会话，回答尚未提交。")
        report = db.execute("SELECT report_sha256 FROM agent_followup_snapshots WHERE run_id=?",
            (row["run_id"],)).fetchone()
        db.execute("UPDATE agent_followup_sessions SET last_followup_id=?,last_order=?,report_run_id=?,report_sha256=? "
            "WHERE conversation_id=?", (row["followup_id"], row["task_order"], row["run_id"], report[0], row["conversation_id"]))

    def history(self, task, after_order=0):
        with self.repository.database_read() as db:
            order = dialect_for(db).order_column("t")
            return [json.loads(row[0]) for row in db.execute(
                "SELECT t.body FROM agent_followup_tasks t WHERE t.conversation_id=? "
                f"AND {order}>? AND {order}<(SELECT {dialect_for(db).order_column()} "
                f"FROM agent_followup_tasks WHERE followup_id=?) ORDER BY {order}",
                (task["conversation_id"], after_order, task["followup_id"]))]

    def historical_reports(self, task, history=None):
        """Read discussed report versions, optionally limited to incremental turns."""
        run_ids = None if history is None else tuple(dict.fromkeys(item["run_id"] for item in history))
        if run_ids == ():
            return []
        clause = "" if run_ids is None else " AND t.run_id IN (" + ",".join("?" for _ in run_ids) + ")"
        with self.repository.database_read() as db:
            order = dialect_for(db).order_column
            rows = db.execute("SELECT DISTINCT t.run_id,s.source_json,s.report_sha256,s.case_id,s.source_job_id "
                "FROM agent_followup_tasks t LEFT JOIN agent_followup_snapshots s "
                "ON s.run_id=t.run_id AND s.conversation_id=t.conversation_id "
                f"WHERE t.conversation_id=? AND t.run_id<>? AND {order('t')}<"
                f"(SELECT {order()} FROM agent_followup_tasks WHERE followup_id=?)" + clause + " ORDER BY t.run_id",
                (task["conversation_id"], task["run_id"], task["followup_id"], *(run_ids or ())))
            reports = []
            for rid, raw, digest, case_id, job_id in rows:
                if raw is None:
                    raise InputChanged("历史报告资料缺失，无法恢复本次上下文。")
                source = self.store._source({"source_json": raw})
                if (source.case_id != case_id or source.source_job_id != job_id or source.report_sha256 != digest
                        or hashlib.sha256(source.report_markdown.encode("utf-8")).hexdigest() != digest):
                    raise InputChanged("历史报告校验失败，无法恢复本次上下文。")
                reports.append({"run_id": rid, "report_sha256": digest,
                    "original_problem": source.problem_text, "original_report": source.report_markdown})
            return reports

    @staticmethod
    def relative_root(conversation_id):
        if str(uuid.UUID(conversation_id)) != conversation_id:
            raise ValueError("会话标识无效。")
        return f"resources/followup-sessions/{conversation_id}"

    def config_dir(self, conversation_id) -> Path:
        root = self.repository.layout.data_root
        config = root / self.relative_root(conversation_id) / "config"
        ensure_no_symlink_ancestors(root, config)
        for directory in (config.parent.parent, config.parent, config):
            directory.mkdir(exist_ok=True, mode=0o700)
            require_real_directory(directory)
            directory.chmod(0o700)
        return config

    @staticmethod
    def purge(db, conversation_id):
        lock_conversation(db, conversation_id)
        db.execute("DELETE FROM agent_followup_sessions WHERE conversation_id=?", (conversation_id,))
