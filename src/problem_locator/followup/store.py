"""Additive follow-up storage on the repository's FULL-WAL connection.

Every mutation stays in agent_followup_* tables. Cleanup hooks accept the
caller's transaction so revocation and the conversation tombstone are atomic.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from problem_locator.agent.models import AgentStoreError

from .context import build_prompt
from .models import (ACTIVE, MAX_TURNS, FollowupEvent, FollowupEventBatch, FollowupFailure,
    FollowupItem, FollowupReceipt, FollowupSource, FollowupStopReceipt)

_TABLES = ("agent_followup_metadata", "agent_followup_snapshots", "agent_followup_tasks",
    "agent_followup_events", "agent_followup_stops")
_SCHEMA = (
    "CREATE TABLE agent_followup_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE agent_followup_snapshots (run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,case_id TEXT NOT NULL,source_job_id TEXT NOT NULL,report_sha256 TEXT NOT NULL,source_json TEXT NOT NULL,status TEXT NOT NULL,manifest_json TEXT,error_code TEXT,reserved_bytes INTEGER NOT NULL DEFAULT 0,last_event_id INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE INDEX agent_followup_snapshots_work ON agent_followup_snapshots(status,created_at)",
    "CREATE INDEX agent_followup_snapshots_conversation ON agent_followup_snapshots(conversation_id)",
    "CREATE INDEX agent_followup_snapshots_job ON agent_followup_snapshots(source_job_id,status)",
    "CREATE TABLE agent_followup_tasks (followup_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,ordinal INTEGER NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL,workspace_id TEXT NOT NULL,epoch TEXT,UNIQUE(conversation_id,request_id),UNIQUE(run_id,ordinal))",
    "CREATE INDEX agent_followup_tasks_work ON agent_followup_tasks(status,run_id,ordinal)",
    "CREATE UNIQUE INDEX agent_followup_one_active ON agent_followup_tasks(conversation_id) WHERE status IN ('QUEUED','RUNNING','CANCELLING')",
    "CREATE INDEX agent_followup_tasks_workspace ON agent_followup_tasks(workspace_id,status)",
    "CREATE TABLE agent_followup_events (conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,sequence INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(run_id,sequence))",
    "CREATE TABLE agent_followup_stops (conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,request_id TEXT NOT NULL,followup_id TEXT NOT NULL,PRIMARY KEY(conversation_id,request_id))",
)
_FAILURES = {
    "AGENT_FOLLOWUP_FAILED": "本次追问未能完成，可以重新发送。",
    "AGENT_FOLLOWUP_INTERRUPTED": "服务已重启，本次追问已中断，可以重新发送。",
    "AGENT_FOLLOWUP_INPUT_CHANGED": "追问资料校验失败，本次回答未发布。",
}


def _json(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _one(db, query, values=()):
    cursor = db.execute(query, values)
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def timestamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def fail(code, message, status=409):
    return AgentStoreError("AGENT_FOLLOWUP_" + code, message, status)


class FollowupStore:
    def __init__(self, repository, clock=None):
        self.repository, self.clock = repository, clock
        self.epoch = str(uuid.uuid4())
        self.notify = lambda: None
        with repository.database_transaction() as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}.intersection(_TABLES)
            if existing:
                marker = None if "agent_followup_metadata" not in existing else db.execute(
                    "SELECT value FROM agent_followup_metadata WHERE key='storage_version'").fetchone()
                if existing != set(_TABLES) or marker is None or marker[0] != "1":
                    raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "报告追问数据版本不受支持，请保留原目录。", 503)
            else:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("INSERT INTO agent_followup_metadata VALUES ('storage_version','1')")

    def now(self):
        return timestamp(self.clock.now() if self.clock is not None else datetime.now(timezone.utc))

    def expired(self, source):
        cutoff = datetime.fromisoformat(self.now().replace("Z", "+00:00")) - timedelta(days=7)
        return datetime.fromisoformat(source.occurred_at.replace("Z", "+00:00")) <= cutoff

    @staticmethod
    def alive(db, cid, rid):
        return db.execute("SELECT 1 FROM agent_conversations c JOIN agent_conversation_runs r USING(conversation_id) "
            "WHERE c.conversation_id=? AND r.run_id=? AND c.deleted_at IS NULL", (cid, rid)).fetchone() is not None

    @staticmethod
    def scope(db, cid, rid, owner):
        row = db.execute("SELECT owner_key,deleted_at FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
        if owner is None or row is None or row[0] != owner or row[1] is not None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        if not db.execute("SELECT 1 FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (cid, rid)).fetchone():
            raise AgentStoreError("AGENT_RUN_NOT_FOUND", "本次诊断不存在。", 404)

    def require_scope(self, cid, rid, owner):
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)

    @staticmethod
    def _source(row):
        return FollowupSource(**json.loads(row["source_json"]))

    def snapshot(self, rid):
        with self.repository.database_read() as db:
            return _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))

    def report_run_ids(self, cid):
        """Capture published runs even when a legacy caller has opened another."""
        with self.repository.database_read() as db:
            return [row[0] for row in db.execute(
                "SELECT r.run_id FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                "WHERE r.conversation_id=? AND c.deleted_at IS NULL "
                "AND json_extract(r.body,'$.report_available')=1 ORDER BY r.ordinal", (cid,))]

    def _register_source(self, db, cid, rid, source, *, enqueue=False):
        previous = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
        if previous is not None:
            if (previous["conversation_id"], previous["source_job_id"], previous["report_sha256"]) != (
                    cid, source.source_job_id, source.report_sha256):
                raise fail("SOURCE_CHANGED", "报告来源发生变化，请刷新会话。")
            if enqueue and previous["status"] == "UNAVAILABLE" and previous["error_code"] is None:
                db.execute("UPDATE agent_followup_snapshots SET status='PENDING',updated_at=? WHERE run_id=?", (self.now(), rid))
            return
        if db.execute("SELECT count(*) FROM agent_followup_snapshots").fetchone()[0] >= 10_000:
            raise fail("LIMIT_EXCEEDED", "追问记录已达到上限，请稍后重试。", 429)
        now = self.now()
        db.execute("INSERT INTO agent_followup_snapshots(run_id,conversation_id,case_id,source_job_id,report_sha256,source_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, cid, source.case_id, source.source_job_id, source.report_sha256, _json(asdict(source)),
                "PENDING" if enqueue else "UNAVAILABLE", now, now))

    def enqueue_snapshot(self, cid, rid, source):
        with self.repository.database_transaction() as db:
            if not self.alive(db, cid, rid) or self.expired(source):
                return False
            self._register_source(db, cid, rid, source, enqueue=True)
        self.notify()
        return True

    def claim_snapshot(self):
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT s.* FROM agent_followup_snapshots s JOIN agent_conversations c USING(conversation_id) "
                "JOIN agent_conversation_runs r ON r.run_id=s.run_id WHERE s.status='PENDING' AND c.deleted_at IS NULL ORDER BY s.created_at,s.run_id LIMIT 1")
            if row is None:
                return None
            if self.expired(self._source(row)):
                db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code='EXPIRED',updated_at=? WHERE run_id=?", (self.now(), row["run_id"]))
                return None
            db.execute("UPDATE agent_followup_snapshots SET status='BUILDING',updated_at=? WHERE run_id=?", (self.now(), row["run_id"]))
            row["status"] = "BUILDING"
            return row

    def reserve_snapshot(self, rid, size, per_report, total):
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
            if row is None or row["status"] != "BUILDING" or not self.alive(db, row["conversation_id"], rid):
                raise ValueError("snapshot was revoked")
            allocated = db.execute("SELECT coalesce(sum(reserved_bytes),0) FROM agent_followup_snapshots WHERE run_id<>?", (rid,)).fetchone()[0]
            if size > per_report or allocated + size > total:
                raise ValueError("snapshot exceeds its byte budget")
            db.execute("UPDATE agent_followup_snapshots SET reserved_bytes=? WHERE run_id=?", (size, rid))

    def finish_snapshot(self, rid, manifest):
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
            if row is None or row["status"] != "BUILDING" or not self.alive(db, row["conversation_id"], rid):
                return False
            db.execute("UPDATE agent_followup_snapshots SET status='READY',manifest_json=?,updated_at=? WHERE run_id=?", (_json(manifest), self.now(), rid))
        return True

    def fail_snapshot(self, rid, code="SNAPSHOT_UNAVAILABLE"):
        with self.repository.database_transaction() as db:
            db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code=?,updated_at=? WHERE run_id=? AND status IN ('PENDING','BUILDING')", (code, self.now(), rid))

    @staticmethod
    def _fingerprint(rid, text):
        return hashlib.sha256(_json([rid, text]).encode()).hexdigest()

    def request(self, cid, rid, request_id, text, owner):
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND request_id=?", (cid, request_id))
            if row is None:
                return None
            if row["fingerprint"] != self._fingerprint(rid, text):
                raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
            return FollowupReceipt.model_validate_json(row["receipt"])

    def _append(self, db, cid, item, kind):
        rid = item.run_id
        sequence = db.execute("SELECT last_event_id FROM agent_followup_snapshots WHERE run_id=?", (rid,)).fetchone()[0] + 1
        event = FollowupEvent(sequence=sequence, conversation_id=cid, run_id=rid,
            followup_id=item.followup_id, type=kind, created_at=self.now(), data=item)
        db.execute("INSERT INTO agent_followup_events VALUES (?,?,?,?)", (cid, rid, sequence, _json(event)))
        db.execute("UPDATE agent_followup_snapshots SET last_event_id=? WHERE run_id=?", (sequence, rid))
        return sequence

    @staticmethod
    def _history(db, rid, before=None):
        clause, args = ("", (rid,)) if before is None else (" AND ordinal<?", (rid, before))
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM agent_followup_tasks WHERE run_id=?" + clause + " ORDER BY ordinal", args)]

    def submit(self, cid, rid, request, source, owner, *, logs_supported=True):
        with self.repository.database_transaction() as db:
            self.scope(db, cid, rid, owner)
            previous = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND request_id=?", (cid, request.request_id))
            fingerprint = self._fingerprint(rid, request.text)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                return FollowupReceipt.model_validate_json(previous["receipt"])
            if self.expired(source):
                raise fail("EXPIRED", "报告已过保留期，请新建对话。")
            if self.active(db, cid) is not None:
                raise fail("BUSY", "本会话有追问正在处理，请等待完成或先停止。")
            history = self._history(db, rid)
            if len(history) >= MAX_TURNS:
                raise fail("LIMIT_EXCEEDED", "本报告的追问次数已达到上限，请新建对话。", 429)
            self._register_source(db, cid, rid, source)
            snapshot = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
            mode = "REPORT_AND_LOGS" if logs_supported and snapshot["status"] == "READY" and source.has_logs else "REPORT_ONLY"
            try:
                build_prompt(source, history, request.text, mode, read_search=logs_supported)
            except ValueError:
                raise fail("CONTEXT_LIMIT", "追问上下文已达到上限，请新建对话。", 413) from None
            fid, now = str(uuid.uuid4()), self.now()
            item = FollowupItem(followup_id=fid, run_id=rid, request_id=request.request_id,
                ordinal=len(history) + 1, status="QUEUED", context_mode=mode, text=request.text, created_at=now, updated_at=now)
            sequence = self._append(db, cid, item, "followup.accepted")
            receipt = FollowupReceipt(conversation_id=cid, run_id=rid, followup_id=fid,
                request_id=request.request_id, event_id=sequence)
            db.execute("INSERT INTO agent_followup_tasks VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (fid, cid, rid, request.request_id, fingerprint, item.ordinal, "QUEUED", _json(item), _json(receipt), fid))
        self.notify()
        return receipt

    @staticmethod
    def active(db, cid):
        row = db.execute("SELECT body FROM agent_followup_tasks WHERE conversation_id=? AND status IN ('QUEUED','RUNNING','CANCELLING') LIMIT 1", (cid,)).fetchone()
        return None if row is None else FollowupItem.model_validate_json(row[0])

    @staticmethod
    def _cursor(cid, rid, ordinal):
        return base64.urlsafe_b64encode(_json([cid, rid, ordinal]).encode()).decode().rstrip("=")

    def page(self, cid, rid, owner, *, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise fail("INVALID_CURSOR", "每页追问数量应为 1 至 100。", 400)
        before = 2**63 - 1
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if not isinstance(value, list) or len(value) != 3 or value[:2] != [cid, rid] or type(value[2]) is not int or value[2] < 1:
                    raise ValueError()
                before = value[2]
            except (ValueError, TypeError, UnicodeError):
                raise fail("INVALID_CURSOR", "追问分页游标无效。", 400) from None
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)
            rows = db.execute("SELECT body,ordinal FROM agent_followup_tasks WHERE run_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT ?", (rid, before, limit + 1)).fetchall()
            snapshot = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
            return {"items": [FollowupItem.model_validate_json(row[0]) for row in reversed(rows[:limit])],
                "next_cursor": self._cursor(cid, rid, rows[limit - 1][1]) if len(rows) > limit else None,
                "active_followup": self.active(db, cid), "last_event_id": snapshot["last_event_id"] if snapshot else 0,
                "snapshot": snapshot, "count": db.execute("SELECT count(*) FROM agent_followup_tasks WHERE run_id=?", (rid,)).fetchone()[0]}

    def events(self, cid, rid, owner, after=0, limit=20):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise fail("INVALID_CURSOR", "追问事件游标无效。", 400)
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)
            head = db.execute("SELECT last_event_id FROM agent_followup_snapshots WHERE run_id=?", (rid,)).fetchone()
            last = 0 if head is None else head[0]
            if after > last:
                raise fail("INVALID_CURSOR", "追问事件游标超出范围。")
            events = [FollowupEvent.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_followup_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (rid, after, limit))]
            busy = db.execute("SELECT 1 FROM agent_followup_tasks WHERE run_id=? AND status IN ('QUEUED','RUNNING','CANCELLING')", (rid,)).fetchone()
            return FollowupEventBatch(events=events, stream_closed=busy is None and (events[-1].sequence if events else after) >= last)

    def _transition(self, db, row, status, *, answer=None, code=None, emit=True):
        old = FollowupItem.model_validate_json(row["body"])
        if old.status == status:
            return old
        failure = None if code is None else FollowupFailure(code=code, message=_FAILURES[code])
        item = FollowupItem.model_validate({**old.model_dump(), "status": status,
            "answer_markdown": answer, "failure": None if failure is None else failure.model_dump(), "updated_at": self.now()})
        db.execute("UPDATE agent_followup_tasks SET status=?,body=? WHERE followup_id=?", (status, _json(item), item.followup_id))
        if emit and self.alive(db, row["conversation_id"], row["run_id"]):
            self._append(db, row["conversation_id"], item, "followup.updated")
        return item

    def claim_task(self):
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT t.* FROM agent_followup_tasks t JOIN agent_conversations c USING(conversation_id) "
                "JOIN agent_conversation_runs r ON r.run_id=t.run_id WHERE t.status='QUEUED' AND c.deleted_at IS NULL ORDER BY t.rowid LIMIT 1")
            if row is None:
                return None
            source = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (row["run_id"],))
            if source is None or self.expired(self._source(source)):
                self._transition(db, row, "CANCELLED")
                return None
            self._transition(db, row, "RUNNING")
            db.execute("UPDATE agent_followup_tasks SET epoch=? WHERE followup_id=?", (self.epoch, row["followup_id"]))
            row.update(status="RUNNING", epoch=self.epoch)
            return row

    def task_context(self, task):
        with self.repository.database_read() as db:
            source = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (task["run_id"],))
            if source is None:
                raise ValueError("followup source was removed")
            return self._source(source), self._history(db, task["run_id"], task["ordinal"]), source

    def execution_allowed(self, fid):
        with self.repository.database_read() as db:
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
            return row is not None and row["epoch"] == self.epoch and row["status"] == "RUNNING" and self.alive(db, row["conversation_id"], row["run_id"])

    def finish_task(self, fid, *, answer=None, code=None, interrupted=False):
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
            if row is None or row["status"] not in {"RUNNING", "CANCELLING"} or row["epoch"] != self.epoch:
                return False
            if row["status"] == "CANCELLING" or not self.alive(db, row["conversation_id"], row["run_id"]):
                self._transition(db, row, "CANCELLED")
            elif interrupted:
                self._transition(db, row, "INTERRUPTED", code="AGENT_FOLLOWUP_INTERRUPTED")
            elif code is not None:
                self._transition(db, row, "FAILED", code=code)
            else:
                self._transition(db, row, "COMPLETED", answer=answer)
        self.notify()
        return True

    def stop(self, cid, rid, fid, request_id, owner):
        with self.repository.database_transaction() as db:
            self.scope(db, cid, rid, owner)
            previous = db.execute("SELECT run_id,followup_id FROM agent_followup_stops WHERE conversation_id=? AND request_id=?", (cid, request_id)).fetchone()
            if previous is not None and tuple(previous) != (rid, fid):
                raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一停止请求不能改为其他追问。", 409)
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND run_id=? AND followup_id=?", (cid, rid, fid))
            if row is None:
                raise fail("NOT_FOUND", "这条追问不存在。", 404)
            if previous is None:
                if db.execute("SELECT count(*) FROM agent_followup_stops WHERE run_id=?", (rid,)).fetchone()[0] >= 256:
                    raise fail("LIMIT_EXCEEDED", "停止请求已达到上限。", 429)
                db.execute("INSERT INTO agent_followup_stops VALUES (?,?,?,?)", (cid, rid, request_id, fid))
            if row["status"] == "QUEUED":
                row["status"] = self._transition(db, row, "CANCELLED").status
            elif row["status"] == "RUNNING":
                row["status"] = self._transition(db, row, "CANCELLING").status
            status = row["status"] if row["status"] in {"CANCELLED", "CANCELLING"} else "ALREADY_FINISHED"
            sequence = db.execute("SELECT last_event_id FROM agent_followup_snapshots WHERE run_id=?", (rid,)).fetchone()[0]
            receipt = FollowupStopReceipt(conversation_id=cid, run_id=rid, followup_id=fid,
                request_id=request_id, status=status, event_id=sequence)
        self.notify()
        return receipt

    def recover(self, *, enabled=True):
        with self.repository.database_transaction() as db:
            for row in [_one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
                    for (fid,) in db.execute("SELECT followup_id FROM agent_followup_tasks WHERE status IN ('QUEUED','RUNNING','CANCELLING')").fetchall()]:
                source = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (row["run_id"],))
                fresh = source is not None and not self.expired(self._source(source))
                if row["status"] == "QUEUED" and enabled and fresh and self.alive(db, row["conversation_id"], row["run_id"]):
                    continue
                cancelled = row["status"] != "RUNNING" or not self.alive(db, row["conversation_id"], row["run_id"])
                self._transition(db, row, "CANCELLED" if cancelled else "INTERRUPTED",
                    code=None if cancelled else "AGENT_FOLLOWUP_INTERRUPTED")
            db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code='SNAPSHOT_INTERRUPTED',updated_at=? WHERE status='BUILDING'", (self.now(),))
            if not enabled:
                db.execute("UPDATE agent_followup_snapshots SET status='UNAVAILABLE',error_code='DISABLED',updated_at=? WHERE status='PENDING'", (self.now(),))

    def revoke(self, db, cid):
        for (fid,) in db.execute("SELECT followup_id FROM agent_followup_tasks WHERE conversation_id=? AND status IN ('QUEUED','RUNNING','CANCELLING')", (cid,)).fetchall():
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
            self._transition(db, row, "CANCELLED" if row["status"] == "QUEUED" else "CANCELLING", emit=False)
        db.execute("UPDATE agent_followup_snapshots SET status='UNAVAILABLE',error_code='REVOKED' WHERE conversation_id=?", (cid,))

    @staticmethod
    def busy(db, cid, run_id=None):
        tail, args = ("", (cid,)) if run_id is None else (" AND run_id=?", (cid, run_id))
        return (db.execute("SELECT 1 FROM agent_followup_tasks WHERE conversation_id=? AND status IN ('QUEUED','RUNNING','CANCELLING')" + tail + " LIMIT 1", args).fetchone() is not None
            or db.execute("SELECT 1 FROM agent_followup_snapshots WHERE conversation_id=? AND status IN ('PENDING','BUILDING')" + tail + " LIMIT 1", args).fetchone() is not None)

    @staticmethod
    def purge(db, cid, run_id=None):
        tail, args = ("", (cid,)) if run_id is None else (" AND run_id=?", (cid, run_id))
        for table in ("agent_followup_events", "agent_followup_stops", "agent_followup_tasks", "agent_followup_snapshots"):
            db.execute(f"DELETE FROM {table} WHERE conversation_id=?" + tail, args)

    @staticmethod
    def workspace_ids(db, cid, run_id=None):
        tail, args = ("", (cid,)) if run_id is None else (" AND run_id=?", (cid, run_id))
        return [row[0] for row in db.execute("SELECT workspace_id FROM agent_followup_tasks WHERE conversation_id=?" + tail, args)]

    def workspace_in_use(self, workspace_id):
        with self.repository.database_read() as db:
            return (db.execute("SELECT 1 FROM agent_followup_snapshots WHERE source_job_id=? AND status IN ('PENDING','BUILDING') LIMIT 1", (workspace_id,)).fetchone() is not None
                or db.execute("SELECT 1 FROM agent_followup_tasks WHERE workspace_id=? AND status IN ('QUEUED','RUNNING','CANCELLING') LIMIT 1", (workspace_id,)).fetchone() is not None)
