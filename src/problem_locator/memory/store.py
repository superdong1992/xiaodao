"""Transactional answer feedback, one-shot learning and bounded experience cards."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from problem_locator.agent.models import AgentStoreError
from problem_locator.storage.database import (dialect_for, lock_conversation, lock_key,
    table_columns, table_names, try_lock_conversation)
from problem_locator.timestamps import format_timestamp, now_timestamp, parse_timestamp

from .comments import FeedbackCommentStoreMixin, initialize_comments
from .models import FeedbackRequest, FeedbackSource, FeedbackView, FollowupFeedbackView

MAX_CARD_BYTES = 4096
MAX_SOURCES = 10_000
MAX_TASKS = 10_000
MAX_REQUESTS_PER_REPORT = 128
SOURCE_RETENTION_DAYS = 7
CARD_RETENTION_DAYS = 90
STORAGE_VERSION = "2"
_TABLES = ("memory_feedback", "memory_feedback_requests", "memory_tasks")
_SCHEMA = (
    "CREATE TABLE memory_feedback (run_id TEXT NOT NULL,answer_key TEXT NOT NULL DEFAULT '',conversation_id TEXT NOT NULL,owner_key TEXT NOT NULL,source_job_id TEXT NOT NULL,skill_name TEXT NOT NULL,report_sha256 TEXT NOT NULL,rating TEXT NOT NULL CHECK(rating IN ('LIKE','DISLIKE')),updated_at TEXT NOT NULL,extraction_attempted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(run_id,answer_key))",
    "CREATE TABLE memory_feedback_requests (owner_key TEXT NOT NULL,request_id TEXT NOT NULL,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,answer_key TEXT NOT NULL DEFAULT '',rating TEXT NOT NULL,PRIMARY KEY(owner_key,request_id))",
    "CREATE TABLE memory_tasks (task_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,run_id TEXT NOT NULL,answer_key TEXT NOT NULL DEFAULT '',source_kind TEXT NOT NULL DEFAULT 'REPORT' CHECK(source_kind IN ('REPORT','FOLLOWUP','CORRECTION')),source_job_id TEXT NOT NULL,skill_name TEXT NOT NULL,report_sha256 TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','READY','FAILED','DELETED')),problem_text TEXT,report_markdown TEXT,context_json TEXT,replaces_answer_key TEXT,evidence_json TEXT,superseded INTEGER NOT NULL DEFAULT 0,card_json TEXT,card_sha256 TEXT,active INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,UNIQUE(run_id,answer_key,report_sha256))",
)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS memory_requests_run ON memory_feedback_requests(run_id,answer_key)",
    "CREATE INDEX IF NOT EXISTS memory_feedback_conversation ON memory_feedback(conversation_id)",
    "CREATE INDEX IF NOT EXISTS memory_tasks_pending ON memory_tasks(status,created_at,task_id)",
    "CREATE INDEX IF NOT EXISTS memory_cards_active ON memory_tasks(skill_name,active,completed_at)",
    "CREATE INDEX IF NOT EXISTS memory_tasks_conversation ON memory_tasks(conversation_id)",
    "CREATE INDEX IF NOT EXISTS memory_replacements ON memory_tasks(run_id,replaces_answer_key)",
)
# Defaults are also used by the offline SQLite -> PostgreSQL importer. A legacy
# report stays the empty answer target; its request identity never changes.
LEGACY_DEFAULTS = {
    "memory_feedback": {"answer_key": ""},
    "memory_feedback_requests": {"answer_key": ""},
    "memory_tasks": {"answer_key": "", "source_kind": "REPORT", "context_json": None,
        "replaces_answer_key": None, "evidence_json": None, "superseded": 0},
}


def _one(db, query, parameters=()):
    cursor = db.execute(query, parameters)
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def _timestamp(value):
    return format_timestamp(parse_timestamp(str(value)))


def _before(now, days):
    return format_timestamp(parse_timestamp(now) - timedelta(days=days))


def _limit():
    return AgentStoreError("AGENT_FEEDBACK_LIMIT_EXCEEDED", "反馈记录已达到上限，请稍后重试。", 429)


def _unsupported():
    return AgentStoreError("AGENT_FEEDBACK_UNSUPPORTED", "回答来源已变化，请刷新后重试。", 409)


class MemoryStore(FeedbackCommentStoreMixin):
    def __init__(self, repository, clock=None):
        self.repository, self.clock = repository, clock
        with repository.database_transaction() as db:
            lock_key(db, "schema", "answer-memory")
            existing = table_names(db).intersection(_TABLES)
            backfill_attempted = bool(existing) and "extraction_attempted" not in table_columns(db, "memory_feedback")
            marker = db.execute("SELECT value FROM metadata WHERE key='memory_storage_version'").fetchone()
            if existing and existing != set(_TABLES):
                raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "反馈数据表不完整，请保留原目录。", 503)
            if marker is not None and marker[0] != STORAGE_VERSION:
                raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "反馈数据版本不受支持，请保留原目录。", 503)
            if marker is not None and not existing:
                raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "反馈数据表缺失，请保留原目录。", 503)
            if not existing:
                for statement in _SCHEMA:
                    db.execute(statement)
            elif "answer_key" not in table_columns(db, "memory_feedback"):
                if marker is not None or any(set(defaults).intersection(table_columns(db, table))
                        for table, defaults in LEGACY_DEFAULTS.items()):
                    raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "反馈数据版本与表结构不一致。", 503)
                self._migrate(db)
            elif any(not set(defaults) <= table_columns(db, table)
                     for table, defaults in LEGACY_DEFAULTS.items()):
                raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "反馈数据表结构不受支持，请保留原目录。", 503)
            initialize_comments(db)
            for statement in _INDEXES:
                db.execute(statement)
            if "extraction_attempted" not in table_columns(db, "memory_feedback"):
                db.execute("ALTER TABLE memory_feedback ADD COLUMN extraction_attempted INTEGER NOT NULL DEFAULT 0")
            if backfill_attempted:
                # Votes outlive extraction tasks. Preserve the receipt per
                # answer when either legacy schema is upgraded.
                db.execute("UPDATE memory_feedback SET extraction_attempted=1 WHERE EXISTS "
                    "(SELECT 1 FROM memory_tasks t WHERE t.run_id=memory_feedback.run_id "
                    "AND t.answer_key=memory_feedback.answer_key AND t.report_sha256=memory_feedback.report_sha256)")
            db.execute("INSERT INTO metadata(key,value) VALUES ('memory_storage_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (STORAGE_VERSION,))

    @staticmethod
    def _migrate(db):
        # Rebuild transactionally on both engines: the old unique keys must be
        # replaced, not merely supplemented with columns.
        for table, statement in zip(_TABLES, _SCHEMA, strict=True):
            columns = sorted(table_columns(db, table))
            db.execute(statement.replace("CREATE TABLE " + table + " ", "CREATE TABLE " + table + "_v2 ", 1))
            names = ",".join(columns)
            db.execute(f"INSERT INTO {table}_v2 ({names}) SELECT {names} FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")

    def _now(self):
        return _timestamp(self.clock.now()) if self.clock is not None else now_timestamp()

    @staticmethod
    def _scope(db, conversation_id, run_id, owner_key):
        lock_conversation(db, conversation_id)
        owner = db.execute("SELECT owner_key,deleted_at FROM agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if owner_key is None or owner is None or owner[1] is not None or owner[0] != owner_key:
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        run = db.execute("SELECT case_id FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (conversation_id, run_id)).fetchone()
        if run is None:
            raise AgentStoreError("AGENT_RUN_NOT_FOUND", "本次诊断不存在。", 404)
        return run[0]

    @staticmethod
    def _view(db, conversation_id, run_id, can_rate, answer_key=""):
        row = db.execute("SELECT rating,updated_at FROM memory_feedback WHERE conversation_id=? AND run_id=? AND answer_key=?", (conversation_id, run_id, answer_key)).fetchone()
        values = dict(conversation_id=conversation_id, run_id=run_id, can_rate=can_rate,
            rating=None if row is None else row[0], updated_at=None if row is None else _timestamp(row[1]))
        return FollowupFeedbackView(**values, followup_id=answer_key) if answer_key else FeedbackView(**values)

    def get_feedback(self, conversation_id, run_id, *, owner_key, can_rate, answer_key=""):
        with self.repository.database_read() as db:
            self._scope(db, conversation_id, run_id, owner_key)
            return self._view(db, conversation_id, run_id, can_rate, answer_key)

    @staticmethod
    def _validate_source(source, case_id, answer_key):
        if (source is None or source.case_id != case_id or source.answer_key != answer_key
                or not source.report_markdown or hashlib.sha256(source.report_markdown.encode("utf-8")).hexdigest() != source.report_sha256
                or (source.source_kind == "REPORT") != (answer_key == "")):
            raise _unsupported()

    @staticmethod
    def _superseded(db, run_id, answer_key):
        # Accepted correction tasks are durable replacement receipts, even after
        # their card expires or the user later dislikes that correction answer.
        return db.execute("SELECT 1 FROM memory_tasks WHERE run_id=? AND replaces_answer_key=? AND source_kind='CORRECTION' AND evidence_json IS NOT NULL LIMIT 1", (run_id, answer_key)).fetchone() is not None

    def _enqueue(self, db, conversation_id, run_id, source, now):
        d = dialect_for(db)
        expired_receipt = ("source_kind='CORRECTION' AND status IN ('READY','FAILED','DELETED') AND card_json IS NULL "
            f"AND ({self._source_ended(d)} OR {d.timestamp('created_at')}<={d.timestamp('?')})")
        if (not source.problem_text.strip() or not source.skill_name.strip()
                or len(source.problem_text.encode("utf-8")) > 65_536
                or len(source.report_markdown.encode("utf-8")) > 65_536
                or len(source.context_json.encode("utf-8")) > 262_144
                or db.execute("SELECT count(*) FROM memory_tasks WHERE NOT (" + expired_receipt + ")",
                    (_before(now, SOURCE_RETENTION_DAYS),)).fetchone()[0] >= MAX_TASKS
                or self._superseded(db, run_id, source.answer_key)):
            return False
        if db.execute("SELECT 1 FROM memory_tasks WHERE run_id=? AND answer_key=?", (run_id, source.answer_key)).fetchone() is not None:
            return False
        db.execute("INSERT INTO memory_tasks (task_id,conversation_id,run_id,answer_key,source_kind,source_job_id,skill_name,report_sha256,status,problem_text,report_markdown,context_json,replaces_answer_key,superseded,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'PENDING',?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), conversation_id, run_id, source.answer_key, source.source_kind,
             source.source_job_id, source.skill_name, source.report_sha256, source.problem_text,
             source.report_markdown, source.context_json or None, source.replaces_answer_key,
             int(self._superseded(db, run_id, source.answer_key)), now, now))
        return True

    def enqueue_correction_in_transaction(self, db, conversation_id, run_id, source):
        """Called by follow-up completion while it owns the conversation lock."""
        if source.source_kind != "CORRECTION" or not source.answer_key or source.replaces_answer_key is None or source.replaces_answer_key == source.answer_key:
            raise ValueError("纠正经验必须明确替代同一轮次中的其他回答。")
        row = db.execute("SELECT r.case_id FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) WHERE r.run_id=? AND r.conversation_id=? AND c.deleted_at IS NULL", (run_id, conversation_id)).fetchone()
        if row is None:
            return False
        self._validate_source(source, row[0], source.answer_key)
        lock_key(db, "memory", "admission")
        return self._enqueue(db, conversation_id, run_id, source, self._now())

    def put_feedback(self, conversation_id, run_id, *, owner_key, request_id, rating,
                     source: FeedbackSource | None, extract_memory=True, answer_key=""):
        request, now = FeedbackRequest(request_id=request_id, rating=rating), self._now()
        with self.repository.database_transaction() as db:
            # Acquire the conversation first, matching completion/deletion. The
            # global admission lock only serializes feedback quotas/request IDs.
            case_id = self._scope(db, conversation_id, run_id, owner_key)
            lock_key(db, "memory", "admission")
            self._validate_source(source, case_id, answer_key)
            previous = db.execute("SELECT conversation_id,run_id,answer_key,rating FROM memory_feedback_requests WHERE owner_key=? AND request_id=?", (owner_key, request.request_id)).fetchone()
            if previous is not None:
                if previous != (conversation_id, run_id, answer_key, request.rating):
                    raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的目标和反馈不能更改。", 409)
                return self._view(db, conversation_id, run_id, True, answer_key)
            self._prune(db, now)
            current = _one(db, "SELECT * FROM memory_feedback WHERE run_id=? AND answer_key=?", (run_id, answer_key))
            if current is not None and (current["report_sha256"] != source.report_sha256 or current["source_job_id"] != source.source_job_id or current["owner_key"] != owner_key):
                raise _unsupported()
            task = _one(db, "SELECT * FROM memory_tasks WHERE run_id=? AND answer_key=?", (run_id, answer_key))
            if task is not None and (task["report_sha256"] != source.report_sha256 or task["source_kind"] != source.source_kind):
                raise _unsupported()
            if db.execute("SELECT count(*) FROM memory_feedback_requests WHERE run_id=? AND answer_key=?", (run_id, answer_key)).fetchone()[0] >= MAX_REQUESTS_PER_REPORT:
                raise _limit()
            if current is None:
                d = dialect_for(db)
                if db.execute("SELECT count(*) FROM memory_feedback f "
                              "JOIN agent_conversation_runs r ON r.run_id=f.run_id AND r.conversation_id=f.conversation_id "
                              "JOIN agent_conversations c ON c.conversation_id=f.conversation_id "
                              f"WHERE c.deleted_at IS NULL AND coalesce({d.json_int('r.body', '$.resources_expired')},0)=0").fetchone()[0] >= MAX_SOURCES:
                    raise _limit()
                db.execute("INSERT INTO memory_feedback(run_id,answer_key,conversation_id,owner_key,source_job_id,skill_name,report_sha256,rating,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (run_id, answer_key, conversation_id, owner_key, source.source_job_id, source.skill_name, source.report_sha256, request.rating, now))
            elif current["rating"] != request.rating:
                db.execute("UPDATE memory_feedback SET rating=?,updated_at=? WHERE run_id=? AND answer_key=?", (request.rating, now, run_id, answer_key))
            db.execute("INSERT INTO memory_feedback_requests(owner_key,request_id,conversation_id,run_id,answer_key,rating) VALUES (?,?,?,?,?,?)", (owner_key, request.request_id, conversation_id, run_id, answer_key, request.rating))
            if (request.rating == "LIKE" and task is None and extract_memory
                    and (current is None or not current["extraction_attempted"])):
                if self._enqueue(db, conversation_id, run_id, source, now):
                    db.execute("UPDATE memory_feedback SET extraction_attempted=1 WHERE run_id=? AND answer_key=?",
                        (run_id, answer_key))
            elif task is not None:
                active = (request.rating == "LIKE" and task["status"] == "READY" and task["card_json"] is not None
                    and not task["superseded"] and not self._superseded(db, run_id, answer_key)
                    and parse_timestamp(task["completed_at"]) > parse_timestamp(_before(now, CARD_RETENTION_DAYS)))
                db.execute("UPDATE memory_tasks SET active=? WHERE task_id=?", (int(active), task["task_id"]))
            return self._view(db, conversation_id, run_id, True, answer_key)

    def claim_task(self):
        now = self._now()
        with self.repository.database_transaction() as db:
            dialect = dialect_for(db)
            suffix = " FOR UPDATE OF t SKIP LOCKED" if dialect.postgres else ""
            created = dialect.timestamp("t.created_at")
            row = _one(db, "SELECT t.* FROM memory_tasks t JOIN agent_conversations c ON c.conversation_id=t.conversation_id JOIN agent_conversation_runs r ON r.run_id=t.run_id AND r.conversation_id=t.conversation_id "
                f"WHERE t.status='PENDING' AND {created}>{dialect.timestamp('?')} AND c.deleted_at IS NULL ORDER BY {created},t.task_id LIMIT 1" + suffix, (_before(now, SOURCE_RETENTION_DAYS),))
            if row is None or not try_lock_conversation(db, row["conversation_id"]):
                return None
            if db.execute("SELECT 1 FROM agent_conversations WHERE conversation_id=? AND deleted_at IS NULL", (row["conversation_id"],)).fetchone() is None:
                return None
            db.execute("UPDATE memory_tasks SET status='RUNNING',updated_at=? WHERE task_id=? AND status='PENDING'", (now, row["task_id"]))
            return row

    def finish_task(self, task_id, card_json, *, evidence_json=None):
        from .extraction import parse_card_json
        content, now = parse_card_json(card_json), self._now()
        with self.repository.database_transaction() as db:
            self._lock_task_owner(db, task_id)
            task = _one(db, "SELECT * FROM memory_tasks WHERE task_id=? AND status='RUNNING'", (task_id,))
            if task is None:
                return False
            if not self._comment_can_finish(db, task):
                db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? WHERE task_id=?", (now, task_id))
                return False
            correction = task["source_kind"] == "CORRECTION"
            if correction:
                if not isinstance(evidence_json, str) or len(evidence_json.encode()) > 65_536 or not isinstance(json.loads(evidence_json), dict) or not json.loads(evidence_json):
                    raise ValueError("纠正经验缺少经过核验的证据。")
                if task["replaces_answer_key"] is None:
                    raise ValueError("纠正经验缺少明确的替代目标。")
            elif evidence_json is not None:
                raise ValueError("普通经验不能提交纠正证据。")
            content = parse_card_json(content, sources=(task["problem_text"] or "", task["report_markdown"] or "", task["context_json"] or ""))
            alive = db.execute("SELECT 1 FROM agent_conversations c JOIN agent_conversation_runs r USING(conversation_id) WHERE c.conversation_id=? AND r.run_id=? AND c.deleted_at IS NULL", (task["conversation_id"], task["run_id"])).fetchone()
            rating = db.execute("SELECT rating FROM memory_feedback WHERE run_id=? AND answer_key=?", (task["run_id"], task["answer_key"])).fetchone()
            if alive is None or (rating is None and not correction) or parse_timestamp(task["created_at"]) <= parse_timestamp(_before(now, SOURCE_RETENTION_DAYS)):
                db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? WHERE task_id=?", (now, task_id))
                return False
            superseded = task["superseded"] or self._superseded(db, task["run_id"], task["answer_key"])
            active = not superseded and (rating is None or rating[0] == "LIKE")
            db.execute("UPDATE memory_tasks SET status='READY',problem_text=NULL,report_markdown=NULL,context_json=NULL,card_json=?,card_sha256=?,evidence_json=?,active=?,superseded=?,completed_at=?,updated_at=? WHERE task_id=?", (content, hashlib.sha256(content.encode("utf-8")).hexdigest(), evidence_json, int(active), int(superseded), now, now, task_id))
            if correction:
                self._supersede_prior_comments(db, task)
                db.execute("UPDATE memory_tasks SET active=0,superseded=1,problem_text=CASE WHEN status='PENDING' THEN NULL ELSE problem_text END,report_markdown=CASE WHEN status='PENDING' THEN NULL ELSE report_markdown END,context_json=CASE WHEN status='PENDING' THEN NULL ELSE context_json END,status=CASE WHEN status='PENDING' THEN 'FAILED' ELSE status END WHERE run_id=? AND answer_key=?", (task["run_id"], task["replaces_answer_key"]))
            return True

    def fail_task(self, task_id):
        with self.repository.database_transaction() as db:
            self._lock_task_owner(db, task_id)
            result = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? WHERE task_id=? AND status IN ('PENDING','RUNNING')", (self._now(), task_id))
            return result.rowcount == 1

    @staticmethod
    def _lock_task_owner(db, task_id):
        if dialect_for(db).postgres:
            row = db.execute("SELECT conversation_id FROM memory_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is not None:
                lock_conversation(db, row[0])

    def recover(self):
        with self.repository.database_transaction() as db:
            result = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? WHERE status='RUNNING'", (self._now(),))
            return result.rowcount

    def active_cards(self, skill_name):
        with self.repository.database_read() as db:
            dialect = dialect_for(db)
            completed = dialect.timestamp("completed_at")
            rows = db.execute("SELECT task_id,card_json,card_sha256,updated_at,skill_name,run_id,answer_key,source_kind FROM memory_tasks "
                f"WHERE skill_name=? AND active=1 AND superseded=0 AND status='READY' AND card_json IS NOT NULL AND {completed}>{dialect.timestamp('?')} ORDER BY {completed} DESC,task_id LIMIT ?", (skill_name, _before(self._now(), CARD_RETENTION_DAYS), MAX_TASKS)).fetchall()
            return [dict(zip(("card_id", "card_json", "card_sha256", "updated_at", "skill_name", "run_id", "answer_key", "source_kind"), (*row[:3], _timestamp(row[3]), *row[4:]))) for row in rows]

    def revoke_conversation(self, db, conversation_id):
        db.execute("UPDATE memory_feedback_comments SET text=NULL,analysis_text=NULL,analysis_reference=NULL WHERE conversation_id=?", (conversation_id,))
        db.execute("UPDATE memory_tasks SET status='DELETED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,evidence_json=NULL,card_json=NULL,card_sha256=NULL,updated_at=? WHERE conversation_id=?", (self._now(), conversation_id))

    def expire_sources(self, db, conversation_id, *, run_id=None):
        scope = "conversation_id=?" + (" AND run_id=?" if run_id is not None else "")
        parameters = (conversation_id,) if run_id is None else (conversation_id, run_id)
        db.execute("UPDATE memory_tasks SET status='DELETED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,card_json=NULL,card_sha256=NULL,updated_at=? WHERE status<>'READY' AND " + scope, (self._now(), *parameters))

    @staticmethod
    def _source_ended(dialect):
        return ("NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_tasks.run_id "
            f"AND coalesce({dialect.json_int('r.body', '$.resources_expired')},0)=0)")

    def _prune(self, db, now):
        self._prune_comments(db, now)
        dialect = dialect_for(db)
        raw = db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? "
            f"WHERE status IN ('PENDING','RUNNING') AND {dialect.timestamp('created_at')}<={dialect.timestamp('?')}", (now, _before(now, SOURCE_RETENTION_DAYS))).rowcount
        cards = db.execute("UPDATE memory_tasks SET active=0,card_json=NULL,card_sha256=NULL "
                           f"WHERE card_json IS NOT NULL AND {dialect.timestamp('completed_at')}<={dialect.timestamp('?')}",
                           (_before(now, CARD_RETENTION_DAYS),)).rowcount
        # Web runs and votes are permanent. Only live sources consume their
        # quota; extraction tasks and cards still have independent lifetimes.
        ended = self._source_ended(dialect)
        db.execute("UPDATE memory_tasks SET status='FAILED',active=0,problem_text=NULL,report_markdown=NULL,context_json=NULL,updated_at=? WHERE status IN ('PENDING','RUNNING') AND " + ended, (now,))
        # A compact correction row is also the one-shot/replacement receipt.
        # Keep it while its report exists, but do not charge expired receipts
        # against the live task quota. The original text and card still expire.
        orphan = "NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_tasks.run_id)"
        tasks = db.execute("DELETE FROM memory_tasks WHERE (source_kind<>'CORRECTION' OR " + orphan + ") AND ("
                           "(status='READY' AND card_json IS NULL) OR "
                           f"(status IN ('FAILED','DELETED') AND ({ended} OR {dialect.timestamp('created_at')}<={dialect.timestamp('?')})))",
                           (_before(now, SOURCE_RETENTION_DAYS),)).rowcount
        requests = db.execute("DELETE FROM memory_feedback_requests WHERE NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_feedback_requests.run_id)").rowcount
        feedback = db.execute("DELETE FROM memory_feedback WHERE NOT EXISTS (SELECT 1 FROM agent_conversation_runs r WHERE r.run_id=memory_feedback.run_id)").rowcount
        return {"expired_sources": raw, "expired_cards": cards, "tasks": tasks, "requests": requests, "feedback": feedback}

    def prune(self):
        with self.repository.database_transaction() as db:
            return self._prune(db, self._now())
