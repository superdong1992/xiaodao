"""Immutable, owner-scoped negative feedback separate from chat and mutable votes."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace

from problem_locator.agent.models import AgentStoreError
from problem_locator.diagnostics import log_event
from problem_locator.storage.database import dialect_for, lock_key, table_columns, table_names

from .models import FeedbackCommentRequest, FeedbackCommentReceipt

COMMENT_TABLE = "memory_feedback_comments"
COMMENT_VERSION = "1"
COMMENT_VERSION_KEY = "feedback_comment_storage_version"
MAX_COMMENTS_PER_ANSWER = 32
MAX_LIVE_COMMENTS = 10_000
_COLUMNS = {"comment_id", "owner_key", "request_id", "conversation_id", "run_id", "answer_key",
    "source_job_id", "target_sha256", "payload_sha256", "ordinal", "text", "analysis_text",
    "analysis_reference", "created_at"}
_SCHEMA = """CREATE TABLE memory_feedback_comments (
comment_id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, request_id TEXT NOT NULL,
conversation_id TEXT NOT NULL, run_id TEXT NOT NULL, answer_key TEXT NOT NULL,
source_job_id TEXT NOT NULL, target_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
ordinal INTEGER NOT NULL, text TEXT, analysis_text TEXT, analysis_reference TEXT,
created_at TEXT NOT NULL, UNIQUE(owner_key,request_id), UNIQUE(run_id,answer_key,ordinal))"""


def initialize_comments(db):
    marker = db.execute("SELECT value FROM metadata WHERE key=?", (COMMENT_VERSION_KEY,)).fetchone()
    exists = COMMENT_TABLE in table_names(db)
    if ((marker is not None and marker[0] != COMMENT_VERSION) or (marker is not None) != exists
            or (exists and table_columns(db, COMMENT_TABLE) != _COLUMNS)):
        raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "文字反馈数据版本与表结构不一致，请保留原目录。", 503)
    if not exists:
        db.execute(_SCHEMA)
        db.execute("INSERT INTO metadata(key,value) VALUES (?,?)", (COMMENT_VERSION_KEY, COMMENT_VERSION))
    db.execute("CREATE INDEX IF NOT EXISTS memory_comments_conversation ON memory_feedback_comments(conversation_id)")


def _fingerprint(request):
    raw = json.dumps(request.model_dump(exclude={"request_id"}), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _receipt(row):
    return FeedbackCommentReceipt(conversation_id=row["conversation_id"], run_id=row["run_id"],
        followup_id=row["answer_key"] or None, comment_id=row["comment_id"],
        request_id=row["request_id"], created_at=row["created_at"])


def comment_context(source, request, comment_id):
    prior = json.loads(source.context_json) if source.context_json else {}
    return json.dumps({"schema_version": 1, "origin": "FEEDBACK_COMMENT",
        "original_problem": source.problem_text,
        "original_report": prior.get("original_report", source.report_markdown),
        "prior_turns": prior.get("prior_turns", []),
        "target_question": prior.get("question"),
        "target_answer_markdown": source.report_markdown, "target_answer_sha256": source.report_sha256,
        "correction": {"intent": "CORRECTION", "target_followup_id": source.answer_key or None},
        "feedback": {"comment_id": comment_id, "text": request.text,
            "analysis_text": request.analysis_text, "analysis_reference": request.analysis_reference}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FeedbackCommentStoreMixin:
    def post_feedback_comment(self, conversation_id, run_id, *, owner_key, request_id,
                              text=None, analysis_text=None, analysis_reference=None,
                              source=None, answer_key="", extract_memory=True):
        from .store import _one, _limit, _unsupported
        request = FeedbackCommentRequest(request_id=request_id, text=text,
            analysis_text=analysis_text, analysis_reference=analysis_reference)
        fingerprint, now = _fingerprint(request), self._now()
        with self.repository.database_transaction() as db:
            case_id = self._scope(db, conversation_id, run_id, owner_key)
            lock_key(db, "memory", "admission")
            previous = _one(db, "SELECT * FROM memory_feedback_comments WHERE owner_key=? AND request_id=?",
                (owner_key, request.request_id))
            if previous is not None:
                if (previous["conversation_id"], previous["run_id"], previous["answer_key"], previous["payload_sha256"]) != (
                        conversation_id, run_id, answer_key, fingerprint):
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的反馈对象和文字资料不能更改。", 409)
                return _receipt(previous)
            self._validate_source(source, case_id, answer_key)
            current = _one(db, "SELECT * FROM memory_feedback WHERE run_id=? AND answer_key=?", (run_id, answer_key))
            if current is None or current["rating"] != "DISLIKE":
                raise AgentStoreError("AGENT_FEEDBACK_REQUIRES_DISLIKE", "请先对这条回答点踩，再提交文字反馈。", 409)
            if current["report_sha256"] != source.report_sha256 or current["source_job_id"] != source.source_job_id:
                raise _unsupported()
            self._prune(db, now)
            ordinal = db.execute("SELECT coalesce(max(ordinal),0)+1 FROM memory_feedback_comments WHERE run_id=? AND answer_key=?",
                (run_id, answer_key)).fetchone()[0]
            if ordinal > MAX_COMMENTS_PER_ANSWER or db.execute(
                    "SELECT count(*) FROM memory_feedback_comments WHERE text IS NOT NULL OR analysis_text IS NOT NULL").fetchone()[0] >= MAX_LIVE_COMMENTS:
                raise _limit()
            comment_id = str(uuid.uuid4())
            db.execute("INSERT INTO memory_feedback_comments(comment_id,owner_key,request_id,conversation_id,run_id,answer_key,source_job_id,target_sha256,payload_sha256,ordinal,text,analysis_text,analysis_reference,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (comment_id, owner_key, request.request_id, conversation_id, run_id, answer_key,
                 source.source_job_id, source.report_sha256, fingerprint, ordinal, request.text,
                 request.analysis_text, request.analysis_reference, now))
            if extract_memory and source.problem_text.strip() and source.skill_name.strip():
                # Feedback is durable even when optional learning cannot be queued.
                db.execute("SAVEPOINT feedback_comment_memory")
                try:
                    correction = replace(source, answer_key="feedback:" + comment_id, source_kind="CORRECTION",
                        replaces_answer_key=answer_key, context_json=comment_context(source, request, comment_id))
                    self._enqueue(db, conversation_id, run_id, correction, now)
                    db.execute("RELEASE SAVEPOINT feedback_comment_memory")
                except Exception as exc:
                    db.execute("ROLLBACK TO SAVEPOINT feedback_comment_memory")
                    db.execute("RELEASE SAVEPOINT feedback_comment_memory")
                    log_event("memory.comment.enqueue_failed", exception_type=type(exc).__name__)
            return FeedbackCommentReceipt(conversation_id=conversation_id, run_id=run_id,
                followup_id=answer_key or None, comment_id=comment_id, request_id=request.request_id, created_at=now)

    @staticmethod
    def _comment_can_finish(db, task):
        from .store import _one
        if not task["answer_key"].startswith("feedback:"):
            return True
        row = _one(db, "SELECT * FROM memory_feedback_comments WHERE comment_id=?", (task["answer_key"][9:],))
        if row is None or (row["conversation_id"], row["run_id"], row["answer_key"], row["target_sha256"]) != (
                task["conversation_id"], task["run_id"], task["replaces_answer_key"], task["report_sha256"]):
            return False
        # A late older review cannot take precedence over newer submitted facts.
        return db.execute("SELECT 1 FROM memory_feedback_comments WHERE run_id=? AND answer_key=? AND ordinal>? LIMIT 1",
            (row["run_id"], row["answer_key"], row["ordinal"])).fetchone() is None

    @staticmethod
    def _supersede_prior_comments(db, task):
        if task["answer_key"].startswith("feedback:"):
            db.execute("UPDATE memory_tasks SET active=0,superseded=1 WHERE run_id=? AND replaces_answer_key=? "
                "AND answer_key LIKE 'feedback:%' AND task_id<>?",
                (task["run_id"], task["replaces_answer_key"], task["task_id"]))

    @staticmethod
    def _prune_comments(db, now):
        from .store import _before, SOURCE_RETENTION_DAYS
        d = dialect_for(db)
        db.execute("UPDATE memory_feedback_comments SET text=NULL,analysis_text=NULL,analysis_reference=NULL "
            f"WHERE {d.timestamp('created_at')}<={d.timestamp('?')}", (_before(now, SOURCE_RETENTION_DAYS),))
        db.execute("DELETE FROM memory_feedback_comments WHERE NOT EXISTS "
            "(SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_feedback_comments.run_id)")
