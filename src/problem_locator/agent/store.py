"""Durable Agent conversations and transactionally published public events.

All database access uses the core repository's connection and lock. Projection
never reads the repository or obtains a Case lock while holding the DB lock.
"""
from __future__ import annotations

import hashlib
import base64
import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
from contextlib import contextmanager

from problem_locator.contracts import StateFile
from problem_locator.application.projection import project_artifact_summaries
from problem_locator.diagnostics import log_event
from .failures import interrupted_execution_failure, public_failure
from .models import (AgentAttachment, AgentEvent, AgentMessage, AgentStoreError,
                     AttachmentRecord, CreateConversationRequest, ConversationReceipt, ConversationView,
                     MessageReceipt, SendMessageRequest, PUBLIC_PROGRESS_MESSAGES, ConversationStatusView,
                     AgentProgressData, ConversationRun, ConversationCapabilities, ConversationSummary,
                     ConversationList, ConversationHistoryEntry, ConversationResultSummary, StopReceipt, DeleteReceipt)

_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
_RESULT = {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED"}
_PROGRESS = PUBLIC_PROGRESS_MESSAGES
_SAFE_FAILURE = "本次定位未能完成，请重新发起任务。"
_QUEUED_NOTICE = "已收到，尚未用于本次诊断。"
_UNUSED_NOTICE = "这条消息未用于本次诊断，请另建任务继续。"
_KEEP_ATTACHMENT_NOTICE = object()


@dataclass(frozen=True)
class ConversationRead:
    view: ConversationStatusView
    progress: AgentProgressData | None
    messages: list[AgentMessage] | None
    attachments: list[AgentAttachment] | None
    snapshot: StateFile | None = None
    history: list[ConversationHistoryEntry] | None = None
    history_next_cursor: str | None = None
    current_run: ConversationRun | None = None
    title: str = "新诊断"
    capabilities: ConversationCapabilities | None = None
    selected_run_id: str | None = None


AGENT_STORAGE_VERSION = "2"
_V2_TABLES = (
    "CREATE TABLE IF NOT EXISTS agent_generic_restarts (conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,run_id TEXT NOT NULL,fingerprint TEXT NOT NULL,command_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(conversation_id,request_id))",
    "CREATE INDEX IF NOT EXISTS agent_generic_restarts_pending ON agent_generic_restarts(status,run_id)",
    "CREATE TABLE IF NOT EXISTS agent_conversation_runs (run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,case_id TEXT UNIQUE,create_request_id TEXT UNIQUE,epoch TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(conversation_id,ordinal))",
    "CREATE INDEX IF NOT EXISTS agent_runs_conversation ON agent_conversation_runs(conversation_id,ordinal)",
    "CREATE TABLE IF NOT EXISTS agent_attachment_imports (run_id TEXT NOT NULL,attachment_id TEXT NOT NULL,case_attachment_id TEXT NOT NULL,PRIMARY KEY(run_id,attachment_id))",
    "CREATE TABLE IF NOT EXISTS agent_stop_requests (conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,run_id TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(conversation_id,request_id))",
    "CREATE TABLE IF NOT EXISTS agent_cleanup_jobs (conversation_id TEXT PRIMARY KEY,status TEXT NOT NULL,manifest TEXT NOT NULL,error_code TEXT)",
    "CREATE INDEX IF NOT EXISTS agent_cleanup_status ON agent_cleanup_jobs(status)",
    "CREATE TABLE IF NOT EXISTS agent_deleted_requests (request_hash TEXT PRIMARY KEY,conversation_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_create_keys (request_hash TEXT PRIMARY KEY,conversation_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_create_key_times (request_hash TEXT PRIMARY KEY,created_at TEXT NOT NULL)",
)


def upgrade_agent_storage_v2(db, owner_map=None):
    """Explicit offline-copy migration; historical payload bytes stay untouched."""
    additions = {"agent_conversations": [("owner_key", "TEXT"), ("title", "TEXT NOT NULL DEFAULT '新诊断'"),
        ("current_run_id", "TEXT"), ("updated_at", "TEXT"), ("deleted_at", "TEXT"), ("cleanup_status", "TEXT")],
        "agent_messages": [("run_id", "TEXT"), ("event_sequence", "INTEGER")],
        "agent_events": [("run_id", "TEXT")], "agent_dispatches": [("run_id", "TEXT")]}
    for table, columns in additions.items():
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    for statement in _V2_TABLES:
        db.execute(statement)
    for cid, epoch, status, raw, case_id, create_key, original_request in db.execute(
            "SELECT conversation_id,epoch,status,body,case_id,create_request_id,request_id FROM agent_conversations WHERE current_run_id IS NULL").fetchall():
        body = json.loads(raw)
        run_id = cid
        body.update(run_id=run_id, ordinal=1, stop_requested=False)
        db.execute("INSERT INTO agent_conversation_runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, cid, 1, case_id, create_key, epoch, status, _json(body)))
        first = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? ORDER BY rowid LIMIT 1", (cid,)).fetchone()
        title = "新诊断" if first is None else _title(json.loads(first[0]).get("text", ""))
        db.execute("UPDATE agent_conversations SET owner_key=?,title=?,current_run_id=?,updated_at=? WHERE conversation_id=?",
            ((owner_map or {}).get(cid), title, run_id, body["updated_at"], cid))
        # Preserve the original request bytes. A separately assigned owner gets
        # an explicit alias for that same native request, never a guessed owner.
        for owner in {None, (owner_map or {}).get(cid)}:
            db.execute("INSERT OR IGNORE INTO agent_create_keys VALUES (?,?)",
                (_create_request_hash(owner, original_request), cid))
        for table in ("agent_messages", "agent_events", "agent_dispatches"):
            db.execute(f"UPDATE {table} SET run_id=? WHERE conversation_id=? AND run_id IS NULL", (run_id, cid))
        for mid, receipt in db.execute("SELECT message_id,receipt FROM agent_messages WHERE conversation_id=?", (cid,)).fetchall():
            db.execute("UPDATE agent_messages SET event_sequence=? WHERE message_id=?", (json.loads(receipt)["event_id"], mid))
        for aid, attachment in db.execute("SELECT attachment_id,body FROM agent_attachments WHERE conversation_id=?", (cid,)).fetchall():
            bound = json.loads(attachment).get("case_attachment_id")
            if bound:
                db.execute("INSERT OR IGNORE INTO agent_attachment_imports VALUES (?,?,?)", (run_id, aid, bound))
    history_index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='agent_history'").fetchone()
    if history_index is not None and "conversation.completed" not in history_index[0]:
        db.execute("DROP INDEX agent_history")
    for statement in (
        "CREATE INDEX IF NOT EXISTS agent_directory ON agent_conversations(owner_key,deleted_at,updated_at DESC,conversation_id DESC)",
        "CREATE INDEX IF NOT EXISTS agent_messages_run ON agent_messages(conversation_id,run_id,event_sequence)",
        "CREATE INDEX IF NOT EXISTS agent_events_run_progress ON agent_events(conversation_id,run_id,sequence) WHERE json_extract(body, '$.type')='agent.progress'",
        "CREATE INDEX IF NOT EXISTS agent_history ON agent_events(conversation_id,sequence) WHERE json_extract(body, '$.type') IN ('message.accepted','assistant.question','result.available','conversation.completed')",
        "CREATE INDEX IF NOT EXISTS agent_dispatches_run ON agent_dispatches(conversation_id,run_id,status,epoch)",
        "CREATE INDEX IF NOT EXISTS agent_dispatches_pending ON agent_dispatches(epoch,conversation_id,run_id) WHERE status='PENDING'",
        "CREATE INDEX IF NOT EXISTS agent_retention_runs ON agent_conversation_runs("
        "coalesce(json_extract(body,'$.completed_at'),json_extract(body,'$.updated_at')))",
        "CREATE INDEX IF NOT EXISTS agent_conversations_intake_work ON agent_conversations(conversation_id) "
        "WHERE deleted_at IS NULL AND status NOT IN ('COMPLETED','FAILED','INTERRUPTED','CANCELLED','CANCELLING') "
        "AND coalesce(json_extract(body,'$.report_available'),0)=0 AND coalesce(json_extract(body,'$.stop_requested'),0)=0 "
        "AND json_extract(body,'$.intake_pending')=1 "
        "AND (case_id IS NULL OR json_extract(body,'$.case_status') IS NULL OR json_extract(body,'$.case_status') IN ('WAITING_INPUT','WAITING_ATTACHMENT'))",
    ):
        db.execute(statement)
    db.execute("INSERT OR IGNORE INTO agent_create_key_times SELECT k.request_hash,"
        "coalesce(json_extract(c.body,'$.conversation_created_at'),json_extract(c.body,'$.created_at'),c.deleted_at,c.updated_at) "
        "FROM agent_create_keys k JOIN agent_conversations c USING(conversation_id)")
    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('agent_storage_version',?)", (AGENT_STORAGE_VERSION,))


def _title(text):
    return " ".join(text.split())[:80] or "新诊断"


def _create_request_key(owner_key, request_id):
    return request_id if owner_key is None else "owner:" + hashlib.sha256(_json([owner_key, request_id]).encode()).hexdigest()


def _create_request_hash(owner_key, request_id):
    # None and an explicit owner are distinct domains, even if a legacy raw
    # request happens to look like an internally scoped storage key.
    return hashlib.sha256(_json([owner_key, request_id]).encode()).hexdigest()


def _json(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class AgentStore:
    """The website projection does not change the core Case revision."""

    def __init__(self, repository, clock=None, id_generator=None, runtime_epoch=None):
        self.repository = repository
        self.clock = clock
        self.ids = id_generator
        self.runtime_epoch = runtime_epoch or str(uuid.uuid4())
        self.condition = threading.Condition()
        self.on_change: Callable[[str], None] = lambda conversation_id: None
        self._scope = threading.local()
        with repository.database_read() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversations'").fetchone()
            marker = db.execute("SELECT value FROM metadata WHERE key='agent_storage_version'").fetchone()
            if exists and (marker is None or marker[0] != AGENT_STORAGE_VERSION):
                raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "会话数据需要先离线升级，请保留原目录。", 503)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    conversation_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    epoch TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
                    case_id TEXT UNIQUE, create_request_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS agent_messages (
                    message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL, receipt TEXT NOT NULL,
                    UNIQUE(conversation_id, request_id));
                CREATE INDEX IF NOT EXISTS agent_messages_conversation ON agent_messages(conversation_id);
                CREATE TABLE IF NOT EXISTS agent_events (
                    conversation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    dedupe_key TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, sequence), UNIQUE(conversation_id, dedupe_key));
                CREATE INDEX IF NOT EXISTS agent_events_progress
                    ON agent_events(conversation_id, sequence)
                    WHERE json_extract(body, '$.type')='agent.progress';
                CREATE TABLE IF NOT EXISTS agent_dispatches (
                    dispatch_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    epoch TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT);
                CREATE INDEX IF NOT EXISTS agent_dispatches_conversation_status
                    ON agent_dispatches(conversation_id,status,epoch);
                CREATE TABLE IF NOT EXISTS agent_message_adoptions (
                    message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    dispatch_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_attachments (
                    attachment_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL, storage_path TEXT,
                    UNIQUE(conversation_id, request_id));
            """)
            upgrade_agent_storage_v2(db)
            db.execute("UPDATE agent_cleanup_jobs SET status='PENDING' WHERE status='RUNNING'")
        repository.on_case_projection = self._project_state
        repository.on_case_committed = self._case_committed

    def _now(self):
        return self.clock.now() if self.clock else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _id(self, kind):
        return self.ids.new(kind) if self.ids else str(uuid.uuid4())

    def _notify(self, conversation_id):
        with self.condition:
            self.condition.notify_all()
        try:
            self.on_change(conversation_id)
        except Exception:
            # Notifications are hints; durable events remain available for
            # polling. A broken subscriber must not turn a committed command
            # into an apparent transactional failure.
            pass

    @contextmanager
    def run_scope(self, conversation_id, run_id, *, deleted=False):
        previous = getattr(self._scope, "value", None)
        self._scope.value = (conversation_id, run_id, deleted)
        try:
            yield
        finally:
            self._scope.value = previous

    def _load(self, db, conversation_id, *, run_id=None, deleted=False):
        scope = getattr(self._scope, "value", None)
        if scope is not None and scope[0] == conversation_id and run_id is None:
            run_id, deleted = scope[1:]
        row = db.execute("SELECT body,current_run_id,title,deleted_at FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        if row[3] is not None and not deleted:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        head = json.loads(row[0])
        selected = run_id or row[1]
        record = db.execute("SELECT body FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (conversation_id, selected)).fetchone()
        if record is None:
            raise AgentStoreError("AGENT_RUN_NOT_FOUND", "本次诊断不存在。", 404)
        body = json.loads(record[0])
        body.update(title=row[2], last_event_id=head.get("last_event_id", 0), _deleted=row[3] is not None)
        return body

    def _save(self, db, body):
        # A pre-retention closed run has no immutable completion anchor yet.
        # Capture its previous stored time before a rename/metadata refresh can
        # replace updated_at. New closures already set completed_at in _close.
        if body["status"] in _CLOSED and "completed_at" not in body:
            previous = db.execute("SELECT body FROM agent_conversation_runs WHERE run_id=? AND conversation_id=?",
                                  (body["run_id"], body["conversation_id"])).fetchone()
            if previous is not None:
                original = json.loads(previous[0])
                body["completed_at"] = original.get("completed_at", original["updated_at"])
        body["updated_at"] = self._now()
        cid, run_id = body["conversation_id"], body["run_id"]
        db.execute("UPDATE agent_conversation_runs SET status=?,case_id=?,body=? WHERE run_id=? AND conversation_id=?",
            (body["status"], body.get("case_id"), _json(body), run_id, cid))
        row = db.execute("SELECT current_run_id,body FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
        head = body if row[0] == run_id else json.loads(row[1])
        head["events_pruned_through"] = max(head.get("events_pruned_through", 0),
            json.loads(row[1]).get("events_pruned_through", 0))
        head["last_event_id"] = max(head.get("last_event_id", 0), body["last_event_id"])
        head["updated_at"] = body["updated_at"]
        db.execute("UPDATE agent_conversations SET status=?,case_id=?,body=?,updated_at=? WHERE conversation_id=?",
            (head["status"], head.get("case_id"), _json(head), head["updated_at"], cid))

    @staticmethod
    def _ensure_open(body):
        if body.get("_deleted") or body.get("stop_requested") or body["status"] in _CLOSED or body.get("report_available"):
            raise AgentStoreError("AGENT_CONVERSATION_CLOSED", "本次任务已经结束，请另建任务。", 409)

    def _append(self, db, body, event_type, data, dedupe_key):
        legacy_key = dedupe_key if body["run_id"] == body["conversation_id"] else None
        dedupe_key = body["run_id"] + ":" + dedupe_key
        row = db.execute("SELECT body FROM agent_events WHERE conversation_id=? AND (dedupe_key=? OR dedupe_key=?)",
                         (body["conversation_id"], dedupe_key, legacy_key)).fetchone()
        if row:
            return self._event(row[0], body["run_id"])
        head = db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (body["conversation_id"],)).fetchone()
        sequence = max(body["last_event_id"], json.loads(head[0])["last_event_id"]) + 1
        event = AgentEvent(schema_version=2, run_id=body["run_id"], sequence=sequence, conversation_id=body["conversation_id"],
            case_id=body.get("case_id"), job_id=body.get("job_id"), type=event_type,
            created_at=self._now(), data=data)
        db.execute("INSERT INTO agent_events(conversation_id,sequence,dedupe_key,body,run_id) VALUES (?,?,?,?,?)",
                   (body["conversation_id"], sequence, dedupe_key, _json(event), body["run_id"]))
        body["last_event_id"] = sequence
        return event

    def create_conversation(self, request_id, *, owner_key=None, title=None):
        request_id = CreateConversationRequest(request_id=request_id).request_id
        with self.repository.database_transaction() as db:
            key = _create_request_key(owner_key, request_id)
            request_hash = _create_request_hash(owner_key, request_id)
            row = db.execute("SELECT c.conversation_id,c.current_run_id,c.deleted_at FROM agent_create_keys k "
                "JOIN agent_conversations c ON c.conversation_id=k.conversation_id WHERE k.request_hash=?", (request_hash,)).fetchone()
            if row:
                if row[2] is not None:
                    raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "原会话已删除，请使用新的 request_id。", 404)
                first_run = db.execute("SELECT run_id FROM agent_conversation_runs WHERE conversation_id=? AND ordinal=1", (row[0],)).fetchone()[0]
                return ConversationReceipt(conversation_id=row[0], request_id=request_id, run_id=first_run)
            conversation_id, run_id, now = self._id("conversation"), self._id("run"), self._now()
            body = dict(conversation_id=conversation_id, status="INTAKE", case_id=None, job_id=None,
                        case_status=None, archive_status="NOT_REQUIRED", current_questions=[],
                        last_event_id=0, created_at=now, updated_at=now, draft={}, report_available=False,
                        intake_pending=False, intake_covered_message_ids=[], run_id=run_id, ordinal=1, stop_requested=False)
            db.execute("INSERT INTO agent_conversations(conversation_id,request_id,epoch,status,body,owner_key,title,current_run_id,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (conversation_id, key, self.runtime_epoch, "INTAKE", _json(body), owner_key, _title(title or ""), run_id, now))
            db.execute("INSERT INTO agent_conversation_runs VALUES (?,?,1,NULL,NULL,?,'INTAKE',?)", (run_id, conversation_id, self.runtime_epoch, _json(body)))
            db.execute("INSERT INTO agent_create_keys VALUES (?,?)", (request_hash, conversation_id))
            db.execute("INSERT INTO agent_create_key_times VALUES (?,?)", (request_hash, now))
        return ConversationReceipt(conversation_id=conversation_id, request_id=request_id, run_id=run_id)

    @staticmethod
    def _event(raw, run_id):
        value = json.loads(raw)
        value.update(schema_version=2, run_id=run_id)
        if value["type"] == "message.accepted":
            value["data"]["run_id"] = run_id
        return AgentEvent.model_validate(value)

    @staticmethod
    def _message(raw, run_id):
        return AgentMessage.model_validate({**json.loads(raw), "run_id": run_id})

    def _new_run(self, db, previous):
        cid, now, run_id = previous["conversation_id"], self._now(), self._id("run")
        ordinal = db.execute("SELECT coalesce(max(ordinal),0)+1 FROM agent_conversation_runs WHERE conversation_id=?", (cid,)).fetchone()[0]
        body = dict(conversation_id=cid, run_id=run_id, ordinal=ordinal, status="INTAKE", case_id=None,
            job_id=None, case_status=None, archive_status="NOT_REQUIRED", current_questions=[], failure=None,
            last_event_id=previous["last_event_id"], created_at=now, updated_at=now,
            events_pruned_through=previous.get("events_pruned_through", 0),
            conversation_created_at=previous.get("conversation_created_at", previous["created_at"]),
            draft={}, report_available=False, intake_pending=False, intake_covered_message_ids=[], stop_requested=False)
        db.execute("INSERT INTO agent_conversation_runs VALUES (?,?,?,NULL,NULL,?,'INTAKE',?)", (run_id, cid, ordinal, self.runtime_epoch, _json(body)))
        db.execute("UPDATE agent_conversations SET current_run_id=?,create_request_id=NULL,epoch=? WHERE conversation_id=?", (run_id, self.runtime_epoch, cid))
        self._append(db, body, "run.started", {"ordinal": ordinal}, "started")
        return body

    def submit_message(self, conversation_id, request_id, text="", attachment_ids=None, *, target_run_id=None,
                       routed_request_key=None):
        request = SendMessageRequest(request_id=request_id, text=text, attachment_ids=attachment_ids or [])
        # Preserve legacy request fingerprints when no explicit target is used.
        payload = request.model_dump()
        if target_run_id is not None:
            payload["target_run_id"] = target_run_id
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            previous = db.execute("SELECT fingerprint,receipt,run_id FROM agent_messages WHERE conversation_id=? AND request_id=?",
                                  (conversation_id, request_id)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                return MessageReceipt.model_validate({**json.loads(previous[1]), "run_id": previous[2]})
            restart = db.execute("SELECT fingerprint,command_key,status,payload,run_id FROM agent_generic_restarts WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id)).fetchone()
            if restart is not None:
                if restart[0] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                if (routed_request_key != restart[1] or restart[2] != "PENDING"
                        or json.loads(restart[3]).get("operation") != "MarkInitialLogArchiveExpected"):
                    raise AgentStoreError("AGENT_RESTART_PENDING", "日志接入尚未完成，请稍后重试同一请求。", 409, retryable=True)
                # The ROUTE marker lost to a specialized route. Resolve its
                # frozen request as the ordinary supplement it always was,
                # atomically with message acceptance and without a new run.
                self._ensure_open(body)
                if restart[4] != body["run_id"]:
                    raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409)
                if body.get("case_has_selected_skill") is not True:
                    # Core projects this value in its own commit transaction.
                    # Never acquire a Case lock while holding the DB lock.
                    raise AgentStoreError("AGENT_ROUTE_CHANGED", "定位策略已更新，正在接入日志，请稍后重试同一请求。", 409)
            elif routed_request_key is not None:
                raise AgentStoreError("AGENT_RUN_CHANGED", "日志接入请求已失效，请刷新会话。", 409)
            if target_run_id is not None and (target_run_id != body["run_id"] or body["status"] in _CLOSED or body.get("report_available")):
                raise AgentStoreError("AGENT_RUN_CHANGED", "本次诊断已结束或发生变化，请刷新会话。", 409)
            if body["status"] in _CLOSED or body.get("report_available"):
                if not request.text.strip():
                    raise AgentStoreError("VALIDATION_ERROR", "再次诊断时请提供问题描述。", 400)
                body = self._new_run(db, body)
            self._ensure_open(body)
            for attachment_id in request.attachment_ids:
                row = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=? AND conversation_id=?",
                                 (attachment_id, conversation_id)).fetchone()
                if not row or json.loads(row[0])["status"] not in {"READY", "IMPORTED"}:
                    raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件不存在、未上传完成或不属于本会话。", 409)
            count = db.execute("SELECT count(*) FROM agent_messages WHERE conversation_id=? AND run_id=?", (conversation_id, body["run_id"])).fetchone()[0]
            if count >= 200:
                raise AgentStoreError("AGENT_MESSAGE_LIMIT", "本次会话消息已达上限，请另建任务。", 409)
            message = AgentMessage(message_id=self._id("message"), request_id=request_id,
                text=request.text, attachment_ids=request.attachment_ids, status="QUEUED", created_at=self._now(),
                notice=_QUEUED_NOTICE, run_id=body["run_id"])
            event = self._append(db, body, "message.accepted", message.model_dump(mode="json"), "message:" + message.message_id)
            receipt = MessageReceipt(conversation_id=conversation_id, message_id=message.message_id,
                request_id=request_id, event_id=event.sequence, run_id=body["run_id"])
            db.execute("INSERT INTO agent_messages(message_id,conversation_id,request_id,fingerprint,body,receipt,run_id,event_sequence) VALUES (?,?,?,?,?,?,?,?)",
                       (message.message_id, conversation_id, request_id, fingerprint, _json(message), _json(receipt), body["run_id"], event.sequence))
            db.execute("UPDATE agent_conversations SET title=? WHERE conversation_id=? AND title='新诊断'",
                (_title(request.text), conversation_id))
            if body.get("case_status") in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
                # Older snapshots have public questions but no private gate
                # projection. Preserve that authoritative snapshot before hiding it.
                body.setdefault("intake_authoritative_questions", list(body.get("current_questions", [])))
            body["intake_pending"] = True
            body["current_questions"] = []
            if body.get("case_status") in {None, "WAITING_INPUT", "WAITING_ATTACHMENT"}:
                body["status"] = "INTAKE"
            self._save(db, body)
            if routed_request_key is not None:
                db.execute("UPDATE agent_generic_restarts SET status='COMPLETED' WHERE command_key=? AND status='PENDING'",
                    (routed_request_key,))
        self._notify(conversation_id)
        return receipt

    def message_request(self, conversation_id, request):
        """Read either the public receipt or a frozen, not-yet-accepted restart."""
        fingerprint = hashlib.sha256(_json(request.model_dump()).encode()).hexdigest()
        with self.repository.database_read() as db:
            self._load(db, conversation_id)
            previous = db.execute("SELECT fingerprint,receipt,run_id FROM agent_messages WHERE conversation_id=? AND request_id=?",
                (conversation_id, request.request_id)).fetchone()
            restart = db.execute("SELECT fingerprint,status,payload FROM agent_generic_restarts WHERE conversation_id=? AND request_id=?",
                (conversation_id, request.request_id)).fetchone()
            if any(row is not None and row[0] != fingerprint for row in (previous, restart)):
                raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
            receipt = None if previous is None else MessageReceipt.model_validate({**json.loads(previous[1]), "run_id": previous[2]})
            pending = None if restart is None else {**json.loads(restart[2]), "status": restart[1]}
            return receipt, pending

    def freeze_generic_restart(self, conversation_id, request, command, *, run_id, archive_sha256):
        """Persist intent before the Case commit; this does not accept the message."""
        fingerprint = hashlib.sha256(_json(request.model_dump()).encode()).hexdigest()
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            self._ensure_open(body)
            if body["run_id"] != run_id or body.get("case_id") != command.case_id:
                raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409)
            existing = db.execute("SELECT fingerprint,status,payload FROM agent_generic_restarts WHERE conversation_id=? AND request_id=?",
                (conversation_id, request.request_id)).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                return {**json.loads(existing[2]), "status": existing[1]}
            if db.execute("SELECT 1 FROM agent_generic_restarts WHERE run_id=? AND status IN ('PENDING','COMMITTED')", (run_id,)).fetchone():
                raise AgentStoreError("AGENT_RESTART_PENDING", "上一份日志正在接入，请稍后重试。", 409, retryable=True)
            if (type(command).__name__ == "RestartGenericDiagnosis" and archive_sha256 is not None
                    and body.get("generic_archive_sha256") == archive_sha256):
                raise AgentStoreError("AGENT_LOG_ALREADY_SELECTED", "这份日志已用于当前定位，无需重复提交。", 409)
            count = db.execute("SELECT count(*) FROM agent_messages WHERE conversation_id=? AND run_id=?", (conversation_id, run_id)).fetchone()[0]
            if count >= 200:
                raise AgentStoreError("AGENT_MESSAGE_LIMIT", "本次会话消息已达上限，请另建任务。", 409)
            # Recheck readiness and ownership at the durable intent boundary.
            for attachment_id in request.attachment_ids:
                row = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=? AND conversation_id=?",
                    (attachment_id, conversation_id)).fetchone()
                if row is None or json.loads(row[0])["status"] not in {"READY", "IMPORTED"}:
                    raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件不存在、未上传完成或不属于本会话。", 409)
            message = AgentMessage(message_id=self._id("message"), request_id=request.request_id,
                text=request.text, attachment_ids=request.attachment_ids, status="QUEUED", created_at=self._now(),
                notice=_QUEUED_NOTICE, run_id=run_id)
            payload = {"conversation_id": conversation_id, "run_id": run_id,
                "operation": type(command).__name__, "command": command.model_dump(mode="json"),
                "message": message.model_dump(mode="json"),
                "archive_sha256": archive_sha256}
            db.execute("INSERT INTO agent_generic_restarts VALUES (?,?,?,?,?,'PENDING',?)",
                (conversation_id, request.request_id, run_id, fingerprint, command.idempotency_key, _json(payload)))
            return {**payload, "status": "PENDING"}

    def pending_generic_restarts(self):
        with self.repository.database_read() as db:
            return [{**json.loads(row[1]), "status": row[0]} for row in db.execute(
                "SELECT status,payload FROM agent_generic_restarts WHERE status IN ('PENDING','COMMITTED') ORDER BY rowid")]

    def retarget_routed_log_request(self, command_key, command):
        """A rejected route marker may follow the now-active Generic Job once."""
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT status,payload FROM agent_generic_restarts WHERE command_key=?", (command_key,)).fetchone()
            if row is None or row[0] != "PENDING":
                raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409)
            payload = json.loads(row[1])
            if payload.get("operation") != "MarkInitialLogArchiveExpected":
                raise AgentStoreError("AGENT_RUN_CHANGED", "本次定位已结束或发生变化，请刷新会话。", 409)
            self._ensure_open(self._load(db, payload["conversation_id"], run_id=payload["run_id"]))
            payload.update(operation="RestartGenericDiagnosis", command=command.model_dump(mode="json"))
            db.execute("UPDATE agent_generic_restarts SET command_key=?,payload=? WHERE command_key=?",
                (command.idempotency_key, _json(payload), command_key))
            return {**payload, "status": "PENDING"}

    def finish_generic_restart(self, command_key, *, rejected=False):
        with self.repository.database_transaction() as db:
            db.execute("UPDATE agent_generic_restarts SET status=? WHERE command_key=? AND status IN ('PENDING','COMMITTED')",
                ("REJECTED" if rejected else "COMPLETED", command_key))

    def _accept_generic_restarts(self, db, body, state):
        rows = list(db.execute("SELECT request_id,fingerprint,command_key,payload FROM agent_generic_restarts "
            "WHERE conversation_id=? AND run_id=? AND status='PENDING'", (body["conversation_id"], body["run_id"])))
        for request_id, fingerprint, key, raw in rows:
            payload = json.loads(raw)
            accepted = next((record for record in state.idempotency_records.values()
                if record.operation == payload.get("operation", "RestartGenericDiagnosis") and record.idempotency_key == key
                and record.case_id == body["case_id"]), None)
            if accepted is None:
                continue
            # A stop/delete that wins this transaction rolls back the core
            # restart too. The old run must never acquire a replacement Job.
            self._ensure_open(body)
            message = AgentMessage.model_validate(payload["message"])
            body["job_id"] = state.cases[body["case_id"]].case.active_job_id
            event = self._append(db, body, "message.accepted", message.model_dump(mode="json"), "message:" + message.message_id)
            receipt = MessageReceipt(conversation_id=body["conversation_id"], message_id=message.message_id,
                request_id=request_id, event_id=event.sequence, run_id=body["run_id"])
            db.execute("INSERT INTO agent_messages(message_id,conversation_id,request_id,fingerprint,body,receipt,run_id,event_sequence) VALUES (?,?,?,?,?,?,?,?)",
                (message.message_id, body["conversation_id"], request_id, fingerprint, _json(message), _json(receipt), body["run_id"], event.sequence))
            db.execute("UPDATE agent_generic_restarts SET status='COMMITTED' WHERE command_key=?", (key,))
            body.update(intake_pending=True, current_questions=[], generic_archive_sha256=payload["archive_sha256"])
            if payload.get("operation", "RestartGenericDiagnosis") == "RestartGenericDiagnosis" and message.text.strip():
                adopted = body.setdefault("generic_adopted_message_ids", [])
                if message.message_id not in adopted:
                    adopted.append(message.message_id)

    def get_conversation(self, conversation_id, *, run_id=None):
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id, run_id=run_id)
            public = {key: value for key, value in body.items() if key in ConversationView.model_fields}
            public["messages"] = [self._message(row[0], body["run_id"]) for row in db.execute(
                "SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? ORDER BY event_sequence", (conversation_id, body["run_id"]))]
            public["attachments"] = [AgentAttachment.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_attachments WHERE conversation_id=? ORDER BY rowid", (conversation_id,))]
            return ConversationView.model_validate(public)

    def get_status(self, conversation_id, *, run_id=None):
        """Read only conversation metadata; history is loaded on explicit request."""
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id, run_id=run_id)
            return self._status(body)

    @staticmethod
    def _status(body):
        public = {key: value for key, value in body.items() if key in ConversationStatusView.model_fields}
        public["created_at"] = body.get("conversation_created_at", body["created_at"])
        public["report_state"] = ("READY" if body.get("report_available", False) or body.get("case_status") in _RESULT else
            "UNAVAILABLE" if body["status"] in _CLOSED else "PENDING")
        return ConversationStatusView.model_validate(public)

    def read_conversation(self, conversation_id, *, history=False, case_snapshot=False, run_id=None,
                          history_before=None, history_limit=50):
        """Capture only requested content; never hold a SQL lock during file IO."""
        with self.repository.database_read() as db:
            selected = self._load(db, conversation_id, run_id=run_id)
        selected_run_id = selected["run_id"]
        def capture(db):
            body = self._load(db, conversation_id, run_id=selected_run_id)
            view = self._status(body)
            current_id = db.execute("SELECT current_run_id FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            current = self._load(db, conversation_id, run_id=current_id)
            row = db.execute("SELECT body FROM agent_events WHERE conversation_id=? "
                "AND run_id=? AND json_extract(body, '$.type')='agent.progress' ORDER BY sequence DESC LIMIT 1",
                (conversation_id, selected_run_id)).fetchone()
            progress = None if row is None else AgentProgressData.model_validate(json.loads(row[0])["data"])
            attachments = None if not history else [AgentAttachment.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_attachments WHERE conversation_id=? ORDER BY rowid", (conversation_id,))]
            entries, next_cursor = self._history(db, conversation_id, history_before, history_limit) if history else (None, None)
            return ConversationRead(view, progress, None, attachments, history=entries, history_next_cursor=next_cursor,
                current_run=self._run_view(current), title=current["title"], capabilities=self._capabilities(current), selected_run_id=selected_run_id)

        if not case_snapshot:
            with self.repository.database_read() as db:
                return capture(db)
        # A conversation binds to one Case exactly once. Discover its lock key
        # without holding the database lock while waiting for a Case lock.
        case_id = selected.get("case_id")
        related, snapshot = self.repository.read_case_snapshot_with(case_id, capture)
        if related.view.case_id != case_id:
            # The Case was bound between discovery and capture. It cannot rebind.
            case_id = related.view.case_id
            related, snapshot = self.repository.read_case_snapshot_with(case_id, capture)
            if related.view.case_id != case_id:
                raise AgentStoreError("STATE_CORRUPT", "会话关联的任务发生异常变化。", 500)
        return ConversationRead(related.view, related.progress, related.messages, related.attachments, snapshot,
            related.history, related.history_next_cursor, related.current_run, related.title, related.capabilities, selected_run_id)

    def has_pending_messages(self, conversation_id, *, run_id=None):
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id, run_id=run_id)
            return db.execute("SELECT 1 FROM agent_messages WHERE conversation_id=? AND run_id=? "
                "AND json_extract(body, '$.status') IN ('QUEUED','PROCESSING') LIMIT 1",
                (conversation_id, body["run_id"])).fetchone() is not None

    def list_events(self, conversation_id, after=0, limit=100):
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AgentStoreError("AGENT_INVALID_CURSOR", "事件游标或批量大小无效。")
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            head = db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            pruned = json.loads(head[0]).get("events_pruned_through", 0)
            if after < pruned:
                raise AgentStoreError("AGENT_EVENT_CURSOR_EXPIRED",
                    "事件记录已过保留期，请先读取会话当前状态，再从 last_event_id 重新订阅。", 409,
                    details=[{"field": "retained_after_sequence", "actual": pruned}])
            if after > body["last_event_id"]:
                raise AgentStoreError("AGENT_INVALID_CURSOR", "事件游标超出会话范围。", 409)
            return [self._event(row[0], row[1]) for row in db.execute(
                "SELECT body,run_id FROM agent_events WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (conversation_id, after, limit))]

    @staticmethod
    def _run_view(body):
        values = {name: body.get(name) for name in ConversationRun.model_fields if name != "report_state"}
        values["report_state"] = AgentStore._status(body).report_state
        return ConversationRun.model_validate(values)

    @staticmethod
    def _capabilities(body):
        deleted = bool(body.get("_deleted", False))
        stopping = body["status"] == "CANCELLING"
        # SQLite json_object represents extracted JSON booleans as 0/1.
        # Keep the public strict-bool contract for both SQL summaries and runs.
        ended = body["status"] in _CLOSED or bool(body.get("report_available", False))
        return ConversationCapabilities(can_send=not deleted and not stopping,
            can_stop=not deleted and not ended and not stopping, can_rediagnose=not deleted and ended,
            can_rename=not deleted, can_delete=not deleted)

    def get_run(self, conversation_id, run_id=None, *, deleted=False):
        with self.repository.database_read() as db:
            return self._load(db, conversation_id, run_id=run_id, deleted=deleted)

    def conversation_for_case(self, case_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT conversation_id FROM agent_conversation_runs WHERE case_id=?", (case_id,)).fetchone()
            return None if row is None else row[0]

    def require_owner(self, conversation_id, owner_key, *, deleted=False):
        with self.repository.database_read() as db:
            row = db.execute("SELECT owner_key,deleted_at FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None or (row[1] is not None and not deleted) or (owner_key is not None and row[0] != owner_key):
                raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)

    @staticmethod
    def _cursor(value):
        return base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value):
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError()
            return json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        except (ValueError, TypeError, UnicodeError):
            raise AgentStoreError("AGENT_INVALID_CURSOR", "分页游标无效。", 400) from None

    def list_conversations(self, owner_key, *, cursor=None, limit=20):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AgentStoreError("VALIDATION_ERROR", "每页数量应为 1 至 100。", 400)
        owner_tag = hashlib.sha256(_json(owner_key).encode()).hexdigest()
        values = [] if cursor is None else self._decode_cursor(cursor)
        if cursor is not None and (not isinstance(values, list) or len(values) != 3 or values[0] != owner_tag or any(not isinstance(v, str) for v in values)):
            raise AgentStoreError("AGENT_INVALID_CURSOR", "分页游标不属于当前用户。", 400)
        with self.repository.database_read() as db:
            params = [owner_key]
            where = "c.owner_key IS ? AND c.deleted_at IS NULL"
            if values:
                where += " AND (c.updated_at,c.conversation_id)<(?,?)"
                params += values[1:]
            fields = ["run_id", "ordinal", "status", "case_id", "job_id", "case_status", "archive_status", "created_at", "updated_at", "report_available"]
            projection = ",".join(f"'{field}',json_extract(r.body,'$.{field}')" for field in fields)
            rows = db.execute(f"SELECT c.conversation_id,c.updated_at,c.title,json_object({projection}),"
                "coalesce(json_extract(r.body,'$.conversation_created_at'),json_extract(r.body,'$.created_at')) "
                "FROM agent_conversations c JOIN agent_conversation_runs r ON r.run_id=c.current_run_id "
                f"WHERE {where} ORDER BY c.updated_at DESC,c.conversation_id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            items = []
            for cid, updated, title, raw, created in rows[:limit]:
                body = json.loads(raw)
                body["conversation_id"] = cid
                items.append(ConversationSummary(conversation_id=cid, title=title, current_run=self._run_view(body),
                    capabilities=self._capabilities(body), created_at=created, updated_at=updated))
            next_cursor = self._cursor([owner_tag, rows[limit - 1][1], rows[limit - 1][0]]) if len(rows) > limit else None
            return ConversationList(items=items, next_cursor=next_cursor)

    def rename_conversation(self, conversation_id, title):
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
            raise AgentStoreError("VALIDATION_ERROR", "标题应为 1 至 80 个字符。", 400)
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            db.execute("UPDATE agent_conversations SET title=? WHERE conversation_id=?", (title.strip(), conversation_id))
            self._save(db, body)
            return ConversationSummary(conversation_id=conversation_id, title=title.strip(),
                current_run=self._run_view(body), capabilities=self._capabilities(body),
                created_at=body.get("conversation_created_at", body["created_at"]), updated_at=body["updated_at"])

    def _history(self, db, conversation_id, before, limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AgentStoreError("VALIDATION_ERROR", "每页历史数量应为 1 至 100。", 400)
        values = [conversation_id, 9223372036854775807] if before is None else self._decode_cursor(before)
        if not isinstance(values, list) or len(values) != 2 or values[0] != conversation_id or type(values[1]) is not int or not 1 <= values[1] <= 9223372036854775807:
            raise AgentStoreError("AGENT_INVALID_CURSOR", "历史游标无效。", 400)
        rows = db.execute("SELECT body,run_id,sequence FROM agent_events WHERE conversation_id=? AND sequence<? "
            "AND json_extract(body, '$.type') IN ('message.accepted','assistant.question','result.available','conversation.completed') "
            "AND (json_extract(body, '$.type')<>'conversation.completed' OR json_extract(body, '$.data.status') IN ('FAILED','INTERRUPTED','CANCELLED')) "
            "ORDER BY sequence DESC LIMIT ?",
            (conversation_id, values[1], limit + 1)).fetchall()
        result = []
        for raw, run_id, sequence in reversed(rows[:limit]):
            event = self._event(raw, run_id)
            common = dict(id=str(sequence), run_id=run_id, created_at=event.created_at)
            if event.type == "message.accepted":
                row = db.execute("SELECT body FROM agent_messages WHERE message_id=? AND conversation_id=?", (event.data["message_id"], conversation_id)).fetchone()
                result.append(ConversationHistoryEntry(**common, type="user.message", message=self._message(row[0], run_id)))
            elif event.type == "assistant.question":
                result.append(ConversationHistoryEntry(**common, type="assistant.question", questions=event.data["questions"]))
            elif event.type == "result.available":
                artifacts = event.data["artifacts"]
                result.append(ConversationHistoryEntry(**common, type="diagnosis.result", result=ConversationResultSummary(
                    status="COMPLETED", report_state="READY", case_id=event.case_id, case_status=event.data["status"],
                    source_job_id=None if not artifacts else artifacts[0]["created_by_job_id"])))
            else:
                row = db.execute("SELECT json_extract(body,'$.case_status'),json_extract(body,'$.failure') "
                    "FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (conversation_id, run_id)).fetchone()
                result.append(ConversationHistoryEntry(**common, type="diagnosis.result", result=ConversationResultSummary(
                    status=event.data["status"], report_state="UNAVAILABLE", case_id=event.case_id,
                    case_status=row[0], failure=None if row[1] is None else json.loads(row[1]))))
        return result, self._cursor([conversation_id, rows[limit - 1][2]]) if len(rows) > limit else None

    def get_draft(self, conversation_id):
        with self.repository.database_read() as db:
            return self._load(db, conversation_id).get("draft", {})

    def request_stop(self, conversation_id, request_id, run_id):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id, run_id=run_id)
            row = db.execute("SELECT run_id,receipt FROM agent_stop_requests WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if row:
                if row[0] != run_id:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一停止请求不能改为其他诊断。", 409)
                return StopReceipt.model_validate_json(row[1])
            ended = body["status"] in _CLOSED or body.get("report_available")
            status = "CANCELLED" if body["status"] == "CANCELLED" else "ALREADY_FINISHED" if ended else "CANCELLING"
            receipt = StopReceipt(conversation_id=conversation_id, run_id=run_id, request_id=request_id, status=status)
            db.execute("INSERT INTO agent_stop_requests VALUES (?,?,?,?)", (conversation_id, request_id, run_id, _json(receipt)))
            if not ended:
                body.update(stop_requested=True, status="CANCELLING", intake_pending=False, current_questions=[])
                self._mark_unused(db, body)
                self._append(db, body, "run.stopping", {"status": "CANCELLING"}, "stopping")
                self._save(db, body)
        self._notify(conversation_id)
        return receipt

    def stop_requested(self, conversation_id, run_id):
        return self.get_run(conversation_id, run_id, deleted=True).get("stop_requested", False)

    def pending_stops(self):
        with self.repository.database_read() as db:
            return [{**json.loads(row[0]), "deleted": row[1] is not None} for row in db.execute(
                "SELECT r.body,c.deleted_at FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) WHERE r.status='CANCELLING'")]

    def finish_stop(self, conversation_id, run_id):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id, run_id=run_id, deleted=True)
            if body.get("report_available"):
                status = "ALREADY_FINISHED"
            else:
                self._close(db, body, "CANCELLED")
                status = "CANCELLED"
            body["stop_requested"] = False
            self._save(db, body)
            self._settle_stop_receipts(db, conversation_id, run_id, status)
        self._notify(conversation_id)
        return {"conversation_id": conversation_id, "run_id": run_id, "status": status}

    @staticmethod
    def _settle_stop_receipts(db, conversation_id, run_id, status):
        for key, raw in db.execute("SELECT request_id,receipt FROM agent_stop_requests WHERE conversation_id=? AND run_id=?", (conversation_id, run_id)).fetchall():
            receipt = {**json.loads(raw), "status": status}
            db.execute("UPDATE agent_stop_requests SET receipt=? WHERE conversation_id=? AND request_id=?", (_json(receipt), conversation_id, key))

    def request_delete(self, conversation_id, *, owner_key=None):
        return self._request_delete(conversation_id, owner_key=owner_key, preserve_completed_memory=False)

    def request_expiry(self, conversation_id):
        """Internal history retention keeps only already completed experience cards."""
        return self._request_delete(conversation_id, owner_key=None, preserve_completed_memory=True)

    def _request_delete(self, conversation_id, *, owner_key, preserve_completed_memory):
        with self.repository.database_transaction() as db:
            self.require_owner(conversation_id, owner_key, deleted=True)
            head = db.execute("SELECT deleted_at,cleanup_status FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            memory_store = getattr(self, "memory_store", None)
            # A user can delete a naturally expired conversation before its
            # tombstone disappears. That still revokes its surviving cards.
            if memory_store is not None and not preserve_completed_memory:
                memory_store.revoke_conversation(db, conversation_id)
            if head[0] is not None:
                return DeleteReceipt(conversation_id=conversation_id, status=head[1])
            db.execute("UPDATE agent_conversations SET deleted_at=?,cleanup_status='DELETING' WHERE conversation_id=?", (self._now(), conversation_id))
            if memory_store is not None and preserve_completed_memory:
                memory_store.expire_sources(db, conversation_id)
            db.execute("UPDATE archive_tasks SET status='CANCELLED' WHERE status IN ('PENDING','RUNNING') "
                "AND case_id IN (SELECT case_id FROM agent_conversation_runs WHERE conversation_id=?)", (conversation_id,))
            for run_id, in db.execute("SELECT run_id FROM agent_conversation_runs WHERE conversation_id=?", (conversation_id,)).fetchall():
                body = self._load(db, conversation_id, run_id=run_id, deleted=True)
                # Intake can fail while its Case is still waiting for input.
                # Deletion must settle that Case even if the Agent run ended.
                case_unfinished = body.get("case_id") is not None and body.get("case_status") not in (
                    _RESULT | {"FAILED", "CANCELLED", "INTERRUPTED"})
                if (body["status"] not in _CLOSED or case_unfinished) and not body.get("report_available"):
                    body.update(stop_requested=True, status="CANCELLING", intake_pending=False, current_questions=[])
                    self._mark_unused(db, body)
                    self._save(db, body)
            manifest = self._cleanup_context(db, conversation_id)
            db.execute("INSERT INTO agent_cleanup_jobs VALUES (?,'PENDING',?,NULL)", (conversation_id, _json(manifest)))
        self._notify(conversation_id)
        return DeleteReceipt(conversation_id=conversation_id, status="DELETING")

    @staticmethod
    def _cleanup_context(db, conversation_id):
        runs = db.execute("SELECT run_id,case_id,body FROM agent_conversation_runs WHERE conversation_id=?", (conversation_id,)).fetchall()
        bodies = [json.loads(row[2]) for row in runs]
        jobs = {body.get("job_id") for body in bodies}
        jobs.update(row[0] for row in db.execute("SELECT DISTINCT json_extract(body,'$.job_id') FROM agent_events WHERE conversation_id=?", (conversation_id,)))
        workspaces = {row[0] for row in db.execute("SELECT DISTINCT json_extract(payload,'$.workspace_id') FROM agent_dispatches WHERE conversation_id=? AND json_extract(payload,'$.workspace_id') IS NOT NULL", (conversation_id,))}
        workspaces.update(workspace for body in bodies for workspace in body.get("legacy_workspace_ids", []))
        return {"run_ids": [row[0] for row in runs], "case_ids": [row[1] for row in runs if row[1] is not None],
            "job_ids": sorted(job for job in jobs if job is not None),
            "workspace_ids": sorted(workspaces),
            "pending_stop": any(body.get("stop_requested", False) for body in bodies),
            "attachment_ids": [row[0] for row in db.execute("SELECT attachment_id FROM agent_attachments WHERE conversation_id=?", (conversation_id,))]}

    def cleanup_context(self, conversation_id):
        with self.repository.database_read() as db:
            return self._cleanup_context(db, conversation_id)

    def claim_cleanup(self):
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT conversation_id,manifest FROM agent_cleanup_jobs WHERE status IN ('PENDING','FAILED') ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE agent_cleanup_jobs SET status='RUNNING' WHERE conversation_id=?", (row[0],))
            return {"conversation_id": row[0], "manifest": json.loads(row[1])}

    def update_cleanup_manifest(self, conversation_id, manifest):
        with self.repository.database_transaction() as db:
            db.execute("UPDATE agent_cleanup_jobs SET manifest=? WHERE conversation_id=? AND status<>'DONE'", (_json(manifest), conversation_id))

    def fail_cleanup(self, conversation_id, code):
        with self.repository.database_transaction() as db:
            # Move a busy/error task to the tail so another conversation can drain.
            row = db.execute("SELECT manifest FROM agent_cleanup_jobs WHERE conversation_id=?", (conversation_id,)).fetchone()
            db.execute("DELETE FROM agent_cleanup_jobs WHERE conversation_id=?", (conversation_id,))
            db.execute("INSERT INTO agent_cleanup_jobs VALUES (?,'FAILED',?,?)", (conversation_id, row[0], str(code)[:100]))

    def finish_cleanup(self, conversation_id):
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT deleted_at,request_id,cleanup_status FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None or row[0] is None:
                raise AgentStoreError("AGENT_DELETE_REQUIRED", "只能清理已经删除的会话。", 409)
            if row[2] == "DELETED":
                return
            db.execute("DELETE FROM agent_attachment_imports WHERE run_id IN (SELECT run_id FROM agent_conversation_runs WHERE conversation_id=?)", (conversation_id,))
            for table in ("agent_message_adoptions", "agent_messages", "agent_events", "agent_dispatches", "agent_attachments", "agent_stop_requests", "agent_generic_restarts", "agent_conversation_runs"):
                db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
            digest = hashlib.sha256(row[1].encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO agent_deleted_requests VALUES (?,?)", (digest, conversation_id))
            db.execute("UPDATE agent_conversations SET body='{}',title='',request_id=?,case_id=NULL,create_request_id=NULL,cleanup_status='DELETED' WHERE conversation_id=?", ("deleted:" + digest, conversation_id))
            db.execute("UPDATE agent_cleanup_jobs SET status='DONE',manifest='{}',error_code=NULL WHERE conversation_id=?", (conversation_id,))

    def get_intake_state(self, conversation_id):
        """Read the private work latch without loading history or changing state."""
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            closed = body["status"] in _CLOSED or body.get("stop_requested") or body.get("_deleted") or body.get("report_available", False)
            commands = False if closed else db.execute(
                "SELECT 1 FROM agent_dispatches WHERE conversation_id=? AND run_id=? AND status='PENDING' AND epoch=? LIMIT 1",
                (conversation_id, body["run_id"], self.runtime_epoch),
            ).fetchone() is not None
            return {"pending": False if closed else body.get("intake_pending", False),
                    "covered_message_ids": list(body.get("intake_covered_message_ids", [])),
                    "pending_commands": commands,
                    "ready": not closed and (body.get("case_id") is None or
                        body.get("case_status") in {None, "WAITING_INPUT", "WAITING_ATTACHMENT"})}

    def _publish_intake_questions(self, db, body, questions):
        questions = list(questions)
        if body.get("current_questions") == questions:
            return
        body["current_questions"] = questions
        if questions:
            identity = [body.get("intake_question_revision"),
                        body.get("intake_covered_message_ids", []), questions]
            self._append(db, body, "assistant.question", {"questions": questions},
                          "intake-questions:" + hashlib.sha256(_json(identity).encode()).hexdigest())

    @staticmethod
    def _intake_questions(body):
        entries = body.get("intake_authoritative_question_entries")
        if entries is None:
            return body.get("intake_authoritative_questions", [])
        notice = body.get("intake_attachment_notice")
        return [notice["message"] if notice is not None and item["kind"] == "ATTACHMENT"
                and item["requirement_id"] == notice["requirement_id"] else item["prompt"]
                for item in entries]

    def finish_intake(self, conversation_id, covered_message_ids, questions=None, *,
                      attachment_notice=_KEEP_ATTACHMENT_NOTICE):
        """Settle one frozen source batch without covering concurrently new input.

        The message adoption status is independent of this coverage receipt:
        an already APPLIED create message can be extracted once without reversal.
        Case questions come from the latest transactionally saved projection.
        """
        if not isinstance(covered_message_ids, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in covered_message_ids
        ):
            raise ValueError("intake coverage requires message identifiers")
        if questions is not None and (not isinstance(questions, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in questions
        )):
            raise ValueError("intake questions require non-empty text")
        if attachment_notice is not _KEEP_ATTACHMENT_NOTICE and attachment_notice is not None and (
            not isinstance(attachment_notice, dict) or set(attachment_notice) != {"requirement_id", "message"}
            or any(not isinstance(value, str) or not value.strip() for value in attachment_notice.values())
        ):
            raise ValueError("attachment notice requires a requirement and a non-empty message")
        changed = False
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            if body["status"] in _CLOSED or body.get("stop_requested") or body.get("_deleted") or body.get("report_available"):
                return
            previous = _json(body)
            if attachment_notice is not _KEEP_ATTACHMENT_NOTICE:
                body["intake_attachment_notice"] = attachment_notice
            messages = [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? ORDER BY event_sequence", (conversation_id, body["run_id"]))]
            known = {message["message_id"] for message in messages}
            covered = set(body.get("intake_covered_message_ids", [])) | set(covered_message_ids)
            if not covered <= known:
                raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "整理记录引用了不属于本会话的消息。", 404)
            body["intake_covered_message_ids"] = [message["message_id"] for message in messages
                                                   if message["message_id"] in covered]
            pending = any(message["status"] != "UNUSED" and message["message_id"] not in covered
                          for message in messages)
            body["intake_pending"] = pending
            waiting = body.get("case_status") in {"WAITING_INPUT", "WAITING_ATTACHMENT"}
            if pending:
                body["current_questions"] = []
                if waiting or body.get("case_id") is None:
                    body["status"] = "INTAKE"
            elif waiting or (questions is not None and body.get("case_id") is None):
                body["status"] = "WAITING_INPUT"
                self._publish_intake_questions(db, body, questions if questions is not None else
                                              self._intake_questions(body))
            elif body.get("case_id") is not None:
                body["status"] = "RUNNING"
                body["current_questions"] = []
            if _json(body) != previous:
                self._save(db, body)
                changed = True
        if changed:
            self._notify(conversation_id)

    def set_draft(self, conversation_id, draft):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            self._ensure_open(body)
            body["draft"] = draft
            self._save(db, body)

    def update_intake(self, conversation_id, draft, questions, status="WAITING_INPUT"):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            self._ensure_open(body)
            if status not in {"INTAKE", "WAITING_INPUT", "RUNNING"}:
                raise ValueError("invalid intake status")
            pending = body.get("intake_pending", False)
            body.update(draft=draft, current_questions=[] if pending else list(questions),
                        status="INTAKE" if pending and status == "WAITING_INPUT" else status)
            if questions and not pending:
                self._append(db, body, "assistant.question", {"questions": list(questions)},
                             "questions:" + hashlib.sha256(_json([draft, questions]).encode()).hexdigest())
            self._save(db, body)
        self._notify(conversation_id)

    def pending_conversations(self):
        with self.repository.database_read() as db:
            # Both branches read only indexed work. Settled histories, empty
            # conversations and runs waiting for a Case stage need no polling.
            eligible = ("c.deleted_at IS NULL AND c.status NOT IN ('COMPLETED','FAILED','INTERRUPTED','CANCELLED','CANCELLING') "
                "AND coalesce(json_extract(c.body,'$.report_available'),0)=0 AND coalesce(json_extract(c.body,'$.stop_requested'),0)=0")
            # Force the small ready-work index: ORDER BY rowid can otherwise
            # make SQLite prefer a full conversation scan to avoid sorting.
            rows = db.execute("SELECT c.conversation_id,c.rowid AS position FROM agent_conversations c "
                "INDEXED BY agent_conversations_intake_work WHERE " + eligible +
                " AND json_extract(c.body,'$.intake_pending')=1 "
                "AND (c.case_id IS NULL OR json_extract(c.body,'$.case_status') IS NULL OR json_extract(c.body,'$.case_status') IN ('WAITING_INPUT','WAITING_ATTACHMENT')) "
                "UNION SELECT c.conversation_id,c.rowid AS position FROM agent_dispatches d "
                "JOIN agent_conversations c ON c.conversation_id=d.conversation_id AND c.current_run_id=d.run_id "
                "WHERE d.status='PENDING' AND d.epoch=? AND " + eligible + " ORDER BY position", (self.runtime_epoch,))
            return [row[0] for row in rows]

    def pending_messages(self, conversation_id):
        return [message for message in self.get_conversation(conversation_id).messages if message.status in {"QUEUED", "PROCESSING"}]

    def set_message_status(self, conversation_id, message_id, status, notice=None):
        if status not in {"PROCESSING", "APPLIED", "UNUSED"}:
            raise ValueError("invalid message transition")
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? AND message_id=?", (conversation_id, body["run_id"], message_id)).fetchone()
            if not row:
                raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "消息不存在。", 404)
            message = self._message(row[0], body["run_id"])
            if message.status == status:
                return
            if message.status in {"APPLIED", "UNUSED"}:
                raise AgentStoreError("AGENT_MESSAGE_FINALIZED", "已处理消息的状态不能更改。", 409)
            self._ensure_open(body)
            message.status = status
            message.notice = notice if notice is not None else (_UNUSED_NOTICE if status == "UNUSED" else None)
            db.execute("UPDATE agent_messages SET body=? WHERE message_id=?", (_json(message), message_id))
            self._append(db, body, "message.updated", {"message_id": message_id, "status": status, "notice": message.notice},
                         "message-status:" + message_id + ":" + status)
            self._save(db, body)
        self._notify(conversation_id)

    def begin_adoption(self, conversation_id, message_id, dispatch_id):
        """Bind one processing message to an already frozen core command."""
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            dispatch = db.execute("SELECT epoch,payload FROM agent_dispatches WHERE conversation_id=? AND run_id=? AND dispatch_id=?",
                                  (conversation_id, body["run_id"], dispatch_id)).fetchone()
            if dispatch is None or dispatch[0] != self.runtime_epoch:
                raise AgentStoreError("AGENT_DISPATCH_NOT_FOUND", "消息尚未关联当前运行中的有效命令。", 409)
            payload = json.loads(dispatch[1])
            if payload.get("operation") not in {"CreateCase", "SubmitSupplement"} or not payload.get("command", {}).get("idempotency_key"):
                raise AgentStoreError("AGENT_ADOPTION_INVALID", "该命令不能采纳用户消息。", 409)
            message_row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? AND message_id=?",
                                     (conversation_id, body["run_id"], message_id)).fetchone()
            if message_row is None:
                raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "消息不存在。", 404)
            message = json.loads(message_row[0])
            if message["status"] == "APPLIED":
                return
            if message["status"] != "PROCESSING":
                raise AgentStoreError("AGENT_ADOPTION_INVALID", "只有正在处理的消息可以关联命令。", 409)
            self._ensure_open(body)
            previous = db.execute("SELECT conversation_id,dispatch_id FROM agent_message_adoptions WHERE message_id=?", (message_id,)).fetchone()
            if previous and previous != (conversation_id, dispatch_id):
                raise AgentStoreError("AGENT_ADOPTION_CONFLICT", "消息已经关联另一项命令。", 409)
            db.execute("INSERT OR IGNORE INTO agent_message_adoptions VALUES (?,?,?)",
                       (message_id, conversation_id, dispatch_id))

    def finish_adoption(self, conversation_id, message_id, accepted: bool):
        """Resolve a command response without reversing a committed decision."""
        if not isinstance(accepted, bool):
            raise TypeError("accepted must be a bool")
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            message_row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? AND message_id=?", (conversation_id, body["run_id"], message_id)).fetchone()
            if message_row is None:
                raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "消息不存在。", 404)
            message = json.loads(message_row[0])
            if message["status"] not in {"APPLIED", "UNUSED"}:
                if db.execute("SELECT 1 FROM agent_message_adoptions WHERE conversation_id=? AND message_id=?", (conversation_id, message_id)).fetchone() is None:
                    raise AgentStoreError("AGENT_ADOPTION_INVALID", "消息尚未关联需要确认的命令。", 409)
                self._finalize_message(db, body, message, "APPLIED" if accepted else "UNUSED")
                self._save(db, body)
            db.execute("DELETE FROM agent_message_adoptions WHERE conversation_id=? AND message_id=?", (conversation_id, message_id))
        self._notify(conversation_id)

    def _finalize_message(self, db, body, message, status):
        message.update(status=status, notice=_UNUSED_NOTICE if status == "UNUSED" else None)
        db.execute("UPDATE agent_messages SET body=? WHERE message_id=?", (_json(message), message["message_id"]))
        self._append(db, body, "message.updated", {"message_id": message["message_id"], "status": status,
            "notice": message["notice"]}, "message-status:" + message["message_id"] + ":" + status)

    def _accept_committed_adoptions(self, db, body, state):
        batches = list(db.execute("SELECT payload FROM agent_dispatches WHERE conversation_id=? AND run_id=? "
            "AND json_type(payload,'$.generic_message_ids')='array'", (body["conversation_id"], body["run_id"])))
        for (raw,) in batches:
            payload = json.loads(raw)
            if payload.get("operation") != "SubmitSupplement":
                continue
            key = payload["command"]["idempotency_key"]
            if not any(record.operation == "SubmitSupplement" and record.idempotency_key == key
                    and record.case_id == body.get("case_id") for record in state.idempotency_records.values()):
                continue
            adopted = body.setdefault("generic_adopted_message_ids", [])
            for message_id in payload["generic_message_ids"]:
                row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND run_id=? AND message_id=?",
                    (body["conversation_id"], body["run_id"], message_id)).fetchone()
                if row is None:
                    raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "补充描述引用了不属于本次诊断的消息。", 409)
                message = json.loads(row[0])
                if message["status"] in {"QUEUED", "PROCESSING"}:
                    self._finalize_message(db, body, message, "APPLIED")
                if message_id not in adopted:
                    adopted.append(message_id)
        rows = list(db.execute("SELECT a.message_id,d.payload,m.body FROM agent_message_adoptions a "
            "JOIN agent_dispatches d ON d.dispatch_id=a.dispatch_id "
            "JOIN agent_messages m ON m.message_id=a.message_id "
            "WHERE a.conversation_id=? AND m.run_id=? AND d.run_id=?", (body["conversation_id"], body["run_id"], body["run_id"])))
        for message_id, payload_json, message_json in rows:
            payload = json.loads(payload_json)
            operation, key = payload["operation"], payload["command"]["idempotency_key"]
            accepted = any(record.operation == operation and record.idempotency_key == key
                           and record.case_id == body.get("case_id") for record in state.idempotency_records.values())
            if accepted:
                message = json.loads(message_json)
                if message["status"] not in {"APPLIED", "UNUSED"}:
                    self._finalize_message(db, body, message, "APPLIED")
                db.execute("DELETE FROM agent_message_adoptions WHERE message_id=?", (message_id,))

    def expect_case(self, conversation_id, create_request_id):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            self._ensure_open(body)
            current = db.execute("SELECT create_request_id FROM agent_conversation_runs WHERE run_id=?", (body["run_id"],)).fetchone()[0]
            if current is not None and current != create_request_id:
                raise AgentStoreError("AGENT_CASE_ALREADY_BOUND", "会话已经关联另一项任务。", 409)
            db.execute("UPDATE agent_conversation_runs SET create_request_id=? WHERE run_id=?", (create_request_id, body["run_id"]))
            db.execute("UPDATE agent_conversations SET create_request_id=? WHERE conversation_id=? AND current_run_id=?", (create_request_id, conversation_id, body["run_id"]))

    def bind_case(self, conversation_id, case_id):
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            if body.get("case_id") not in {None, case_id}:
                raise AgentStoreError("AGENT_CASE_ALREADY_BOUND", "会话已经关联另一项任务。", 409)
            body["case_id"] = case_id
            self._save(db, body)

    def record_dispatch(self, conversation_id, dispatch_id, payload):
        serialized = _json(payload)
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            existing = db.execute("SELECT conversation_id,epoch,status,payload,result,run_id FROM agent_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if existing:
                if existing[0] != conversation_id or existing[3] != serialized or existing[5] != body["run_id"]:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "派发标识已经用于不同命令。", 409)
                if existing[1] != self.runtime_epoch and existing[2] != "COMPLETED":
                    raise AgentStoreError("AGENT_DISPATCH_INTERRUPTED", "命令已经随服务重启中断，请另建任务。", 409)
                return {"dispatch_id": dispatch_id, "epoch": existing[1], "status": existing[2],
                        "payload": json.loads(existing[3]), "result": json.loads(existing[4]) if existing[4] else None}
            self._ensure_open(body)
            db.execute("INSERT INTO agent_dispatches(dispatch_id,conversation_id,epoch,status,payload,result,run_id) VALUES (?,?,?,'PENDING',?,NULL,?)",
                       (dispatch_id, conversation_id, self.runtime_epoch, serialized, body["run_id"]))
            return {"dispatch_id": dispatch_id, "epoch": self.runtime_epoch, "status": "PENDING", "payload": payload, "result": None}

    def get_dispatch(self, conversation_id, dispatch_id):
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            row = db.execute("SELECT epoch,status,payload,result FROM agent_dispatches WHERE conversation_id=? AND run_id=? AND dispatch_id=?",
                             (conversation_id, body["run_id"], dispatch_id)).fetchone()
            return None if row is None else {"dispatch_id": dispatch_id, "epoch": row[0],
                "status": row[1], "payload": json.loads(row[2]), "result": json.loads(row[3]) if row[3] else None}

    def pending_dispatches(self, conversation_id):
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            return [{"dispatch_id": row[0], "epoch": self.runtime_epoch, "status": "PENDING",
                "payload": json.loads(row[1]), "result": json.loads(row[2]) if row[2] else None}
                for row in db.execute("SELECT dispatch_id,payload,result FROM agent_dispatches WHERE conversation_id=? AND run_id=? AND epoch=? AND status='PENDING' ORDER BY rowid",
                    (conversation_id, body["run_id"], self.runtime_epoch))]

    def complete_dispatch(self, dispatch_id, result=None, status="COMPLETED"):
        if status not in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
            raise ValueError("invalid dispatch status")
        with self.repository.database_transaction() as db:
            db.execute("UPDATE agent_dispatches SET status=?,result=? WHERE dispatch_id=? AND epoch=? AND status='PENDING'",
                       (status, _json(result), dispatch_id, self.runtime_epoch))

    def append_progress(self, conversation_id, stage, *, dedupe_key=None):
        if stage not in _PROGRESS:
            raise ValueError("unknown public progress stage")
        return self.append_event(conversation_id, "agent.progress", {"stage": stage, "message": _PROGRESS[stage]},
                                 dedupe_key=dedupe_key or "progress:" + stage + ":" + self._id("event"))

    def append_case_progress(self, case_id, job_id, stage):
        if stage not in _PROGRESS:
            raise ValueError("unknown public progress stage")
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT conversation_id,run_id FROM agent_conversation_runs WHERE case_id=?", (case_id,)).fetchone()
            if not row:
                return None
            body = self._load(db, row[0], run_id=row[1], deleted=True)
            if body["status"] in _CLOSED or body.get("stop_requested") or body.get("_deleted") or body.get("report_available"):
                return None
            event = self._append(db, body, "agent.progress", {"stage": stage, "message": _PROGRESS[stage]},
                                 "progress:" + job_id + ":" + stage)
            # The supplied job identifies the actual execution node; the active
            # Case may already have advanced before this notification arrives.
            event.job_id = job_id
            db.execute("UPDATE agent_events SET body=? WHERE conversation_id=? AND sequence=?",
                       (_json(event), row[0], event.sequence))
            self._save(db, body)
        self._notify(row[0])
        return event

    def append_event(self, conversation_id, event_type, data, *, dedupe_key):
        # Public extension points intentionally accept no arbitrary model or
        # tool output. Terminal event types belong exclusively to projection.
        allowed = {"agent.progress": {"stage", "message"}, "assistant.question": {"questions"}}
        if event_type not in allowed or set(data) != allowed[event_type]:
            raise ValueError("event payload is not on the public allowlist")
        if event_type == "agent.progress" and data.get("message") != _PROGRESS.get(data.get("stage")):
            raise ValueError("progress text must come from the public catalog")
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            self._ensure_open(body)
            event = self._append(db, body, event_type, data, dedupe_key)
            self._save(db, body)
        self._notify(conversation_id)
        return event

    def fail_conversation(self, conversation_id, code="AGENT_EXECUTION_FAILED", *, interrupted=False, phase="AGENT", source_details=()):
        # Internal exception text is deliberately not an argument.
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            if body["status"] in _CLOSED or body.get("stop_requested") or body.get("_deleted") or body.get("report_available"):
                return
            self._close(db, body, "INTERRUPTED" if interrupted else "FAILED", code,
                failure=public_failure(code, phase=phase, source_details=source_details))
            self._save(db, body)
        self._notify(conversation_id)

    def _close(self, db, body, status, code=None, *, failure=None):
        body.setdefault("completed_at", self._now())
        body["status"] = status
        body["intake_pending"] = False
        body["current_questions"] = []
        body["intake_attachment_notice"] = None
        if status in {"FAILED", "INTERRUPTED"} and body.get("failure") is None:
            failure = failure or public_failure(code or ("AGENT_INTERRUPTED" if status == "INTERRUPTED" else None),
                phase="RESTART" if status == "INTERRUPTED" else "AGENT")
            body["failure"] = failure.model_dump(mode="json")
            log_event("agent.failure_projected", conversation_id=body["conversation_id"],
                case_id=body.get("case_id"), job_id=body.get("job_id"),
                code=failure.code, **{item["field"]: item["actual"] for item in failure.details})
        self._mark_unused(db, body)
        if status == "INTERRUPTED":
            self._append(db, body, "conversation.interrupted", {"code": "AGENT_INTERRUPTED", "message": "服务已重启，本次任务已中断，请重新发起。"}, "interrupted")
        elif status == "FAILED":
            self._append(db, body, "agent.failed", {"code": "AGENT_EXECUTION_FAILED", "message": _SAFE_FAILURE}, "failed")
        self._append(db, body, "conversation.completed", {"status": status}, "completed")

    def _mark_unused(self, db, body):
        for row in list(db.execute("SELECT message_id,body FROM agent_messages WHERE conversation_id=? AND run_id=?", (body["conversation_id"], body["run_id"]))):
            message = json.loads(row[1])
            if message["status"] in {"QUEUED", "PROCESSING"}:
                self._finalize_message(db, body, message, "UNUSED")
            db.execute("DELETE FROM agent_message_adoptions WHERE message_id=?", (row[0],))

    def _project_state(self, db, state):
        for case_id, aggregate in state.cases.items():
            row = db.execute("SELECT conversation_id,run_id FROM agent_conversation_runs WHERE case_id=?", (case_id,)).fetchone()
            if not row:
                for record in state.idempotency_records.values():
                    if str(record.operation) == "CreateCase" and record.case_id == case_id:
                        row = db.execute("SELECT conversation_id,run_id FROM agent_conversation_runs WHERE create_request_id=?", (record.idempotency_key,)).fetchone()
                        if row:
                            break
            if row:
                body = self._load(db, row[0], run_id=row[1], deleted=True)
                body["case_id"] = case_id
                self._accept_generic_restarts(db, body, state)
                self._accept_committed_adoptions(db, body, state)
                self._project_case(db, body, aggregate)

    def _project_case(self, db, body, aggregate):
        case = aggregate.case
        if body["status"] in _CLOSED:
            return
        body.update(case_id=case.case_id, job_id=case.active_job_id, case_status=case.status.value,
                    archive_status=case.archive_status,
                    case_has_selected_skill=getattr(case, "selected_skill_ref", None) is not None)
        self._append(db, body, "case.updated", {"status": case.status.value, "case_revision": case.case_revision},
                     "case:" + str(case.case_revision))
        if body.get("stop_requested") and case.status.value not in _RESULT:
            body.update(status="CANCELLING", intake_pending=False, current_questions=[])
            self._save(db, body)
            return
        if case.status.value in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
            entries = [{"requirement_id": requirement.requirement_id, "kind": requirement.kind.value,
                        "prompt": requirement.prompt} for requirement in case.diagnosis_state.pending_requirements
                       if requirement.status.value == "OPEN"]
            body["intake_authoritative_question_entries"] = entries
            body["intake_authoritative_questions"] = [item["prompt"] for item in entries]
            notice = body.get("intake_attachment_notice")
            if notice is not None and not any(item["kind"] == "ATTACHMENT"
                    and item["requirement_id"] == notice["requirement_id"] for item in entries):
                body["intake_attachment_notice"] = None
            questions = self._intake_questions(body)
            body["intake_question_revision"] = case.case_revision
            if body.get("intake_pending", False):
                body["status"] = "INTAKE"
                body["current_questions"] = []
            else:
                body["status"] = "WAITING_INPUT"
                self._publish_intake_questions(db, body, questions)
        elif case.status.value in _RESULT:
            summaries = project_artifact_summaries(case, aggregate.artifacts.values(), include_internal=False)
            reports = [item for item in summaries if item.kind.value in {"USER_RESULT", "GENERIC_REPORT"}]
            generic = case.generic_result_v2 or case.generic_result
            if reports or generic is not None:
                body["report_available"] = True
                body["stop_requested"] = False
                self._settle_stop_receipts(db, body["conversation_id"], body["run_id"], "ALREADY_FINISHED")
                body["intake_pending"] = False
                body["current_questions"] = []
                body["intake_attachment_notice"] = None
                result_field = ("generic_result_v2" if case.generic_result_v2 is not None else
                                "generic_result" if case.generic_result is not None else
                                "unresolved_result" if case.status.value == "UNRESOLVED" else "final_result")
                self._append(db, body, "result.available", {"status": case.status.value,
                    "artifacts": [item.model_dump(mode="json") for item in reports],
                    "result_field": result_field}, "result")
                self._mark_unused(db, body)
            self._append(db, body, "archive.updated", {"status": case.archive_status,
                "artifacts": [item.model_dump(mode="json") for item in summaries if item.kind.value in {"USER_RESULT_ARCHIVE", "AUDIT_BUNDLE"}]},
                "archive:" + case.archive_status)
            if case.archive_status != "PENDING":
                self._close(db, body, "COMPLETED")
            else:
                body["status"] = "RUNNING"
        elif case.status.value in {"FAILED", "CANCELLED", "INTERRUPTED"}:
            if case.status.value == "CANCELLED":
                if body.get("stop_requested"):
                    body["status"] = "CANCELLING"
                else:
                    self._close(db, body, "CANCELLED")
                self._save(db, body)
                return
            case_failure = case.failure
            outcome = aggregate.outcomes.get(case_failure.source_outcome_id) if case_failure is not None else None
            execution_failure = outcome.error if outcome is not None else None
            if execution_failure is None and case_failure is not None:
                records = [record for record in aggregate.execution_failure_records.values()
                    if record.job_id == case_failure.source_job_id and record.failure.code == case_failure.code
                    and (case_failure.diagnostic_id is None or record.failure.diagnostic_id == case_failure.diagnostic_id)]
                if records:
                    execution_failure = max(records, key=lambda item: (item.recorded_at, item.failure_id)).failure
            if case_failure is None and case.status.value == "INTERRUPTED":
                execution_failure = interrupted_execution_failure(aggregate)
            failure = public_failure(
                case_failure.code if case_failure is not None else
                    execution_failure.code if execution_failure is not None else "AGENT_INTERRUPTED",
                phase=execution_failure.stage if execution_failure is not None else "AGENT",
                diagnostic_id=case_failure.diagnostic_id if case_failure is not None else
                    execution_failure.diagnostic_id if execution_failure is not None else None,
                reason_code=case_failure.reason_code if case_failure is not None else
                    execution_failure.reason_code if execution_failure is not None else None,
                source_details=execution_failure.details if execution_failure is not None else ())
            self._close(db, body, "INTERRUPTED" if case.status.value == "INTERRUPTED" else "FAILED", failure=failure)
        else:
            body["status"] = "RUNNING"
            body["current_questions"] = []
        self._save(db, body)

    def _case_committed(self, case_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT conversation_id FROM agent_conversation_runs WHERE case_id=?", (case_id,)).fetchone()
        if row:
            self._notify(row[0])

    def recover(self):
        """Recover public history without restarting any model or active Case."""
        changed = []
        with self.repository.database_transaction() as db:
            for conversation_id, run_id, epoch, case_id in list(db.execute(
                    "SELECT r.conversation_id,r.run_id,r.epoch,r.case_id FROM agent_conversation_runs r "
                    "JOIN agent_conversations c USING(conversation_id) WHERE c.deleted_at IS NULL "
                    "AND r.status NOT IN ('COMPLETED','FAILED','INTERRUPTED','CANCELLED')")):
                if epoch == self.runtime_epoch:
                    continue
                body = self._load(db, conversation_id, run_id=run_id)
                persisted = db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (case_id,)).fetchone() if case_id else None
                if persisted:
                    state = StateFile.model_validate_json(persisted[0])
                    self._accept_committed_adoptions(db, body, state)
                    self._project_case(db, body, state.cases[case_id])
                elif not body.get("stop_requested") and db.execute("SELECT 1 FROM agent_messages WHERE conversation_id=? AND run_id=? LIMIT 1", (conversation_id, run_id)).fetchone():
                    self._close(db, body, "INTERRUPTED")
                    self._save(db, body)
                db.execute("UPDATE agent_conversation_runs SET epoch=? WHERE run_id=?", (self.runtime_epoch, run_id))
                db.execute("UPDATE agent_conversations SET epoch=? WHERE conversation_id=? AND current_run_id=?", (self.runtime_epoch, conversation_id, run_id))
                db.execute("UPDATE agent_dispatches SET status='INTERRUPTED' WHERE run_id=? AND status='PENDING' AND epoch<>?", (run_id, self.runtime_epoch))
                changed.append(conversation_id)
        for conversation_id in changed:
            self._notify(conversation_id)

    def reserve_attachment(self, conversation_id, request_id, name, content_type, size, sha256, storage_path=None,
                           *, max_total_bytes=5 * 1024**3, max_file_bytes=2684354560):
        fingerprint = hashlib.sha256(_json([name, content_type, size, sha256]).encode()).hexdigest()
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            row = db.execute("SELECT fingerprint,body,storage_path FROM agent_attachments WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一附件请求的内容不能更改。", 409)
                return AttachmentRecord(**json.loads(row[1]), storage_path=row[2])
            uploads = [json.loads(row[0]) for row in db.execute("SELECT body FROM agent_attachments WHERE conversation_id=?", (conversation_id,))]
            if not 0 < size <= max_file_bytes or len(uploads) >= 20 or sum(item["size"] for item in uploads) + size > max_total_bytes:
                raise AgentStoreError("AGENT_ATTACHMENT_LIMIT", "附件大小或数量超出本次会话限制。", 413)
            attachment = AgentAttachment(attachment_id=self._id("agentattachment"), conversation_id=conversation_id,
                request_id=request_id, name=name, content_type=content_type, size=size, sha256=sha256, created_at=self._now())
            db.execute("INSERT INTO agent_attachments VALUES (?,?,?,?,?,?)", (attachment.attachment_id,
                conversation_id, request_id, fingerprint, _json(attachment), storage_path))
            self._append(db, body, "attachment.updated", attachment.model_dump(mode="json"), "attachment:" + attachment.attachment_id + ":RESERVED")
            self._save(db, body)
        self._notify(conversation_id)
        return AttachmentRecord(**attachment.model_dump(), storage_path=storage_path)

    def get_attachment(self, attachment_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT body,storage_path FROM agent_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            if not row:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_FOUND", "附件不存在。", 404)
            attachment = json.loads(row[0])
            self.require_owner(attachment["conversation_id"], None)
            return AttachmentRecord(**attachment, storage_path=row[1])

    def set_attachment_status(self, attachment_id, status, storage_path=None, *, case_attachment_id=None):
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT body,storage_path FROM agent_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            if not row:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_FOUND", "附件不存在。", 404)
            attachment = AgentAttachment.model_validate_json(row[0])
            body = self._load(db, attachment.conversation_id)
            if attachment.status == status and (case_attachment_id is None or attachment.case_attachment_id == case_attachment_id):
                return AttachmentRecord(**attachment.model_dump(), storage_path=row[1])
            transitions = {"RESERVED": {"UPLOADING", "READY", "FAILED"}, "UPLOADING": {"READY", "FAILED"},
                           "FAILED": {"UPLOADING", "READY"}, "READY": {"IMPORTED"}, "IMPORTED": set()}
            if status not in transitions[attachment.status]:
                raise AgentStoreError("AGENT_ATTACHMENT_STATE_CONFLICT", "附件状态已变化，请刷新后重试。", 409)
            attachment.status = status
            if case_attachment_id:
                attachment.case_attachment_id = case_attachment_id
            final_path = storage_path if storage_path is not None else row[1]
            db.execute("UPDATE agent_attachments SET body=?,storage_path=? WHERE attachment_id=?", (_json(attachment), final_path, attachment_id))
            # An upload may fail and then be explicitly retried. Each actual
            # state transition must be visible; repeating the same request in
            # the same state returned above without creating another event.
            self._append(db, body, "attachment.updated", attachment.model_dump(mode="json"),
                         "attachment:" + attachment_id + ":" + status + ":" + str(body["last_event_id"] + 1))
            self._save(db, body)
        self._notify(attachment.conversation_id)
        return AttachmentRecord(**attachment.model_dump(), storage_path=final_path)

    def complete_attachment(self, attachment_id, storage_path=None):
        return self.set_attachment_status(attachment_id, "READY", storage_path)

    def get_attachment_import(self, attachment_id, run_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT case_attachment_id FROM agent_attachment_imports WHERE attachment_id=? AND run_id=?", (attachment_id, run_id)).fetchone()
            return None if row is None else row[0]

    def bind_attachment(self, attachment_id, case_attachment_id, *, run_id=None):
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT body,storage_path FROM agent_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            if row is None:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_FOUND", "附件不存在。", 404)
            attachment = AgentAttachment.model_validate_json(row[0])
            body = self._load(db, attachment.conversation_id, run_id=run_id)
            bound = db.execute("SELECT case_attachment_id FROM agent_attachment_imports WHERE attachment_id=? AND run_id=?", (attachment_id, body["run_id"])).fetchone()
            if bound is not None:
                if bound[0] != case_attachment_id:
                    raise AgentStoreError("AGENT_ATTACHMENT_STATE_CONFLICT", "本轮诊断已经导入了该附件。", 409)
                return AttachmentRecord(**attachment.model_dump(), storage_path=row[1])
            self._ensure_open(body)
            if attachment.status not in {"READY", "IMPORTED"}:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件尚未上传完成。", 409)
            db.execute("INSERT INTO agent_attachment_imports VALUES (?,?,?)", (body["run_id"], attachment_id, case_attachment_id))
            attachment.status = "IMPORTED"
            attachment.case_attachment_id = case_attachment_id
            db.execute("UPDATE agent_attachments SET body=? WHERE attachment_id=?", (_json(attachment), attachment_id))
            self._append(db, body, "attachment.updated", attachment.model_dump(mode="json"), "attachment:" + attachment_id + ":IMPORTED")
            self._save(db, body)
        self._notify(attachment.conversation_id)
        return AttachmentRecord(**attachment.model_dump(), storage_path=row[1])
