"""Transactional feedback, one-shot extraction tasks, and bounded experience cards."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from problem_locator.agent.models import AgentStoreError

from .models import FeedbackRequest, FeedbackSource, FeedbackView

MAX_CARD_BYTES = 4096
MAX_SOURCES = 10_000
MAX_TASKS = 10_000
MAX_REQUESTS_PER_REPORT = 128
SOURCE_RETENTION_DAYS = 7
CARD_RETENTION_DAYS = 90

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory_feedback (run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,owner_key TEXT NOT NULL,source_job_id TEXT NOT NULL,skill_name TEXT NOT NULL,report_sha256 TEXT NOT NULL,rating TEXT NOT NULL CHECK(rating IN ('LIKE','DISLIKE')),updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_feedback_requests (owner_key TEXT NOT NULL,request_id TEXT NOT NULL,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,rating TEXT NOT NULL,PRIMARY KEY(owner_key,request_id))",
    "CREATE INDEX IF NOT EXISTS memory_requests_run ON memory_feedback_requests(run_id)",
    "CREATE INDEX IF NOT EXISTS memory_feedback_conversation ON memory_feedback(conversation_id)",
    "CREATE TABLE IF NOT EXISTS memory_tasks (task_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,source_job_id TEXT NOT NULL,skill_name TEXT NOT NULL,report_sha256 TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','READY','FAILED','DELETED')),problem_text TEXT,report_markdown TEXT,card_json TEXT,card_sha256 TEXT,active INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,UNIQUE(run_id,report_sha256))",
    "CREATE INDEX IF NOT EXISTS memory_tasks_pending ON memory_tasks(status,created_at,task_id)",
    "CREATE INDEX IF NOT EXISTS memory_cards_active ON memory_tasks(skill_name,active,completed_at)",
    "CREATE INDEX IF NOT EXISTS memory_tasks_conversation ON memory_tasks(conversation_id)",
)


def _one(db, query, parameters=()):
    cursor = db.execute(query, parameters)
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _before(now, days):
    return _timestamp(datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=days))


def _limit():
    return AgentStoreError("AGENT_FEEDBACK_LIMIT_EXCEEDED", "反馈记录已达到上限，请稍后重试。", 429)


class MemoryStore:
    def __init__(self, repository, clock=None):
        self.repository = repository
        self.clock = clock
        with repository.database_transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def _now(self):
        value = self.clock.now() if self.clock is not None else datetime.now(timezone.utc)
        return _timestamp(value)

    @staticmethod
    def _scope(db, conversation_id, run_id, owner_key):
        owner = db.execute("SELECT owner_key,deleted_at FROM agent_conversations WHERE conversation_id=?",
                           (conversation_id,)).fetchone()
        if owner_key is None or owner is None or owner[1] is not None or owner[0] != owner_key:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        run = db.execute("SELECT case_id FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?",
                         (conversation_id, run_id)).fetchone()
        if run is None:
            raise AgentStoreError("AGENT_RUN_NOT_FOUND", "本次诊断不存在。", 404)
        return run[0]

    @staticmethod
    def _view(db, conversation_id, run_id, can_rate):
        row = db.execute("SELECT rating,updated_at FROM memory_feedback WHERE conversation_id=? AND run_id=?",
                         (conversation_id, run_id)).fetchone()
        return FeedbackView(conversation_id=conversation_id, run_id=run_id, can_rate=can_rate,
                            rating=None if row is None else row[0], updated_at=None if row is None else row[1])

    def get_feedback(self, conversation_id, run_id, *, owner_key, can_rate):
        with self.repository.database_read() as db:
            self._scope(db, conversation_id, run_id, owner_key)
            return self._view(db, conversation_id, run_id, can_rate)

    def put_feedback(self, conversation_id, run_id, *, owner_key, request_id, rating,
                     source: FeedbackSource | None):
        request = FeedbackRequest(request_id=request_id, rating=rating)
        now = self._now()
        with self.repository.database_transaction() as db:
            case_id = self._scope(db, conversation_id, run_id, owner_key)
            if source is None:
                raise AgentStoreError("AGENT_FEEDBACK_UNSUPPORTED", "这份报告暂不支持反馈。", 409)
            if (case_id != source.case_id or not source.problem_text or not source.report_markdown
                    or len(source.problem_text.encode("utf-8")) > 65_536
                    or len(source.report_markdown.encode("utf-8")) > 65_536
                    or hashlib.sha256(source.report_markdown.encode("utf-8")).hexdigest() != source.report_sha256):
                raise AgentStoreError("AGENT_FEEDBACK_UNSUPPORTED", "报告来源已变化，请刷新后重试。", 409)
            previous = db.execute("SELECT conversation_id,run_id,rating FROM memory_feedback_requests WHERE owner_key=? AND request_id=?",
                                  (owner_key, request.request_id)).fetchone()
            if previous is not None:
                if previous != (conversation_id, run_id, request.rating):
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的目标和反馈不能更改。", 409)
                # Return today's state: a late replay must never restore an old vote.
                return self._view(db, conversation_id, run_id, True)
            self._prune(db, now)
            current = _one(db, "SELECT * FROM memory_feedback WHERE run_id=?", (run_id,))
            if current is not None and (current["report_sha256"] != source.report_sha256
                                       or current["source_job_id"] != source.source_job_id
                                       or current["owner_key"] != owner_key):
                raise AgentStoreError("AGENT_FEEDBACK_UNSUPPORTED", "报告来源已变化，请刷新后重试。", 409)
            if db.execute("SELECT count(*) FROM memory_feedback_requests WHERE run_id=?", (run_id,)).fetchone()[0] >= MAX_REQUESTS_PER_REPORT:
                raise _limit()
            if current is None:
                if db.execute("SELECT count(*) FROM memory_feedback").fetchone()[0] >= MAX_SOURCES:
                    raise _limit()
                db.execute("INSERT INTO memory_feedback VALUES (?,?,?,?,?,?,?,?)",
                           (run_id, conversation_id, owner_key, source.source_job_id, source.skill_name,
                            source.report_sha256, request.rating, now))
            elif current["rating"] != request.rating:
                db.execute("UPDATE memory_feedback SET rating=?,updated_at=? WHERE run_id=?",
                           (request.rating, now, run_id))
            db.execute("INSERT INTO memory_feedback_requests VALUES (?,?,?,?,?)",
                       (owner_key, request.request_id, conversation_id, run_id, request.rating))
            task = _one(db, "SELECT * FROM memory_tasks WHERE run_id=? AND report_sha256=?",
                        (run_id, source.report_sha256))
            if request.rating == "LIKE" and task is None:
                if db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] >= MAX_TASKS:
                    raise _limit()
                db.execute("INSERT INTO memory_tasks (task_id,conversation_id,run_id,source_job_id,skill_name,report_sha256,status,problem_text,report_markdown,created_at,updated_at) VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?)",
                           (str(uuid.uuid4()), conversation_id, run_id, source.source_job_id,
                            source.skill_name, source.report_sha256, source.problem_text,
                            source.report_markdown, now, now))
            elif task is not None:
                active = (request.rating == "LIKE" and task["status"] == "READY"
                          and task["card_json"] is not None
                          and task["completed_at"] > _before(now, CARD_RETENTION_DAYS))
                db.execute("UPDATE memory_tasks SET active=? WHERE task_id=?", (int(active), task["task_id"]))
            return self._view(db, conversation_id, run_id, True)

    def claim_task(self):
        now = self._now()
        with self.repository.database_transaction() as db:
            row = _one(db, "SELECT t.* FROM memory_tasks t JOIN agent_conversations c ON c.conversation_id=t.conversation_id JOIN agent_conversation_runs r ON r.run_id=t.run_id AND r.conversation_id=t.conversation_id WHERE t.status='PENDING' AND t.created_at>? AND c.deleted_at IS NULL ORDER BY t.created_at,t.task_id LIMIT 1",
                       (_before(now, SOURCE_RETENTION_DAYS),))
            if row is None:
                return None
            db.execute("UPDATE memory_tasks SET status='RUNNING',updated_at=? WHERE task_id=? AND status='PENDING'",
                       (now, row["task_id"]))
            return {key: row[key] for key in ("task_id", "conversation_id", "run_id", "skill_name",
                                              "problem_text", "report_markdown", "report_sha256")}

    def finish_task(self, task_id, card_json):
        from .extraction import parse_card_json
        content = parse_card_json(card_json)
        now = self._now()
        with self.repository.database_transaction() as db:
            task = _one(db, "SELECT * FROM memory_tasks WHERE task_id=? AND status='RUNNING'", (task_id,))
            if task is None:
                return False
            content = parse_card_json(content, sources=(task["problem_text"] or "", task["report_markdown"] or ""))
            rating = db.execute("SELECT f.rating FROM memory_feedback f JOIN agent_conversations c ON c.conversation_id=f.conversation_id JOIN agent_conversation_runs r ON r.run_id=f.run_id WHERE f.run_id=? AND c.deleted_at IS NULL",
                                (task["run_id"],)).fetchone()
            if rating is None or task["created_at"] <= _before(now, SOURCE_RETENTION_DAYS):
                db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,updated_at=? WHERE task_id=?",
                           (now, task_id))
                return False
            db.execute("UPDATE memory_tasks SET status='READY',problem_text=NULL,report_markdown=NULL,card_json=?,card_sha256=?,active=?,completed_at=?,updated_at=? WHERE task_id=?",
                       (content, hashlib.sha256(content.encode("utf-8")).hexdigest(), int(rating[0] == "LIKE"), now, now, task_id))
            return True

    def fail_task(self, task_id):
        with self.repository.database_transaction() as db:
            result = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,updated_at=? WHERE task_id=? AND status IN ('PENDING','RUNNING')",
                                (self._now(), task_id))
            return result.rowcount == 1

    def recover(self):
        with self.repository.database_transaction() as db:
            result = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,updated_at=? WHERE status='RUNNING'",
                                (self._now(),))
            return result.rowcount

    def active_cards(self, skill_name):
        with self.repository.database_read() as db:
            rows = db.execute("SELECT task_id,card_json,card_sha256,updated_at,skill_name FROM memory_tasks WHERE skill_name=? AND active=1 AND status='READY' AND card_json IS NOT NULL AND completed_at>? ORDER BY completed_at DESC,task_id LIMIT ?",
                              (skill_name, _before(self._now(), CARD_RETENTION_DAYS), MAX_TASKS)).fetchall()
            return [dict(zip(("card_id", "card_json", "card_sha256", "updated_at", "skill_name"), row)) for row in rows]

    def revoke_conversation(self, db, conversation_id):
        # Called inside the existing deletion transaction, before cleanup can run.
        db.execute("UPDATE memory_tasks SET status='DELETED',active=0,problem_text=NULL,report_markdown=NULL,card_json=NULL,card_sha256=NULL,updated_at=? WHERE conversation_id=?",
                   (self._now(), conversation_id))

    def expire_sources(self, db, conversation_id, *, run_id=None):
        # Natural source expiry retains completed cards with their original
        # lifetime and vote. Fence unfinished work in the cleanup transaction.
        scope = "conversation_id=?" + (" AND run_id=?" if run_id is not None else "")
        parameters = (conversation_id,) if run_id is None else (conversation_id, run_id)
        db.execute("UPDATE memory_tasks SET status='DELETED',active=0,problem_text=NULL,report_markdown=NULL,card_json=NULL,card_sha256=NULL,updated_at=? WHERE status<>'READY' AND " + scope,
                   (self._now(), *parameters))

    def _prune(self, db, now):
        raw = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,updated_at=? WHERE status IN ('PENDING','RUNNING') AND created_at<=?",
                         (now, _before(now, SOURCE_RETENTION_DAYS))).rowcount
        cards = db.execute("UPDATE memory_tasks SET active=0,card_json=NULL,card_sha256=NULL WHERE card_json IS NOT NULL AND completed_at<=?",
                           (_before(now, CARD_RETENTION_DAYS),)).rowcount
        # Natural Case retention removes source runs. Completed cards have their
        # own lifetime; failed/deleted tasks and feedback receipts can go now.
        orphan = "NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_tasks.run_id)"
        db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,updated_at=? WHERE status IN ('PENDING','RUNNING') AND " + orphan, (now,))
        tasks = db.execute("DELETE FROM memory_tasks WHERE " + orphan + " AND (status IN ('FAILED','DELETED') OR (status='READY' AND card_json IS NULL))").rowcount
        requests = db.execute("DELETE FROM memory_feedback_requests WHERE NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_feedback_requests.run_id)").rowcount
        feedback = db.execute("DELETE FROM memory_feedback WHERE NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_feedback.run_id)").rowcount
        return {"expired_sources": raw, "expired_cards": cards, "tasks": tasks, "requests": requests, "feedback": feedback}

    def prune(self):
        with self.repository.database_transaction() as db:
            return self._prune(db, self._now())
