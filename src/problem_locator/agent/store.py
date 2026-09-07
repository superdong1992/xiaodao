"""Durable Agent conversations and transactionally published public events.

All database access uses the core repository's connection and lock. Projection
never reads the repository or obtains a Case lock while holding the DB lock.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from problem_locator.contracts import StateFile
from problem_locator.application.projection import project_artifact_summaries
from .models import (AgentAttachment, AgentEvent, AgentMessage, AgentStoreError,
                     AttachmentRecord, CreateConversationRequest, ConversationReceipt, ConversationView,
                     MessageReceipt, SendMessageRequest, PUBLIC_PROGRESS_MESSAGES)

_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED"}
_RESULT = {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED"}
_PROGRESS = PUBLIC_PROGRESS_MESSAGES
_SAFE_FAILURE = "本次定位未能完成，请重新发起任务。"
_QUEUED_NOTICE = "已收到，尚未用于本次诊断。"
_UNUSED_NOTICE = "这条消息未用于本次诊断，请另建任务继续。"


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
        with repository.database_read() as db:
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
                CREATE TABLE IF NOT EXISTS agent_dispatches (
                    dispatch_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    epoch TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS agent_message_adoptions (
                    message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    dispatch_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_attachments (
                    attachment_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL, storage_path TEXT,
                    UNIQUE(conversation_id, request_id));
            """)
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

    @staticmethod
    def _load(db, conversation_id):
        row = db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        return json.loads(row[0])

    def _save(self, db, body):
        body["updated_at"] = self._now()
        db.execute("UPDATE agent_conversations SET status=?,case_id=?,body=? WHERE conversation_id=?",
                   (body["status"], body.get("case_id"), _json(body), body["conversation_id"]))

    @staticmethod
    def _ensure_open(body):
        if body["status"] in _CLOSED or body.get("report_available"):
            raise AgentStoreError("AGENT_CONVERSATION_CLOSED", "本次任务已经结束，请另建任务。", 409)

    def _append(self, db, body, event_type, data, dedupe_key):
        row = db.execute("SELECT body FROM agent_events WHERE conversation_id=? AND dedupe_key=?",
                         (body["conversation_id"], dedupe_key)).fetchone()
        if row:
            return AgentEvent.model_validate_json(row[0])
        sequence = body["last_event_id"] + 1
        event = AgentEvent(sequence=sequence, conversation_id=body["conversation_id"],
            case_id=body.get("case_id"), job_id=body.get("job_id"), type=event_type,
            created_at=self._now(), data=data)
        db.execute("INSERT INTO agent_events VALUES (?,?,?,?)",
                   (body["conversation_id"], sequence, dedupe_key, _json(event)))
        body["last_event_id"] = sequence
        return event

    def create_conversation(self, request_id):
        request_id = CreateConversationRequest(request_id=request_id).request_id
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT conversation_id FROM agent_conversations WHERE request_id=?", (request_id,)).fetchone()
            if row:
                return ConversationReceipt(conversation_id=row[0], request_id=request_id)
            conversation_id, now = self._id("conversation"), self._now()
            body = dict(conversation_id=conversation_id, status="INTAKE", case_id=None, job_id=None,
                        case_status=None, archive_status="NOT_REQUIRED", current_questions=[],
                        last_event_id=0, created_at=now, updated_at=now, draft={}, report_available=False)
            db.execute("INSERT INTO agent_conversations VALUES (?,?,?,?,?,NULL,NULL)",
                       (conversation_id, request_id, self.runtime_epoch, "INTAKE", _json(body)))
        return ConversationReceipt(conversation_id=conversation_id, request_id=request_id)

    def submit_message(self, conversation_id, request_id, text="", attachment_ids=None):
        request = SendMessageRequest(request_id=request_id, text=text, attachment_ids=attachment_ids or [])
        fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            previous = db.execute("SELECT fingerprint,receipt FROM agent_messages WHERE conversation_id=? AND request_id=?",
                                  (conversation_id, request_id)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
                return MessageReceipt.model_validate_json(previous[1])
            self._ensure_open(body)
            for attachment_id in request.attachment_ids:
                row = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=? AND conversation_id=?",
                                 (attachment_id, conversation_id)).fetchone()
                if not row or json.loads(row[0])["status"] not in {"READY", "IMPORTED"}:
                    raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件不存在、未上传完成或不属于本会话。", 409)
            count = db.execute("SELECT count(*) FROM agent_messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            if count >= 200:
                raise AgentStoreError("AGENT_MESSAGE_LIMIT", "本次会话消息已达上限，请另建任务。", 409)
            message = AgentMessage(message_id=self._id("message"), request_id=request_id,
                text=request.text, attachment_ids=request.attachment_ids, status="QUEUED", created_at=self._now(),
                notice=_QUEUED_NOTICE)
            event = self._append(db, body, "message.accepted", message.model_dump(mode="json"), "message:" + message.message_id)
            receipt = MessageReceipt(conversation_id=conversation_id, message_id=message.message_id,
                request_id=request_id, event_id=event.sequence)
            db.execute("INSERT INTO agent_messages VALUES (?,?,?,?,?,?)",
                       (message.message_id, conversation_id, request_id, fingerprint, _json(message), _json(receipt)))
            self._save(db, body)
        self._notify(conversation_id)
        return receipt

    def get_conversation(self, conversation_id):
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            public = {key: value for key, value in body.items() if key in ConversationView.model_fields}
            public["messages"] = [AgentMessage.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_messages WHERE conversation_id=? ORDER BY rowid", (conversation_id,))]
            public["attachments"] = [AgentAttachment.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_attachments WHERE conversation_id=? ORDER BY rowid", (conversation_id,))]
            return ConversationView.model_validate(public)

    def list_events(self, conversation_id, after=0, limit=100):
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AgentStoreError("AGENT_INVALID_CURSOR", "事件游标或批量大小无效。")
        with self.repository.database_read() as db:
            body = self._load(db, conversation_id)
            if after > body["last_event_id"]:
                raise AgentStoreError("AGENT_INVALID_CURSOR", "事件游标超出会话范围。", 409)
            return [AgentEvent.model_validate_json(row[0]) for row in db.execute(
                "SELECT body FROM agent_events WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (conversation_id, after, limit))]

    def get_draft(self, conversation_id):
        with self.repository.database_read() as db:
            return self._load(db, conversation_id).get("draft", {})

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
            body.update(draft=draft, current_questions=list(questions), status=status)
            if questions:
                self._append(db, body, "assistant.question", {"questions": list(questions)},
                             "questions:" + hashlib.sha256(_json([draft, questions]).encode()).hexdigest())
            self._save(db, body)
        self._notify(conversation_id)

    def pending_conversations(self):
        with self.repository.database_read() as db:
            return [row[0] for row in db.execute("SELECT conversation_id FROM agent_conversations WHERE status NOT IN ('COMPLETED','FAILED','INTERRUPTED') ORDER BY rowid")]

    def pending_messages(self, conversation_id):
        return [message for message in self.get_conversation(conversation_id).messages if message.status in {"QUEUED", "PROCESSING"}]

    def set_message_status(self, conversation_id, message_id, status, notice=None):
        if status not in {"PROCESSING", "APPLIED", "UNUSED"}:
            raise ValueError("invalid message transition")
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND message_id=?", (conversation_id, message_id)).fetchone()
            if not row:
                raise AgentStoreError("AGENT_MESSAGE_NOT_FOUND", "消息不存在。", 404)
            message = AgentMessage.model_validate_json(row[0])
            if message.status == status:
                return
            if message.status in {"APPLIED", "UNUSED"}:
                raise AgentStoreError("AGENT_MESSAGE_FINALIZED", "已处理消息的状态不能更改。", 409)
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
            dispatch = db.execute("SELECT epoch,payload FROM agent_dispatches WHERE conversation_id=? AND dispatch_id=?",
                                  (conversation_id, dispatch_id)).fetchone()
            if dispatch is None or dispatch[0] != self.runtime_epoch:
                raise AgentStoreError("AGENT_DISPATCH_NOT_FOUND", "消息尚未关联当前运行中的有效命令。", 409)
            payload = json.loads(dispatch[1])
            if payload.get("operation") not in {"CreateCase", "SubmitSupplement"} or not payload.get("command", {}).get("idempotency_key"):
                raise AgentStoreError("AGENT_ADOPTION_INVALID", "该命令不能采纳用户消息。", 409)
            message_row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND message_id=?",
                                     (conversation_id, message_id)).fetchone()
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
            message_row = db.execute("SELECT body FROM agent_messages WHERE conversation_id=? AND message_id=?", (conversation_id, message_id)).fetchone()
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
        rows = list(db.execute("SELECT a.message_id,d.payload,m.body FROM agent_message_adoptions a "
            "JOIN agent_dispatches d ON d.dispatch_id=a.dispatch_id "
            "JOIN agent_messages m ON m.message_id=a.message_id "
            "WHERE a.conversation_id=?", (body["conversation_id"],)))
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
            current = db.execute("SELECT create_request_id FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            if current is not None and current != create_request_id:
                raise AgentStoreError("AGENT_CASE_ALREADY_BOUND", "会话已经关联另一项任务。", 409)
            db.execute("UPDATE agent_conversations SET create_request_id=? WHERE conversation_id=?", (create_request_id, conversation_id))

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
            existing = db.execute("SELECT conversation_id,epoch,status,payload,result FROM agent_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if existing:
                if existing[0] != conversation_id or existing[3] != serialized:
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "派发标识已经用于不同命令。", 409)
                if existing[1] != self.runtime_epoch and existing[2] != "COMPLETED":
                    raise AgentStoreError("AGENT_DISPATCH_INTERRUPTED", "命令已经随服务重启中断，请另建任务。", 409)
                return {"dispatch_id": dispatch_id, "epoch": existing[1], "status": existing[2],
                        "payload": json.loads(existing[3]), "result": json.loads(existing[4]) if existing[4] else None}
            self._ensure_open(body)
            db.execute("INSERT INTO agent_dispatches VALUES (?,?,?,'PENDING',?,NULL)",
                       (dispatch_id, conversation_id, self.runtime_epoch, serialized))
            return {"dispatch_id": dispatch_id, "epoch": self.runtime_epoch, "status": "PENDING", "payload": payload, "result": None}

    def get_dispatch(self, conversation_id, dispatch_id):
        with self.repository.database_read() as db:
            self._load(db, conversation_id)
            row = db.execute("SELECT epoch,status,payload,result FROM agent_dispatches WHERE conversation_id=? AND dispatch_id=?",
                             (conversation_id, dispatch_id)).fetchone()
            return None if row is None else {"dispatch_id": dispatch_id, "epoch": row[0],
                "status": row[1], "payload": json.loads(row[2]), "result": json.loads(row[3]) if row[3] else None}

    def pending_dispatches(self, conversation_id):
        with self.repository.database_read() as db:
            self._load(db, conversation_id)
            return [{"dispatch_id": row[0], "epoch": self.runtime_epoch, "status": "PENDING",
                "payload": json.loads(row[1]), "result": json.loads(row[2]) if row[2] else None}
                for row in db.execute("SELECT dispatch_id,payload,result FROM agent_dispatches WHERE conversation_id=? AND epoch=? AND status='PENDING' ORDER BY rowid",
                    (conversation_id, self.runtime_epoch))]

    def complete_dispatch(self, dispatch_id, result=None, status="COMPLETED"):
        if status not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
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
            row = db.execute("SELECT conversation_id FROM agent_conversations WHERE case_id=?", (case_id,)).fetchone()
            if not row:
                return None
            body = self._load(db, row[0])
            if body["status"] in _CLOSED or body.get("report_available"):
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

    def fail_conversation(self, conversation_id, code="AGENT_EXECUTION_FAILED", *, interrupted=False):
        # Internal exception text is deliberately not an argument.
        with self.repository.database_transaction() as db:
            body = self._load(db, conversation_id)
            if body["status"] in _CLOSED or body.get("report_available"):
                return
            self._close(db, body, "INTERRUPTED" if interrupted else "FAILED", code)
            self._save(db, body)
        self._notify(conversation_id)

    def _close(self, db, body, status, code=None):
        body["status"] = status
        self._mark_unused(db, body)
        if status == "INTERRUPTED":
            self._append(db, body, "conversation.interrupted", {"code": "AGENT_INTERRUPTED", "message": "服务已重启，本次任务已中断，请重新发起。"}, "interrupted")
        elif status == "FAILED":
            self._append(db, body, "agent.failed", {"code": "AGENT_EXECUTION_FAILED", "message": _SAFE_FAILURE}, "failed")
        self._append(db, body, "conversation.completed", {"status": status}, "completed")

    def _mark_unused(self, db, body):
        for row in list(db.execute("SELECT message_id,body FROM agent_messages WHERE conversation_id=?", (body["conversation_id"],))):
            message = json.loads(row[1])
            if message["status"] in {"QUEUED", "PROCESSING"}:
                self._finalize_message(db, body, message, "UNUSED")
            db.execute("DELETE FROM agent_message_adoptions WHERE message_id=?", (row[0],))

    def _project_state(self, db, state):
        for case_id, aggregate in state.cases.items():
            row = db.execute("SELECT conversation_id FROM agent_conversations WHERE case_id=?", (case_id,)).fetchone()
            if not row:
                for record in state.idempotency_records.values():
                    if str(record.operation) == "CreateCase" and record.case_id == case_id:
                        row = db.execute("SELECT conversation_id FROM agent_conversations WHERE create_request_id=?", (record.idempotency_key,)).fetchone()
                        if row:
                            break
            if row:
                body = self._load(db, row[0])
                body["case_id"] = case_id
                self._accept_committed_adoptions(db, body, state)
                self._project_case(db, body, aggregate)

    def _project_case(self, db, body, aggregate):
        case = aggregate.case
        if body["status"] in _CLOSED:
            return
        body.update(case_id=case.case_id, job_id=case.active_job_id, case_status=case.status.value,
                    archive_status=case.archive_status)
        self._append(db, body, "case.updated", {"status": case.status.value, "case_revision": case.case_revision},
                     "case:" + str(case.case_revision))
        if case.status.value in {"WAITING_INPUT", "WAITING_ATTACHMENT"}:
            body["status"] = "WAITING_INPUT"
            body["current_questions"] = [requirement.prompt for requirement in case.diagnosis_state.pending_requirements if requirement.status.value == "OPEN"]
            if body["current_questions"]:
                self._append(db, body, "assistant.question", {"questions": body["current_questions"]}, "case-questions:" + str(case.case_revision))
        elif case.status.value in _RESULT:
            summaries = project_artifact_summaries(case, aggregate.artifacts.values(), include_internal=False)
            reports = [item for item in summaries if item.kind.value in {"USER_RESULT", "GENERIC_REPORT"}]
            generic = case.generic_result_v2 or case.generic_result
            if reports or generic is not None:
                body["report_available"] = True
                body["current_questions"] = []
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
            self._close(db, body, "INTERRUPTED" if case.status.value == "INTERRUPTED" else "FAILED")
        else:
            body["status"] = "RUNNING"
            body["current_questions"] = []
        self._save(db, body)

    def _case_committed(self, case_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT conversation_id FROM agent_conversations WHERE case_id=?", (case_id,)).fetchone()
        if row:
            self._notify(row[0])

    def recover(self):
        """Recover public history without restarting any model or active Case."""
        changed = []
        with self.repository.database_transaction() as db:
            for conversation_id, epoch, case_id in list(db.execute("SELECT conversation_id,epoch,case_id FROM agent_conversations WHERE status NOT IN ('COMPLETED','FAILED','INTERRUPTED')")):
                if epoch == self.runtime_epoch:
                    continue
                body = self._load(db, conversation_id)
                persisted = db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (case_id,)).fetchone() if case_id else None
                if persisted:
                    state = StateFile.model_validate_json(persisted[0])
                    self._accept_committed_adoptions(db, body, state)
                    self._project_case(db, body, state.cases[case_id])
                elif db.execute("SELECT 1 FROM agent_messages WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone():
                    self._close(db, body, "INTERRUPTED")
                    self._save(db, body)
                db.execute("UPDATE agent_conversations SET epoch=? WHERE conversation_id=?", (self.runtime_epoch, conversation_id))
                db.execute("UPDATE agent_dispatches SET status='INTERRUPTED' WHERE conversation_id=? AND status='PENDING' AND epoch<>?", (conversation_id, self.runtime_epoch))
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
            self._ensure_open(body)
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
            return AttachmentRecord(**json.loads(row[0]), storage_path=row[1])

    def set_attachment_status(self, attachment_id, status, storage_path=None, *, case_attachment_id=None):
        with self.repository.database_transaction() as db:
            row = db.execute("SELECT body,storage_path FROM agent_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            if not row:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_FOUND", "附件不存在。", 404)
            attachment = AgentAttachment.model_validate_json(row[0])
            body = self._load(db, attachment.conversation_id)
            if attachment.status == status and (case_attachment_id is None or attachment.case_attachment_id == case_attachment_id):
                return AttachmentRecord(**attachment.model_dump(), storage_path=row[1])
            self._ensure_open(body)
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

    def bind_attachment(self, attachment_id, case_attachment_id):
        return self.set_attachment_status(attachment_id, "IMPORTED", case_attachment_id=case_attachment_id)
