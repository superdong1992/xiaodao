"""Permanent Web reports and disposable resources on the production SQL adapter."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from problem_locator.agent.cleanup import ConversationCleanupService
from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.agent.usage import ConversationUsageGuard
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.storage.coordination import AttachmentUploadRegistry, StorageCoordinationLock
from problem_locator.storage.history_retention import HistoryRetentionService
from problem_locator.storage.quarantine import QuarantineMover
from problem_locator.storage.state_repository import CaseStateRepository
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent.test_conversation_detail import publish
from tests.deterministic.unit.application.test_reports import ARTIFACT_ID, CASE_ID, generic_report, specialist_report
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FaultInjectingReplace
from tests.deterministic.unit.storage.test_history_retention import MutableClock
from tests.deterministic.unit.storage.test_web_resource_retention import advance, upload

# The same file and concurrency assertions must also hold on actual PostgreSQL.
from tests.deterministic.unit.agent.test_cleanup_history_receipts import (
    test_user_delete_racing_history_isolation_waits_for_physical_cleanup,
    test_unrelated_history_receipt_does_not_block_explicit_delete,
)
from tests.deterministic.unit.storage.test_web_resource_retention import (
    test_text_history_and_metadata_survive_web_deadline_and_reopen,
    test_shared_attachment_protects_new_waiting_run_during_pressure,
    test_completed_followup_answers_survive_resource_reclamation,
)


@pytest.fixture
def stack(postgres_repository):
    repository, clock = postgres_repository, MutableClock()
    coordination = StorageCoordinationLock()
    store = AgentStore(repository, clock=clock, runtime_epoch="postgres-web-retention")
    usage, registry = ConversationUsageGuard(), AttachmentUploadRegistry()
    quarantine = QuarantineMover(repository.layout, coordination, FakeFileSync(), FaultInjectingReplace())
    history = HistoryRetentionService(repository, store, quarantine, usage, clock,
        attachment_registry=registry, coordination_lock=coordination, web_retention_days=15)
    cleanup = ConversationCleanupService(store, repository, quarantine, usage)
    return SimpleNamespace(repository=repository, root=repository.layout.data_root, clock=clock,
        store=store, usage=usage, registry=registry, quarantine=quarantine, history=history, cleanup=cleanup)


class _FileResources:
    def __init__(self, root):
        self.root = root

    def open_read(self, resource):
        return (self.root / resource.storage_key).open("rb")


@pytest.mark.parametrize("report_format", ["markdown", "json"])
def test_postgres_report_bytea_survives_resource_cleanup_thirty_days_and_database_reopen(
        stack, postgres_database_url, report_format):
    created = stack.store.create_conversation("permanent-report", owner_key="owner")
    cid, rid = created.conversation_id, created.run_id
    aid, log_path = upload(stack, cid)
    stack.store.submit_message(cid, "question", "原始问题：请核对日志。", [aid])
    stack.store.append_event(cid, "assistant.question", {"questions": ["故障开始于何时？"]}, dedupe_key="ask")
    aggregate, content, _ = generic_report() if report_format == "markdown" else specialist_report()
    aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(update={"archive_status": "FAILED"})})
    artifact = aggregate.artifacts[ARTIFACT_ID]
    report_path = stack.root / artifact.storage_key
    report_path.parent.mkdir(parents=True)
    report_path.write_bytes(content.content)
    queries = ApplicationQueryService(stack.repository, _FileResources(stack.root), SimpleNamespace())
    service = AgentConversationService(stack.store, queries, SimpleNamespace(), stack.repository.layout)
    service.usage_guard = stack.usage
    # Result fixtures intentionally bypass evidence replay, as the report API
    # reads published results. Agent projections and all archive SQL are real.
    publish(SimpleNamespace(repository=stack.repository, store=stack.store, conversation_id=cid), aggregate)
    before = service.get_conversation(cid)
    stack.history.archive_run = service.report_archive.archive_run
    advance(stack, 15)
    assert stack.history.run_once()
    assert not report_path.exists() and not log_path.exists()
    assert not stack.repository.read_snapshot(CASE_ID).cases
    with stack.repository.database_read() as db:
        assert db.execute("SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() "
            "AND table_name='agent_report_archives' AND column_name='content'").fetchone() == ("bytea",)
        assert bytes(db.execute("SELECT content FROM agent_report_archives WHERE run_id=?", (rid,)).fetchone()[0]) == content.content
    advance(stack, 365)
    stack.history.run_once()
    root = stack.root
    stack.repository.close()
    reopened = CaseStateRepository(root, StorageCoordinationLock(), stack.clock,
        DeterministicIdGenerator(), database_url=postgres_database_url)
    try:
        store = AgentStore(reopened, clock=stack.clock, runtime_epoch="postgres-web-restarted")
        # No report reader or physical resource store remains after restart.
        restored = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), reopened.layout)
        detail = restored.get_conversation(cid)
        assert detail.history == before.history and detail.result == before.result
        assert detail.resources_expired and detail.resource_cleanup_reason == "RETENTION_EXPIRED"
        assert store.get_attachment(aid).status == "EXPIRED"
        download = restored.open_conversation_artifact(cid, ARTIFACT_ID)
        assert download.stream.read() == content.content
        assert download.artifact.sha256 == artifact.sha256
        download.stream.close()
        assert store.list_conversations("owner").items[0].conversation_id == cid
        store.request_delete(cid, owner_key="owner")
        cleanup = ConversationCleanupService(store, reopened,
            QuarantineMover(reopened.layout, StorageCoordinationLock(), FakeFileSync(), FaultInjectingReplace()),
            restored.usage_guard)
        assert cleanup.run_once()
        assert store.request_delete(cid, owner_key="owner").status == "DELETED"
        assert not restored.report_archive.archive_run(cid, rid)
        with reopened.database_read() as db:
            assert db.execute("SELECT count(*) FROM agent_report_archives WHERE conversation_id=?", (cid,)).fetchone()[0] == 0
        with pytest.raises(AgentStoreError) as gone:
            restored.get_conversation(cid)
        assert gone.value.status_code == 404
    finally:
        reopened.close()
