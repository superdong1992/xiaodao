"""Small, immutable Web report archives independent of disposable Case files."""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass

from problem_locator.application.reports import MAX_REPORT_BYTES
from problem_locator.diagnostics import log_event
from problem_locator.storage.database import dialect_for, lock_conversation

from .models import AgentStoreError, ConversationArtifact, ConversationReportView

_CLOSED = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}


@dataclass(frozen=True)
class ArchivedReport:
    result: ConversationReportView
    artifacts: list[ConversationArtifact]
    content: bytes

    def validate_content(self):
        artifact = self.result.artifact
        if artifact is None:
            valid = not self.content
        else:
            valid = (len(self.content) == artifact.size <= MAX_REPORT_BYTES
                and hashlib.sha256(self.content).hexdigest() == artifact.sha256)
            valid = valid and any(item.model_dump(exclude={"download_url"}) == artifact.model_dump()
                for item in self.artifacts)
            if valid:
                try:
                    valid = (self.content.decode("utf-8") == self.result.markdown
                        if self.result.format == "markdown" else
                        json.loads(self.content) == self.result.report.model_dump(mode="json"))
                except (UnicodeError, ValueError, AttributeError):
                    valid = False
        if not valid:
            raise AgentStoreError("STATE_CORRUPT", "历史报告校验失败，暂时无法读取。", 503)
        return self


@dataclass(frozen=True)
class ArchivedDownload:
    artifact: ConversationArtifact
    stream: io.BytesIO


class ReportArchive:
    """Never stores attachments, logs or a full execution aggregate in SQL.

    Retention calls archive_run while holding its exclusive conversation lease;
    no operation lease is acquired here. Publication and explicit deletion use
    the same conversation SQL lock so a late archive cannot resurrect deletion.
    """

    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.repository = self.store.repository
        self.ensure_schema(self.repository)

    @staticmethod
    def ensure_schema(repository):
        with repository.database_transaction() as db:
            binary = "BYTEA" if dialect_for(db).postgres else "BLOB"
            db.execute("CREATE TABLE IF NOT EXISTS agent_report_archives ("
                "run_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,"
                f"body TEXT NOT NULL,content {binary} NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_report_archives_conversation "
                "ON agent_report_archives(conversation_id)")

    def read(self, conversation_id, run_id):
        with self.repository.database_read() as db:
            row = db.execute("SELECT a.body,a.content FROM agent_report_archives a "
                "JOIN agent_conversations c ON c.conversation_id=a.conversation_id "
                "WHERE a.conversation_id=? AND a.run_id=? AND c.deleted_at IS NULL",
                (conversation_id, run_id)).fetchone()
        if row is None:
            return None
        body = json.loads(row[0])
        result = ConversationReportView.model_validate(body["result"])
        artifacts = [ConversationArtifact.model_validate(item) for item in body["artifacts"]]
        if result.conversation_id != conversation_id or any(
                item.created_by_job_id != result.source_job_id for item in artifacts):
            raise AgentStoreError("STATE_CORRUPT", "历史报告归属校验失败。", 503)
        return ArchivedReport(result, artifacts, bytes(row[1])).validate_content()

    def archive_run(self, conversation_id, run_id):
        """Return true only if the visible result survives removing the Case."""
        try:
            existing = self.read(conversation_id, run_id)
            body = self.store.get_run(conversation_id, run_id)
            if body["status"] not in _CLOSED:
                return False
            if existing is not None and body.get("resources_expired"):
                return True
            if body.get("case_id") is None:
                return not body.get("report_available", False)
            captured = self.store.read_conversation(conversation_id, run_id=run_id, case_snapshot=True)
            if captured.snapshot is None or body["case_id"] not in captured.snapshot.cases:
                return existing is not None or not body.get("report_available", False)
            _, published, artifacts = self.service.application.read_conversation_delivery(
                body["case_id"], captured.snapshot, report=True, artifacts=True)
            result = self.service._report_view(captured.view, published)
            if body.get("report_available") and result.report_state != "READY":
                return False
            content = b""
            if result.artifact is not None:
                opened = self.service.application.open_artifact(body["case_id"], result.artifact.artifact_id)
                try:
                    if opened.artifact.sha256 != result.artifact.sha256 or opened.artifact.size != result.artifact.size:
                        return False
                    chunks, remaining = [], min(result.artifact.size, MAX_REPORT_BYTES) + 1
                    while remaining:
                        chunk = opened.stream.read(min(65536, remaining))
                        if not isinstance(chunk, bytes) or len(chunk) > min(65536, remaining):
                            raise ValueError("invalid report stream")
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    content = b"".join(chunks)
                finally:
                    opened.stream.close()
            archived = ArchivedReport(result, [ConversationArtifact.model_validate(
                item.model_dump(mode="json")) for item in artifacts], content).validate_content()
            if existing is not None and (existing.content != content or existing.result.case_id != result.case_id
                    or existing.result.format != result.format or existing.result.artifact != result.artifact):
                raise ValueError("published report identity changed")
            raw = json.dumps({"result": result.model_dump(mode="json"), "artifacts": [
                item.model_dump(mode="json") for item in archived.artifacts]}, ensure_ascii=False, separators=(",", ":"))
            with self.repository.database_transaction() as db:
                lock_conversation(db, conversation_id)
                latest = self.store._load(db, conversation_id, run_id=run_id)
                if latest["status"] not in _CLOSED or latest.get("case_id") != body["case_id"]:
                    return False
                db.execute("INSERT INTO agent_report_archives(run_id,conversation_id,body,content) "
                    "VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET body=excluded.body",
                    (run_id, conversation_id, raw, content))
            return self.read(conversation_id, run_id) is not None
        except Exception as error:
            # A failed archive must fail closed: retention keeps source bytes.
            log_event("agent.report_archive.failed", conversation_id=conversation_id,
                run_id=run_id, error_type=type(error).__name__)
            return False

    def open(self, conversation_id, run_id, artifact_id, *, resources_expired):
        archived = self.read(conversation_id, run_id)
        if archived is None:
            return None
        artifact = next((item for item in archived.artifacts if item.artifact_id == artifact_id), None)
        if artifact is None:
            return None
        if archived.result.artifact is not None and artifact_id == archived.result.artifact.artifact_id:
            return ArchivedDownload(artifact, io.BytesIO(archived.content))
        if resources_expired:
            raise AgentStoreError("AGENT_RESOURCE_EXPIRED", "相关日志已清理，该文件已无法下载。", 410)
        return None
