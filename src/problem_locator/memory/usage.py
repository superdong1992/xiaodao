"""Private usage identities and evidence-checked cross-report memory revisions."""
from __future__ import annotations

import hashlib
import json

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import DiagnosisMode
from problem_locator.storage.database import table_columns, table_names

USAGE_VERSION_KEY = "memory_usage_version"
USAGE_VERSION = "1"
USAGE_COLUMNS = {
    "memory_usages": {"job_id", "case_id", "skill_name", "card_id", "card_sha256", "source_kind", "created_at"},
    "memory_revisions": {"correction_task_id", "skill_name", "old_card_id", "old_card_sha256", "relation", "scope_terms_json", "published"},
}
USAGE_TABLES = set(USAGE_COLUMNS)
_SCHEMA = (
    "CREATE TABLE memory_usages (job_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,skill_name TEXT NOT NULL,card_id TEXT NOT NULL,card_sha256 TEXT NOT NULL,source_kind TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE memory_revisions (correction_task_id TEXT PRIMARY KEY,skill_name TEXT NOT NULL,old_card_id TEXT NOT NULL,old_card_sha256 TEXT NOT NULL,relation TEXT NOT NULL CHECK(relation IN ('GLOBAL_ERROR','NOT_APPLICABLE','DIAGNOSIS_OMISSION')),scope_terms_json TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)))",
)


def initialize_usage(db):
    existing = table_names(db).intersection(USAGE_TABLES)
    marker = db.execute("SELECT value FROM metadata WHERE key=?", (USAGE_VERSION_KEY,)).fetchone()
    if (bool(existing) != (marker is not None) or (existing and existing != USAGE_TABLES)
            or (marker is not None and marker[0] != USAGE_VERSION)
            or any(table_columns(db, table) != USAGE_COLUMNS[table] for table in existing)):
        raise AgentStoreError("STATE_SCHEMA_UNSUPPORTED", "经验使用记录与版本标记不一致。", 503)
    if not existing:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO metadata(key,value) VALUES (?,?)", (USAGE_VERSION_KEY, USAGE_VERSION))
    db.execute("CREATE INDEX IF NOT EXISTS memory_usage_case ON memory_usages(case_id)")
    db.execute("CREATE INDEX IF NOT EXISTS memory_usage_card ON memory_usages(card_id)")
    db.execute("CREATE INDEX IF NOT EXISTS memory_revision_rejected ON memory_revisions(skill_name,old_card_sha256,relation,published)")
    db.execute("CREATE INDEX IF NOT EXISTS memory_card_content ON memory_tasks(skill_name,card_sha256)")


class MemoryUsageMixin:
    def record_usage(self, job, selection):
        """One small insert after a successful Generic execution; never store source text.

        Card content is immutable and resolved by exact ID/hash later. This avoids
        copying another source's content or retaining it after explicit deletion.
        Only Web-owned runs have feedback targets; MCP-only runs need no receipt.
        """
        if job.diagnosis_mode is not DiagnosisMode.GENERIC or selection is None:
            return False
        from .extraction import parse_card_json
        content = parse_card_json(selection.card_json)
        if (content != selection.card_json or hashlib.sha256(content.encode()).hexdigest() != selection.card_sha256
                or selection.source_kind not in {"REPORT", "FOLLOWUP", "CORRECTION"}):
            raise ValueError("经验使用内容与校验值不一致。")
        identity = (job.case_id, job.generic_skill_name, selection.card_id, selection.card_sha256, selection.source_kind)
        with self.repository.database_transaction() as db:
            inserted = db.execute("INSERT INTO memory_usages(job_id,case_id,skill_name,card_id,card_sha256,source_kind,created_at) "
                "SELECT ?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                "WHERE r.case_id=? AND c.deleted_at IS NULL) AND EXISTS (SELECT 1 FROM memory_tasks t "
                "WHERE t.task_id=? AND t.skill_name=? AND t.card_sha256=? AND t.card_json=? AND t.source_kind=? AND t.status='READY') "
                "ON CONFLICT(job_id) DO NOTHING RETURNING job_id",
                (job.job_id, *identity, self._now(), job.case_id, selection.card_id, job.generic_skill_name,
                 selection.card_sha256, content, selection.source_kind)).fetchone()
            if inserted is not None:
                return True
            previous = db.execute("SELECT case_id,skill_name,card_id,card_sha256,source_kind FROM memory_usages WHERE job_id=?", (job.job_id,)).fetchone()
            if previous is not None and previous != identity:
                raise ValueError("同次诊断的经验使用记录不能改变。")
            return previous is not None

    def usage_for_task(self, task, *, db=None):
        # A normal follow-up does not independently recall shared memory. Never
        # attribute its feedback to the original report's historical reference.
        if task.get("source_kind") != "CORRECTION" or task.get("replaces_answer_key") != "":
            return None
        if db is None:
            with self.repository.database_read() as connection:
                return self.usage_for_task(task, db=connection)
        from .store import _one
        return _one(db, "SELECT u.job_id,u.case_id,u.skill_name,u.card_id,u.card_sha256,t.card_json,u.source_kind "
            "FROM memory_usages u JOIN memory_tasks t ON t.task_id=u.card_id AND t.card_sha256=u.card_sha256 "
            "AND t.skill_name=u.skill_name AND t.source_kind=u.source_kind "
            "JOIN agent_conversation_runs r ON r.case_id=u.case_id JOIN agent_conversations c ON c.conversation_id=r.conversation_id "
            "WHERE u.job_id=? AND u.skill_name=? AND r.run_id=? AND r.conversation_id=? AND c.deleted_at IS NULL "
            "AND t.status='READY' AND t.card_json IS NOT NULL",
            (task["source_job_id"], task["skill_name"], task["run_id"], task["conversation_id"]))

    @staticmethod
    def _globally_replaced(db, skill_name, digest):
        return db.execute("SELECT 1 FROM memory_revisions WHERE skill_name=? AND old_card_sha256=? AND relation='GLOBAL_ERROR' AND published=1 LIMIT 1",
            (skill_name, digest)).fetchone() is not None

    def _validated_revision(self, db, task, content, receipt):
        from .review import validate_memory_review
        usage = self.usage_for_task(task, db=db)
        review = receipt.get("memory_review")
        if usage is None:
            if "memory_review" in receipt:
                raise ValueError("纠正引用的经验使用记录已不可用。")
            return None
        return validate_memory_review(review, usage, receipt.get("evidence"), content)

    @staticmethod
    def _publish_revision(db, task, review, *, active):
        if review is None or review["relation"] == "UNRELATED":
            return
        db.execute("INSERT INTO memory_revisions(correction_task_id,skill_name,old_card_id,old_card_sha256,relation,scope_terms_json) "
            "VALUES (?,?,?,?,?,?)", (task["task_id"], task["skill_name"], review["card_id"], review["card_sha256"],
            review["relation"], json.dumps(review["scope_terms"], ensure_ascii=False, separators=(",", ":"))))
        if active and not MemoryUsageMixin._activate_revision(db, task["task_id"]):
            db.execute("UPDATE memory_tasks SET active=0 WHERE task_id=?", (task["task_id"],))

    @staticmethod
    def _activate_revision(db, task_id):
        row = db.execute("SELECT skill_name,old_card_sha256,relation,old_card_id FROM memory_revisions WHERE correction_task_id=? AND published=0", (task_id,)).fetchone()
        if row is None:
            return True
        if db.execute("SELECT 1 FROM memory_tasks WHERE task_id=? AND skill_name=? AND card_sha256=? "
                "AND status='READY' AND card_json IS NOT NULL", (row[3], row[0], row[1])).fetchone() is None:
            return False
        db.execute("UPDATE memory_revisions SET published=1 WHERE correction_task_id=?", (task_id,))
        if row[2] == "GLOBAL_ERROR":
            # Same verified content is rejected even when independently liked.
            # Keep the minimal hash tombstone if the correcting source is deleted.
            db.execute("UPDATE memory_tasks SET active=0,superseded=1 WHERE skill_name=? AND card_sha256=?",
                (row[0], row[1]))
        return True

    @staticmethod
    def _revoke_usage(db, conversation_id):
        db.execute("DELETE FROM memory_usages WHERE case_id IN (SELECT case_id FROM agent_conversation_runs WHERE conversation_id=?) "
            "OR card_id IN (SELECT task_id FROM memory_tasks WHERE conversation_id=?)", (conversation_id, conversation_id))
        db.execute("DELETE FROM memory_revisions WHERE (relation<>'GLOBAL_ERROR' OR published=0) AND correction_task_id IN "
            "(SELECT task_id FROM memory_tasks WHERE conversation_id=?)", (conversation_id,))

    @staticmethod
    def _prune_usage(db):
        db.execute("DELETE FROM memory_usages WHERE NOT EXISTS (SELECT 1 FROM agent_conversation_runs r "
            "JOIN agent_conversations c USING(conversation_id) WHERE r.case_id=memory_usages.case_id AND c.deleted_at IS NULL)")
