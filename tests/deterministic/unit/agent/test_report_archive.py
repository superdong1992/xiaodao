"""Web history and exact report downloads survive removal of large resources."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.report_archive import ReportArchive
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import ArtifactKind
from problem_locator.interfaces.agent_http import register_agent_routes
from tests.deterministic.unit.agent.test_conversation_detail import setup, publish as publish_report
from tests.deterministic.unit.application.test_reports import (
    ARTIFACT_ID, CASE_ID, OTHER_ID, generic_report, specialist_report,
)
from tests.deterministic.unit.storage.test_state_repository import _open


def configure(setup):
    queries = ApplicationQueryService(setup.repository, setup.resources, SimpleNamespace())
    setup.service.application.open_artifact = queries.open_artifact
    return queries


def publish(setup, aggregate=None):
    aggregate = aggregate or setup.aggregate
    if aggregate.case.archive_status == "PENDING":
        aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(
            update={"archive_status": "FAILED"})})
    publish_report(setup, aggregate)


def expire(setup):
    with setup.repository.database_transaction() as db:
        body = setup.store._load(db, setup.conversation_id)
        body.update(resources_expired=True, resources_expired_at="2026-10-15T00:00:00.000Z",
            resource_cleanup_reason="RETENTION_EXPIRED")
        setup.store._save(db, body)
    setup.repository._live.pop(CASE_ID, None)


@pytest.mark.parametrize("format", ["COMPLETED", "PARTIAL", "INCONCLUSIVE", "generic-v1", "markdown"])
def test_all_report_formats_survive_case_removal_and_archive_restart(setup, format):
    configure(setup)
    aggregate, resources, _ = (generic_report(1 if format == "generic-v1" else 2)
        if format in {"generic-v1", "markdown"} else specialist_report(format))
    setup.resources.content = resources.content
    setup.store.submit_message(setup.conversation_id, "question", "请帮我检查日志。")
    publish(setup, aggregate)
    before = setup.service.get_conversation(setup.conversation_id)
    run_id = before.selected_run_id
    assert setup.service.report_archive.archive_run(setup.conversation_id, run_id)
    expire(setup)
    setup.service.report_archive = ReportArchive(setup.service)
    after = setup.service.get_conversation(setup.conversation_id)
    assert after.result == before.result
    assert after.history == before.history
    assert after.resources_expired is True
    assert after.resource_cleanup_reason == "RETENTION_EXPIRED"
    assert setup.service.report_archive.archive_run(setup.conversation_id, run_id)
    if format != "generic-v1":
        opened = setup.service.open_conversation_artifact(setup.conversation_id, ARTIFACT_ID)
        assert opened.stream.read() == resources.content
        assert hashlib.sha256(resources.content).hexdigest() == opened.artifact.sha256
        opened.stream.close()


def test_archived_zip_metadata_is_private_to_expired_download_check(setup):
    configure(setup)
    publish(setup)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    artifact = setup.aggregate.artifacts[ARTIFACT_ID]
    archive = artifact.model_copy(update={"artifact_id": OTHER_ID, "kind": ArtifactKind.USER_RESULT_ARCHIVE,
        "name": "logs.zip", "content_type": "application/zip", "size": 1000000000,
        "metadata": {"description": "日志归档"}})
    aggregate = setup.aggregate.model_copy(update={"case": setup.aggregate.case.model_copy(
        update={"archive_status": "READY"}), "artifacts": {ARTIFACT_ID: artifact, OTHER_ID: archive}})
    publish(setup, aggregate)
    # An early backfill must pick up the subsequently published ZIP metadata.
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    expire(setup)
    detail = setup.service.get_conversation(setup.conversation_id)
    assert [item.artifact_id for item in detail.artifacts] == [ARTIFACT_ID]
    with pytest.raises(AgentStoreError) as error:
        setup.service.open_conversation_artifact(setup.conversation_id, OTHER_ID)
    assert error.value.status_code == 410
    assert error.value.code == "AGENT_RESOURCE_EXPIRED"


def test_corrupt_bytes_fail_closed_and_active_run_cannot_be_archived(setup):
    configure(setup)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert not setup.service.report_archive.archive_run(setup.conversation_id, rid)
    publish(setup)
    setup.resources.content = b"wrong report"
    assert not setup.service.report_archive.archive_run(setup.conversation_id, rid)
    assert setup.service.report_archive.read(setup.conversation_id, rid) is None


def test_delete_during_archive_read_cannot_write_late_report(setup):
    configure(setup)
    publish(setup)
    original = setup.service.application.open_artifact
    def read_and_delete(*args):
        result = original(*args)
        setup.store.request_delete(setup.conversation_id)
        return result
    setup.service.application.open_artifact = read_and_delete
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert not setup.service.report_archive.archive_run(setup.conversation_id, rid)
    with setup.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_report_archives").fetchone()[0] == 0


def test_archived_download_preserves_owner_share_and_delete_boundary(setup):
    queries = configure(setup)
    with setup.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversations SET owner_key=? WHERE conversation_id=?", ("a" * 64, setup.conversation_id))
    publish(setup)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    expire(setup)
    app = FastAPI()
    register_agent_routes(app, setup.service, "http://testserver", queries)
    path = f"/api/v1/agent/conversations/{setup.conversation_id}/files/{ARTIFACT_ID}/content?run_id={rid}"
    with TestClient(app) as client:
        assert client.get(path, headers={"X-Agent-Owner-Key": "b" * 64}).status_code == 404
        with setup.service.usage_guard.acquire_cleanup_if_idle(setup.conversation_id, temporary=True):
            assert client.get(path, headers={"X-Agent-Owner-Key": "b" * 64}).status_code == 404
            busy = client.get(path, headers={"X-Agent-Owner-Key": "a" * 64})
            assert busy.status_code == 503 and busy.headers["Retry-After"] == "5"
        response = client.get(path, headers={"X-Agent-Owner-Key": "a" * 64})
        assert response.status_code == 200
        assert response.content == setup.resources.content
        assert response.headers["X-Content-SHA256"] == hashlib.sha256(response.content).hexdigest()
        shared = client.get(path + "&share=true", headers={"X-Agent-Owner-Key": "b" * 64})
        assert shared.status_code == 200 and shared.content == response.content
        setup.store.request_delete(setup.conversation_id, owner_key="a" * 64)
        assert client.get(path, headers={"X-Agent-Owner-Key": "a" * 64}).status_code == 404
    assert setup.service.report_archive.read(setup.conversation_id, rid) is None


def test_idle_waiting_round_stops_without_restart_failure_and_new_message_opens_round(setup):
    setup.store.update_intake(setup.conversation_id, {}, ["请补充日志。"])
    with setup.repository.database_transaction() as db:
        body = setup.store._load(db, setup.conversation_id)
        body["last_business_activity_at"] = "2026-09-01T00:00:00.000Z"
        setup.store._save(db, body)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert setup.service.expire_waiting_run(setup.conversation_id, rid, "2026-10-01T00:00:00.000Z")
    old = setup.store.get_run(setup.conversation_id, rid)
    assert old["status"] == "CANCELLED"
    assert (old.get("failure") or {}).get("code") != "AGENT_INTERRUPTED"
    receipt = setup.service.send_message(setup.conversation_id, "after-expiry", "重新分析这份日志。")
    assert receipt.run_id != rid


def test_idle_expiry_rechecks_newly_accepted_business_activity(setup):
    setup.store.update_intake(setup.conversation_id, {}, ["请补充日志。"])
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    setup.service.send_message(setup.conversation_id, "fresh-input", "这是刚刚补充的现象。")
    assert not setup.service.expire_waiting_run(setup.conversation_id, rid, "2099-10-01T00:00:00.000Z")
    assert setup.store.get_run(setup.conversation_id)["status"] != "CANCELLED"


def test_closed_database_reopens_archived_report_without_any_case_or_file_reader(setup):
    configure(setup)
    publish(setup)
    original = setup.service.get_conversation(setup.conversation_id)
    assert setup.service.report_archive.archive_run(setup.conversation_id, original.selected_run_id)
    expire(setup)
    root = setup.repository.layout.data_root
    setup.repository.close()
    reopened = _open(root)
    try:
        store = AgentStore(reopened)
        # No application file/report methods: the old Case and resources are gone.
        service = AgentConversationService(store, SimpleNamespace(), SimpleNamespace(), reopened.layout)
        result = service.get_conversation(setup.conversation_id)
        assert result.result == original.result and result.resources_expired
        downloaded = service.open_conversation_artifact(setup.conversation_id, ARTIFACT_ID)
        assert downloaded.stream.read() == setup.resources.content
        downloaded.stream.close()
    finally:
        reopened.close()


def test_missing_case_falls_back_to_archive_even_before_expiry_projection(setup):
    configure(setup)
    publish(setup)
    original = setup.service.get_conversation(setup.conversation_id)
    assert setup.service.report_archive.archive_run(setup.conversation_id, original.selected_run_id)
    setup.repository._live.pop(CASE_ID, None)
    result = setup.service.get_conversation(setup.conversation_id)
    assert result.result == original.result
    assert result.resources_expired is False

