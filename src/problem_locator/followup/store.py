"""Additive follow-up storage on the repository's transactional database.

Completed corrections enqueue their memory check in the same transaction.
Cleanup hooks keep revocation and the conversation tombstone atomic.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import timedelta

from problem_locator.agent.models import AgentStoreError
from problem_locator.diagnostics import log_event
from problem_locator.storage.database import (dialect_for, lock_conversation, lock_key,
    table_columns, table_names, try_lock_conversation)
from problem_locator.timestamps import format_timestamp, now_timestamp, parse_timestamp

from .context import build_prompt
from .models import (ACTIVE, MAX_TEXT_BYTES, FollowupEvent, FollowupEventBatch, FollowupFailure,
    FollowupItem, FollowupReceipt, FollowupSource, FollowupStopReceipt)

_TABLES = ("agent_followup_metadata", "agent_followup_snapshots", "agent_followup_tasks",
    "agent_followup_events", "agent_followup_stops")
_SCHEMA = (
    "CREATE TABLE agent_followup_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE agent_followup_snapshots (run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,case_id TEXT NOT NULL,source_job_id TEXT NOT NULL,report_sha256 TEXT NOT NULL,source_json TEXT NOT NULL,status TEXT NOT NULL,manifest_json TEXT,error_code TEXT,reserved_bytes BIGINT NOT NULL DEFAULT 0,last_event_id BIGINT NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE INDEX agent_followup_snapshots_work ON agent_followup_snapshots(status,created_at)",
    "CREATE INDEX agent_followup_snapshots_conversation ON agent_followup_snapshots(conversation_id)",
    "CREATE INDEX agent_followup_snapshots_job ON agent_followup_snapshots(source_job_id,status)",
    "CREATE TABLE agent_followup_tasks (followup_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,ordinal BIGINT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL,workspace_id TEXT NOT NULL,epoch TEXT,correction_json TEXT NOT NULL DEFAULT '{}',UNIQUE(conversation_id,request_id),UNIQUE(run_id,ordinal))",
    "CREATE INDEX agent_followup_tasks_work ON agent_followup_tasks(status,run_id,ordinal)",
    "CREATE UNIQUE INDEX agent_followup_one_active ON agent_followup_tasks(conversation_id) WHERE status IN ('QUEUED','RUNNING','CANCELLING')",
    "CREATE INDEX agent_followup_tasks_workspace ON agent_followup_tasks(workspace_id,status)",
    "CREATE TABLE agent_followup_events (conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,sequence BIGINT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(run_id,sequence))",
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
    return format_timestamp(parse_timestamp(str(value)))


def fail(code, message, status=409):
    return AgentStoreError("AGENT_FOLLOWUP_" + code, message, status)


class FollowupStore:
    def __init__(self, repository, clock=None, *, retention_days=15, memory_store=None):
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("retention_days 必须是 1–3650 之间的整数")
        self.retention_days = retention_days
        self.repository, self.clock = repository, clock
        self.memory_store = memory_store
        self.epoch = str(uuid.uuid4())
        self.notify = lambda: None
        self.last_claim_empty = False
        with repository.database_transaction() as db:
            lock_key(db, "schema", "agent-followup")
            existing = table_names(db).intersection(_TABLES)
            if existing:
                marker = None if "agent_followup_metadata" not in existing else db.execute(
                    "SELECT value FROM agent_followup_metadata WHERE key='storage_version'").fetchone()
                if existing != set(_TABLES) or marker is None or marker[0] not in {"1", "2"}:
                    raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "报告追问数据版本不受支持，请保留原目录。", 503)
                if marker[0] == "1":
                    db.execute("ALTER TABLE agent_followup_tasks ADD COLUMN correction_json TEXT NOT NULL DEFAULT '{}'")
                    db.execute("UPDATE agent_followup_metadata SET value='2' WHERE key='storage_version'")
                if "correction_json" not in table_columns(db, "agent_followup_tasks"):
                    raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "报告追问数据结构不完整，请保留原目录。", 503)
            else:
                for statement in _SCHEMA:
                    if dialect_for(db).postgres and statement.startswith("CREATE TABLE agent_followup_tasks "):
                        statement = statement[:-1] + ",storage_order BIGINT GENERATED ALWAYS AS IDENTITY)"
                    db.execute(statement)
                db.execute("INSERT INTO agent_followup_metadata(key,value) VALUES ('storage_version','2')")

        from .sessions import FollowupSessions
        self.sessions = FollowupSessions(self)

    def now(self):
        return timestamp(self.clock.now()) if self.clock is not None else now_timestamp()

    def expired(self, source):
        cutoff = parse_timestamp(self.now()) - timedelta(days=self.retention_days)
        with self.repository.database_read() as db:
            row = db.execute("SELECT body FROM agent_conversation_runs WHERE case_id=?", (source.case_id,)).fetchone()
        if row is not None:
            run = json.loads(row[0])
            if run.get("resources_expired", False):
                return True
            if run["status"] not in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
                return False
            return parse_timestamp(run.get("completed_at", run["updated_at"])) <= cutoff
        return parse_timestamp(source.occurred_at) <= cutoff

    @staticmethod
    def resources_expired(db, cid, rid):
        row = db.execute("SELECT body FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (cid, rid)).fetchone()
        return row is None or bool(json.loads(row[0]).get("resources_expired", False))

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
            available = dialect_for(db).json_int("r.body", "$.report_available")
            expired = dialect_for(db).json_int("r.body", "$.resources_expired")
            return [row[0] for row in db.execute(
                "SELECT r.run_id FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                "WHERE r.conversation_id=? AND c.deleted_at IS NULL "
                f"AND {available}=1 AND coalesce({expired},0)=0 ORDER BY r.ordinal", (cid,))]

    def _register_source(self, db, cid, rid, source, *, enqueue=False):
        previous = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
        if previous is not None:
            if (previous["conversation_id"], previous["source_job_id"], previous["report_sha256"]) != (
                    cid, source.source_job_id, source.report_sha256):
                raise fail("SOURCE_CHANGED", "报告来源发生变化，请刷新会话。")
            saved = json.loads(previous["source_json"])
            if not saved.get("skill_name") and source.skill_name:
                # The current published result proves the old Skill identity.
                # Never substitute the deployment's current Skill setting.
                saved["skill_name"] = source.skill_name
                db.execute("UPDATE agent_followup_snapshots SET source_json=? WHERE run_id=?", (_json(saved), rid))
            if enqueue and previous["status"] == "UNAVAILABLE" and previous["error_code"] is None:
                db.execute("UPDATE agent_followup_snapshots SET status='PENDING',updated_at=? WHERE run_id=?", (self.now(), rid))
            return
        # Serialize only allocations competing for this quota. Unrelated
        # conversations can still read, finish tasks and append events.
        lock_key(db, "quota", "followup-snapshot-count")
        if db.execute("SELECT count(*) FROM agent_followup_snapshots WHERE status IN ('PENDING','BUILDING','READY') OR reserved_bytes>0").fetchone()[0] >= 10_000:
            raise fail("LIMIT_EXCEEDED", "追问记录已达到上限，请稍后重试。", 429)
        now = self.now()
        db.execute("INSERT INTO agent_followup_snapshots(run_id,conversation_id,case_id,source_job_id,report_sha256,source_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, cid, source.case_id, source.source_job_id, source.report_sha256, _json(asdict(source)),
                "PENDING" if enqueue else "UNAVAILABLE", now, now))

    def enqueue_snapshot(self, cid, rid, source):
        with self.repository.database_transaction() as db:
            lock_conversation(db, cid)
            if not self.alive(db, cid, rid) or self.resources_expired(db, cid, rid) or self.expired(source):
                return False
            self._register_source(db, cid, rid, source, enqueue=True)
        self.notify()
        return True

    def claim_snapshot(self):
        with self.repository.database_transaction() as db:
            dialect = dialect_for(db)
            suffix = " FOR UPDATE OF s SKIP LOCKED" if dialect.postgres else ""
            row = _one(db, "SELECT s.* FROM agent_followup_snapshots s JOIN agent_conversations c USING(conversation_id) "
                "JOIN agent_conversation_runs r ON r.run_id=s.run_id WHERE s.status='PENDING' AND c.deleted_at IS NULL "
                f"ORDER BY {dialect.timestamp('s.created_at')},s.run_id LIMIT 1" + suffix)
            if row is None:
                return None
            if not try_lock_conversation(db, row["conversation_id"]) or not self.alive(db, row["conversation_id"], row["run_id"]):
                return None
            if self.expired(self._source(row)):
                db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code='EXPIRED',updated_at=? WHERE run_id=?", (self.now(), row["run_id"]))
                return None
            db.execute("UPDATE agent_followup_snapshots SET status='BUILDING',updated_at=? WHERE run_id=?", (self.now(), row["run_id"]))
            row["status"] = "BUILDING"
            return row

    @staticmethod
    def _locked_row(db, table, column, identity):
        row = db.execute(f"SELECT conversation_id FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if row is None:
            return None
        lock_conversation(db, row[0])
        # The lookup above may predate another transaction's deletion or
        # transition. Reload only after obtaining the conversation lock.
        return _one(db, f"SELECT * FROM {table} WHERE {column}=?", (identity,))

    def reserve_snapshot(self, rid, size, per_report, total):
        with self.repository.database_transaction() as db:
            row = self._locked_row(db, "agent_followup_snapshots", "run_id", rid)
            if row is None or row["status"] != "BUILDING" or not self.alive(db, row["conversation_id"], rid):
                raise ValueError("snapshot was revoked")
            lock_key(db, "quota", "followup-snapshot-bytes")
            allocated = db.execute("SELECT coalesce(sum(reserved_bytes),0) FROM agent_followup_snapshots WHERE run_id<>?", (rid,)).fetchone()[0]
            if size > per_report or allocated + size > total:
                raise ValueError("snapshot exceeds its byte budget")
            db.execute("UPDATE agent_followup_snapshots SET reserved_bytes=? WHERE run_id=?", (size, rid))

    def finish_snapshot(self, rid, manifest):
        with self.repository.database_transaction() as db:
            row = self._locked_row(db, "agent_followup_snapshots", "run_id", rid)
            if row is None or row["status"] != "BUILDING" or not self.alive(db, row["conversation_id"], rid):
                return False
            db.execute("UPDATE agent_followup_snapshots SET status='READY',manifest_json=?,updated_at=? WHERE run_id=?", (_json(manifest), self.now(), rid))
        return True

    def fail_snapshot(self, rid, code="SNAPSHOT_UNAVAILABLE"):
        with self.repository.database_transaction() as db:
            if self._locked_row(db, "agent_followup_snapshots", "run_id", rid) is None:
                return
            db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code=?,updated_at=? WHERE run_id=? AND status IN ('PENDING','BUILDING')", (code, self.now(), rid))

    @staticmethod
    def _fingerprint(rid, text, *, intent=None, target_followup_id=None, attachment_ids=()):
        # Existing ordinary requests must replay after the storage upgrade.
        value = [rid, text] if intent is None else [rid, text, intent, target_followup_id]
        if attachment_ids:
            value.append(list(attachment_ids))
        return hashlib.sha256(_json(value).encode()).hexdigest()

    def request(self, cid, rid, request_id, text, owner, *, intent=None, target_followup_id=None, attachment_ids=()):
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND request_id=?", (cid, request_id))
            if row is None:
                return None
            if row["fingerprint"] != self._fingerprint(rid, text, intent=intent,
                    target_followup_id=target_followup_id, attachment_ids=attachment_ids):
                raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
            return FollowupReceipt.model_validate_json(row["receipt"])

    def _append(self, db, cid, item, kind):
        lock_conversation(db, cid)
        rid = item.run_id
        sequence = db.execute("SELECT last_event_id FROM agent_followup_snapshots WHERE run_id=?", (rid,)).fetchone()[0] + 1
        event = FollowupEvent(sequence=sequence, conversation_id=cid, run_id=rid,
            followup_id=item.followup_id, type=kind, created_at=self.now(), data=item)
        db.execute("INSERT INTO agent_followup_events(conversation_id,run_id,sequence,body) VALUES (?,?,?,?)", (cid, rid, sequence, _json(event)))
        db.execute("UPDATE agent_followup_snapshots SET last_event_id=? WHERE run_id=?", (sequence, rid))
        return sequence

    @staticmethod
    def _history(db, rid, before=None):
        clause, args = ("", (rid,)) if before is None else (" AND ordinal<?", (rid, before))
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM agent_followup_tasks WHERE run_id=?" + clause + " ORDER BY ordinal", args)]

    def submit(self, cid, rid, request, source, owner, *, logs_supported=True, attachment_ids=None):
        with self.repository.database_transaction() as db:
            lock_conversation(db, cid)
            self.scope(db, cid, rid, owner)
            previous = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND request_id=?", (cid, request.request_id))
            fingerprint = self._fingerprint(rid, request.text, intent=request.intent,
                target_followup_id=request.target_followup_id, attachment_ids=request.attachment_ids)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                return FollowupReceipt.model_validate_json(previous["receipt"])
            if self.active(db, cid) is not None:
                raise fail("BUSY", "本会话有追问正在处理，请等待完成或先停止。")
            correction = self._correction(db, cid, rid, request, source)
            history = self._history(db, rid)
            self._register_source(db, cid, rid, source)
            report_ids = dialect_for(db).json_document("r.body", "$.report_inputs.attachment_ids")
            snapshot = _one(db, "SELECT s.*," + report_ids + " AS report_attachment_ids FROM agent_followup_snapshots s "
                "JOIN agent_conversation_runs r ON r.run_id=s.run_id AND r.conversation_id=s.conversation_id "
                "WHERE s.run_id=?", (rid,))
            mode = "REPORT_AND_LOGS" if (logs_supported and snapshot["status"] == "READY"
                and source.has_logs and not self.expired(source)) else "REPORT_ONLY"
            fid, now = str(uuid.uuid4()), self.now()
            item = FollowupItem(followup_id=fid, run_id=rid, request_id=request.request_id,
                ordinal=len(history) + 1, status="QUEUED", context_mode=mode, text=request.text,
                attachment_ids=request.attachment_ids if attachment_ids is None else attachment_ids,
                created_at=now, updated_at=now)
            task = {"followup_id": fid, "conversation_id": cid, "run_id": rid, "ordinal": item.ordinal,
                "status": "RUNNING", "body": _json(item)}
            decision_context = self.decision_context(db, task, history, json.loads(snapshot["report_attachment_ids"] or "[]"))
            try:
                build_prompt(source, [], request.text, mode,
                    read_search=logs_supported, correction=correction,
                    request_key=fid, snapshot_status=snapshot["status"], diagnosis_context=decision_context)
            except ValueError:
                raise fail("CONTEXT_LIMIT", "本次补充内容过多，请分开发送。", 413) from None
            sequence = self._append(db, cid, item, "followup.accepted")
            receipt = FollowupReceipt(conversation_id=cid, run_id=rid, followup_id=fid,
                request_id=request.request_id, event_id=sequence)
            db.execute("INSERT INTO agent_followup_tasks(followup_id,conversation_id,run_id,request_id,fingerprint,ordinal,status,body,receipt,workspace_id,epoch,correction_json) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (fid, cid, rid, request.request_id, fingerprint, item.ordinal, "QUEUED", _json(item), _json(receipt), fid, _json(correction)))
        self.notify()
        return receipt

    @staticmethod
    def _correction(db, cid, rid, request, source):
        if request.intent != "CORRECTION":
            return {}
        target = source.report_markdown
        if request.target_followup_id is not None:
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND run_id=? AND followup_id=?",
                (cid, rid, request.target_followup_id))
            if row is None:
                raise fail("NOT_FOUND", "要纠正的回答不存在。", 404)
            item = FollowupItem.model_validate_json(row["body"])
            if item.status != "COMPLETED":
                raise fail("UNSUPPORTED", "只能纠正已经完成的回答。")
            target = item.answer_markdown
        return {"intent": "CORRECTION", "target_followup_id": request.target_followup_id,
            "target_answer_markdown": target}

    def learning_source(self, cid, rid, fid, owner):
        """Private source only; public follow-up item and SSE schemas stay fixed."""
        with self.repository.database_read() as db:
            self.scope(db, cid, rid, owner)
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE conversation_id=? AND run_id=? AND followup_id=?", (cid, rid, fid))
            if row is None:
                raise fail("NOT_FOUND", "这条追问不存在。", 404)
            item = FollowupItem.model_validate_json(row["body"])
            snapshot = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (rid,))
            return (None if snapshot is None else self._source(snapshot), item,
                json.loads(row["correction_json"]), self._history(db, rid, row["ordinal"]))

    def _enqueue_correction(self, db, row, item):
        if self.memory_store is None or item.diagnosis_run_id is not None:
            return
        correction = json.loads(row["correction_json"])
        if not correction:
            return
        snapshot = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (row["run_id"],))
        if snapshot is None:
            return
        source = self._source(snapshot)
        if source.source_kind != "GENERIC" or not source.skill_name:
            return
        from problem_locator.memory.models import FeedbackSource
        history = self._history(db, row["run_id"], row["ordinal"])
        context = {"original_problem": source.problem_text, "original_report": source.report_markdown,
            "prior_turns": [{"question": item["text"], "answer": item.get("answer_markdown"),
                "status": item["status"]} for item in history],
            "question": item.text, "answer": item.answer_markdown, "correction": correction}
        self.memory_store.enqueue_correction_in_transaction(db, row["conversation_id"], row["run_id"],
            FeedbackSource(case_id=source.case_id, source_job_id=source.source_job_id, skill_name=source.skill_name,
                problem_text=source.problem_text, report_markdown=item.answer_markdown,
                report_sha256=hashlib.sha256(item.answer_markdown.encode("utf-8")).hexdigest(),
                answer_key=item.followup_id, source_kind="CORRECTION", context_json=_json(context),
                replaces_answer_key=correction["target_followup_id"] or ""))

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

    def _transition(self, db, row, status, *, answer=None, code=None, emit=True, diagnosis_run_id=None,
                    checkpoint=True):
        old = FollowupItem.model_validate_json(row["body"])
        if old.status == status:
            return old
        failure = None if code is None else FollowupFailure(code=code, message=_FAILURES[code])
        item = FollowupItem.model_validate({**old.model_dump(), "status": status,
            "answer_markdown": answer, "diagnosis_run_id": diagnosis_run_id,
            "failure": None if failure is None else failure.model_dump(), "updated_at": self.now()})
        db.execute("UPDATE agent_followup_tasks SET status=?,body=? WHERE followup_id=?", (status, _json(item), item.followup_id))
        if status == "COMPLETED" and checkpoint:
            self.sessions.checkpoint(db, row)
        if emit and self.alive(db, row["conversation_id"], row["run_id"]):
            self._append(db, row["conversation_id"], item, "followup.updated")
        return item

    def claim_task(self):
        self.last_claim_empty = False
        with self.repository.database_transaction() as db:
            dialect = dialect_for(db)
            suffix = " FOR UPDATE OF t SKIP LOCKED" if dialect.postgres else ""
            row = _one(db, "SELECT t.* FROM agent_followup_tasks t JOIN agent_conversations c USING(conversation_id) "
                "JOIN agent_conversation_runs r ON r.run_id=t.run_id WHERE t.status='QUEUED' AND c.deleted_at IS NULL "
                f"ORDER BY {dialect.order_column('t')} LIMIT 1" + suffix)
            if row is None:
                self.last_claim_empty = True
                return None
            # A deleter holds the conversation lock before touching tasks.
            # Never wait for that lock while retaining this queue row lock.
            if not try_lock_conversation(db, row["conversation_id"]) or not self.alive(db, row["conversation_id"], row["run_id"]):
                return None
            source = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (row["run_id"],))
            if source is None:
                self._transition(db, row, "CANCELLED")
                return None
            self._transition(db, row, "RUNNING")
            db.execute("UPDATE agent_followup_tasks SET epoch=? WHERE followup_id=?", (self.epoch, row["followup_id"]))
            row.update(status="RUNNING", epoch=self.epoch)
            return row

    def task_context(self, task):
        with self.repository.database_read() as db:
            dialect = dialect_for(db)
            effective_text = dialect.json_text("r.body", "$.report_inputs.effective_problem_text")
            report_ids = dialect.json_document("r.body", "$.report_inputs.attachment_ids")
            source = _one(db, "SELECT s.*," + effective_text + " AS effective_problem_text," +
                report_ids + " AS report_attachment_ids "
                "FROM agent_followup_snapshots s LEFT JOIN agent_conversation_runs r "
                "ON r.run_id=s.run_id AND r.conversation_id=s.conversation_id WHERE s.run_id=?", (task["run_id"],))
            if source is None:
                raise ValueError("followup source was removed")
            current = self._source(source)
            effective = source.pop("effective_problem_text")
            if effective is not None:
                if not isinstance(effective, str) or not effective.strip() or len(effective.encode("utf-8")) > MAX_TEXT_BYTES:
                    raise ValueError("confirmed problem exceeds the followup input contract")
                # Old snapshots may predate capture of named facts. Keep their
                # log bytes immutable and use the run's adopted text in context.
                current = replace(current, problem_text=effective)
            if self.resources_expired(db, task["conversation_id"], task["run_id"]):
                current = replace(current, has_logs=False, has_log_manifest=False)
            source["report_attachment_ids"] = json.loads(source["report_attachment_ids"] or "[]")
            return current, self._history(db, task["run_id"], task["ordinal"]), source

    @staticmethod
    def material_origins(task, history, report_ids):
        """A candidate is an uploaded input, never an inferred file name."""
        origins = {}
        def add(ids, origin):
            for aid in dict.fromkeys(ids):
                origins.setdefault(aid, []).append(origin)
        add(report_ids, {"source": "REPORT", "run_id": task["run_id"]})
        for entry in history:
            if entry["status"] in {"CANCELLED", "CANCELLING"}:
                continue
            add(entry.get("attachment_ids", []), {"source": "FOLLOWUP", "run_id": task["run_id"],
                "followup_id": entry["followup_id"], "turn": entry["ordinal"], "status": entry["status"]})
        current = json.loads(task["body"])
        add(current.get("attachment_ids", []), {"source": "CURRENT", "run_id": task["run_id"],
            "followup_id": task["followup_id"], "turn": task["ordinal"], "status": task["status"]})
        return origins

    @staticmethod
    def latest_attempt_link(history):
        return next((entry for entry in reversed(history) if entry.get("diagnosis_run_id")), None)

    @staticmethod
    def _latest_attempt(db, task, history):
        link = FollowupStore.latest_attempt_link(history)
        if link is None:
            return None
        d = dialect_for(db)
        row = db.execute("SELECT run_id,status," + d.json_text("body", "$.parent_run_id") + "," +
            d.json_int("body", "$.report_available") + "," + d.json_document("body", "$.failure") + "," +
            d.json_document("body", "$.current_questions") + " FROM agent_conversation_runs "
            "WHERE conversation_id=? AND run_id=?", (task["conversation_id"], link["diagnosis_run_id"])).fetchone()
        if row is None or row[2] != task["run_id"] or row[3]:
            # Do not revive an older failed attempt after its successor published.
            return None
        failure = json.loads(row[4] or "null")
        result = {"run_id": row[0], "status": row[1], "current_questions": json.loads(row[5] or "[]"),
            "failure": None if failure is None else {key: failure[key] for key in ("code", "message")},
            "effective_problem_text": None, "initial_attachment_ids": [], "user_messages": []}
        for (raw,) in db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? ORDER BY event_sequence",
                (task["conversation_id"], row[0])):
            message = json.loads(raw)
            if message["request_id"] == "followup-diagnosis-" + link["followup_id"]:
                result["initial_attachment_ids"] = message["attachment_ids"]
                result["effective_problem_text"] = message["text"]
                continue
            result["user_messages"].append({key: message[key] for key in ("message_id", "text", "status", "attachment_ids")})
        return result

    def decision_context(self, db, task, history, report_ids):
        origins = self.material_origins(task, history, report_ids)
        attempt = self._latest_attempt(db, task, history)
        if attempt is not None:
            for aid in dict.fromkeys(attempt["initial_attachment_ids"]):
                origins.setdefault(aid, []).append({"source": "LATEST_ATTEMPT", "run_id": attempt["run_id"],
                    "kind": "INITIAL_INPUT"})
            for message in attempt["user_messages"]:
                if message["status"] == "UNUSED":
                    continue
                origin = {"source": "LATEST_ATTEMPT", "run_id": attempt["run_id"],
                    "message_id": message["message_id"], "status": message["status"]}
                for aid in dict.fromkeys(message["attachment_ids"]):
                    origins.setdefault(aid, []).append(origin)
        materials = []
        if origins:
            placeholders = ",".join("?" for _ in origins)
            rows = {entry["attachment_id"]: entry for (raw,) in db.execute(
                "SELECT body FROM agent_attachments WHERE conversation_id=? AND attachment_id IN (" + placeholders + ")",
                (task["conversation_id"], *origins)) for entry in (json.loads(raw),)}
            for aid, sources in origins.items():
                record = rows.get(aid, {})
                if record.get("conversation_id") != task["conversation_id"]:
                    record = {}
                materials.append({"attachment_id": aid, "name": record.get("name"), "size": record.get("size"),
                    "sha256": record.get("sha256"), "available": record.get("status") in {"READY", "IMPORTED"},
                    "origins": sources})
        return {"materials": materials, "latest_attempt": attempt}

    def execution_allowed(self, fid):
        with self.repository.database_read() as db:
            row = _one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
            return row is not None and row["epoch"] == self.epoch and row["status"] == "RUNNING" and self.alive(db, row["conversation_id"], row["run_id"])

    def finish_task(self, fid, *, answer=None, code=None, interrupted=False, checkpoint=True):
        with self.repository.database_transaction() as db:
            row = self._locked_row(db, "agent_followup_tasks", "followup_id", fid)
            if row is None or row["status"] not in {"RUNNING", "CANCELLING"} or row["epoch"] != self.epoch:
                return False
            if row["status"] == "CANCELLING" or not self.alive(db, row["conversation_id"], row["run_id"]):
                self._transition(db, row, "CANCELLED")
            elif interrupted:
                self._transition(db, row, "INTERRUPTED", code="AGENT_FOLLOWUP_INTERRUPTED")
            elif code is not None:
                self._transition(db, row, "FAILED", code=code)
            else:
                item = self._transition(db, row, "COMPLETED", answer=answer, checkpoint=checkpoint)
                db.execute("SAVEPOINT followup_memory_enqueue")
                try:
                    self._enqueue_correction(db, row, item)
                except Exception as exc:
                    db.execute("ROLLBACK TO SAVEPOINT followup_memory_enqueue")
                    log_event("agent.followup.memory_enqueue_failed", error_type=type(exc).__name__)
                finally:
                    db.execute("RELEASE SAVEPOINT followup_memory_enqueue")
        self.notify()
        return True

    def stop(self, cid, rid, fid, request_id, owner):
        with self.repository.database_transaction() as db:
            lock_conversation(db, cid)
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
                db.execute("INSERT INTO agent_followup_stops(conversation_id,run_id,request_id,followup_id) VALUES (?,?,?,?)", (cid, rid, request_id, fid))
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
        with self.repository.database_read() as db:
            conversations = db.execute("SELECT conversation_id FROM agent_followup_tasks WHERE status IN ('QUEUED','RUNNING','CANCELLING') "
                "UNION SELECT conversation_id FROM agent_followup_snapshots WHERE status IN ('PENDING','BUILDING')").fetchall()
        for (cid,) in conversations:
            with self.repository.database_transaction() as db:
                lock_conversation(db, cid)
                tasks = db.execute("SELECT followup_id FROM agent_followup_tasks WHERE conversation_id=? "
                    "AND status IN ('QUEUED','RUNNING','CANCELLING')", (cid,)).fetchall()
                for (fid,) in tasks:
                    row = _one(db, "SELECT * FROM agent_followup_tasks WHERE followup_id=?", (fid,))
                    source = _one(db, "SELECT * FROM agent_followup_snapshots WHERE run_id=?", (row["run_id"],))
                    fresh = source is not None
                    if row["status"] == "QUEUED" and enabled and fresh and self.alive(db, cid, row["run_id"]):
                        continue
                    cancelled = row["status"] != "RUNNING" or not self.alive(db, cid, row["run_id"])
                    self._transition(db, row, "CANCELLED" if cancelled else "INTERRUPTED",
                        code=None if cancelled else "AGENT_FOLLOWUP_INTERRUPTED")
                db.execute("UPDATE agent_followup_snapshots SET status='FAILED',error_code='SNAPSHOT_INTERRUPTED',updated_at=? "
                    "WHERE conversation_id=? AND status='BUILDING'", (self.now(), cid))
                if not enabled:
                    db.execute("UPDATE agent_followup_snapshots SET status='UNAVAILABLE',error_code='DISABLED',updated_at=? "
                        "WHERE conversation_id=? AND status='PENDING'", (self.now(), cid))

    def revoke(self, db, cid):
        lock_conversation(db, cid)
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
        lock_conversation(db, cid)
        tail, args = ("", (cid,)) if run_id is None else (" AND run_id=?", (cid, run_id))
        if run_id is None:
            from .sessions import FollowupSessions
            FollowupSessions.purge(db, cid)
        for table in ("agent_followup_events", "agent_followup_stops", "agent_followup_tasks", "agent_followup_snapshots"):
            db.execute(f"DELETE FROM {table} WHERE conversation_id=?" + tail, args)

    @staticmethod
    def expire_resources(db, cid, rid):
        """Revoke future log use without removing questions, answers or cursors."""
        lock_conversation(db, cid)
        db.execute("UPDATE agent_followup_snapshots SET status='UNAVAILABLE',error_code='EXPIRED' "
            "WHERE conversation_id=? AND run_id=?", (cid, rid))

    @staticmethod
    def finish_resource_cleanup(db, cid, rid):
        db.execute("UPDATE agent_followup_snapshots SET reserved_bytes=0,manifest_json=NULL "
            "WHERE conversation_id=? AND run_id=? AND error_code='EXPIRED'", (cid, rid))

    @staticmethod
    def workspace_ids(db, cid, run_id=None):
        tail, args = ("", (cid,)) if run_id is None else (" AND run_id=?", (cid, run_id))
        workspaces = {row[0] for row in db.execute("SELECT workspace_id FROM agent_followup_tasks WHERE conversation_id=?" + tail, args)}
        if "memory_tasks" in table_names(db):
            # Correction checks copy private logs into their task workspace.
            # Include every state: success/failure/deletion does not remove those files.
            workspaces.update(row[0] for row in db.execute(
                "SELECT task_id FROM memory_tasks WHERE source_kind='CORRECTION' AND conversation_id=?" + tail, args))
        return sorted(workspaces)

    def workspace_in_use(self, workspace_id):
        with self.repository.database_read() as db:
            return (db.execute("SELECT 1 FROM agent_followup_snapshots WHERE source_job_id=? AND status IN ('PENDING','BUILDING') LIMIT 1", (workspace_id,)).fetchone() is not None
                or db.execute("SELECT 1 FROM agent_followup_tasks WHERE workspace_id=? AND status IN ('QUEUED','RUNNING','CANCELLING') LIMIT 1", (workspace_id,)).fetchone() is not None
                or ("memory_tasks" in table_names(db) and db.execute(
                    "SELECT 1 FROM memory_tasks WHERE task_id=? AND status='RUNNING' LIMIT 1", (workspace_id,)).fetchone() is not None))
