"""Offline legacy feedback projections and permanent report BYTEA imports."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.entrypoints import postgres_import as migration
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import CaseStateRepository
from tests.postgres_helpers import postgres_database_url
from tests.deterministic.postgres.test_postgres_import import migration_host, _manifest, _verify_table_proofs
from tests.deterministic.unit.agent.test_conversation_detail import publish
from tests.deterministic.unit.agent.test_report_archive import expire
from tests.deterministic.unit.application.test_reports import ARTIFACT_ID, generic_report
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator
from tests.deterministic.unit.storage.test_history_retention import MutableClock
from tests.deterministic.unit.storage.test_state_repository import _open

OWNER = "a" * 64


@pytest.mark.parametrize("legacy_task,attempted", [("matching", 1), ("different-report", 0), ("none", 0)])
def test_postgres_import_derives_legacy_feedback_without_changing_source_and_keeps_report_bytes(
        tmp_path, migration_host, postgres_database_url, monkeypatch, legacy_task, attempted):
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    clock = MutableClock()
    repository = _open(source_root)
    try:
        store = AgentStore(repository, clock=clock, runtime_epoch="sqlite-report-import")
        memory = MemoryStore(repository, clock)
        created = store.create_conversation("legacy-report", owner_key=OWNER)
        cid, rid = created.conversation_id, created.run_id
        store.submit_message(cid, "question", "升级前的原始问题。")
        aggregate, resources, _ = generic_report()
        queries = ApplicationQueryService(repository, resources, SimpleNamespace())
        service = AgentConversationService(store, queries, SimpleNamespace(), repository.layout)
        context = SimpleNamespace(repository=repository, store=store, conversation_id=cid)
        publish(context, aggregate)
        report_source = FeedbackSource(aggregate.case.case_id,
            aggregate.case.generic_result_v2.source_job_id, "generic-test", "升级前的原始问题。",
            resources.content.decode(), aggregate.case.generic_result_v2.report_sha256)
        memory.put_feedback(cid, rid, owner_key=OWNER, request_id="like", rating="LIKE",
            source=report_source, extract_memory=legacy_task != "none")
        assert service.report_archive.archive_run(cid, rid)
        expire(context)
        original = service.get_conversation(cid)
        with repository.database_transaction() as db:
            # Model the drained, pre-upgrade feedback schema. The old task is
            # the only durable evidence that extraction was already attempted.
            db.execute("UPDATE memory_tasks SET status='FAILED',problem_text=NULL,report_markdown=NULL")
            if legacy_task == "different-report":
                db.execute("UPDATE memory_tasks SET report_sha256=?", ("f" * 64,))
            db.execute("ALTER TABLE memory_feedback DROP COLUMN extraction_attempted")
            expected_feedback = db.execute("SELECT * FROM memory_feedback").fetchone()
            feedback_columns = [row[1] for row in db.execute("PRAGMA table_info(memory_feedback)")]
    finally:
        repository.close()
    (source_root / ".instance.lock").write_bytes(b"offline report source\n")
    before = _manifest(source_root)
    monkeypatch.setenv("POSTGRES_WEB_IMPORT_TEST_URL", postgres_database_url)
    result = migration.import_data_root(source_root, target_root, execute=True,
        database_url_env="POSTGRES_WEB_IMPORT_TEST_URL")
    assert result["status"] == "IMPORTED" and _manifest(source_root) == before
    proof = result["tables"]["memory_feedback"]
    assert proof["derived_columns"] == ["extraction_attempted"]
    assert proof["source"] == migration._digest([expected_feedback])
    reopened = CaseStateRepository(target_root, StorageCoordinationLock(), clock,
        DeterministicIdGenerator(), database_url=postgres_database_url)
    try:
        _verify_table_proofs(reopened, result)
        with reopened.database_read() as db:
            selected = ",".join('"' + name + '"' for name in feedback_columns)
            assert db.execute(f"SELECT {selected},extraction_attempted FROM memory_feedback").fetchone() == (*expected_feedback, attempted)
            assert db.execute("SELECT content,pg_typeof(content)::text FROM agent_report_archives WHERE run_id=?",
                (rid,)).fetchone() == (resources.content, "bytea")
        imported_store = AgentStore(reopened, clock=clock, runtime_epoch="postgres-imported-report")
        restored = AgentConversationService(imported_store, SimpleNamespace(), SimpleNamespace(), reopened.layout)
        detail = restored.get_conversation(cid)
        assert detail.history == original.history and detail.result == original.result
        assert detail.resources_expired
        downloaded = restored.open_conversation_artifact(cid, ARTIFACT_ID)
        assert downloaded.stream.read() == resources.content
        downloaded.stream.close()
        imported_memory = MemoryStore(reopened, clock)
        assert imported_memory.get_feedback(cid, rid, owner_key=OWNER, can_rate=True).rating == "LIKE"
    finally:
        reopened.close()
