"""Conversation/run isolation, deletion intents and byte-preserving v1 migration."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentEvent, AgentStoreError, ConversationDetail
from problem_locator.agent.store import AgentStore, upgrade_agent_storage_v2
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _open


@pytest.fixture
def store(tmp_path):
    repository = _open(tmp_path)
    result = AgentStore(repository, runtime_epoch="first")
    yield result
    repository.close()


def _stop(store, cid, run_id):
    receipt = store.request_stop(cid, "stop:" + run_id, run_id)
    assert receipt.status == "CANCELLING"
    store.finish_stop(cid, run_id)


def test_new_run_has_new_input_and_original_message_replay_cannot_target_it(store):
    created = store.create_conversation("create")
    cid = created.conversation_id
    original = store.submit_message(cid, "first", "第一轮问题")
    store.set_draft(cid, {"problem": "old private facts"})
    _stop(store, cid, original.run_id)
    newer = store.submit_message(cid, "second", "第二轮独立问题")
    assert newer.run_id != original.run_id
    assert store.create_conversation("create") == created
    assert store.submit_message(cid, "first", "第一轮问题") == original
    assert store.get_draft(cid) == {}
    assert store.has_pending_messages(cid)
    assert not store.has_pending_messages(cid, run_id=original.run_id)
    assert [m.text for m in store.get_conversation(cid).messages] == ["第二轮独立问题"]
    assert store.get_run(cid, original.run_id)["status"] == "CANCELLED"
    with store.run_scope(cid, original.run_id):
        with pytest.raises(AgentStoreError):
            store.set_draft(cid, {"late": "old result"})
    assert store.get_draft(cid) == {}
    assert store.request_stop(cid, "stop:" + original.run_id, original.run_id).status == "CANCELLED"
    assert not store.stop_requested(cid, newer.run_id)
    with pytest.raises(AgentStoreError) as conflict:
        store.submit_message(cid, "old-target", "旧轮补充", target_run_id=original.run_id)
    assert conflict.value.code == "AGENT_RUN_CHANGED"


def test_detail_keeps_conversation_creation_time_after_new_run(store, monkeypatch):
    monkeypatch.setattr(store, "_now", lambda: "2026-09-18T01:00:00.000Z")
    created = store.create_conversation("create")
    _stop(store, created.conversation_id, created.run_id)
    monkeypatch.setattr(store, "_now", lambda: "2026-09-18T02:00:00.000Z")
    store.submit_message(created.conversation_id, "new", "新诊断")
    current = store.read_conversation(created.conversation_id)
    old = store.read_conversation(created.conversation_id, run_id=created.run_id)
    assert current.view.created_at == old.view.created_at == "2026-09-18T01:00:00.000Z"
    assert current.current_run.created_at == "2026-09-18T02:00:00.000Z"


def test_stopping_fences_late_create_projection_and_waits_for_controller(store):
    receipt = store.create_conversation("create")
    cid, rid = receipt.conversation_id, receipt.run_id
    message = store.submit_message(cid, "first", "问题")
    store.request_stop(cid, "stop", rid)
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=JOB_ID,
        status=SimpleNamespace(value="RUNNING"), archive_status="NOT_REQUIRED", case_revision=1)
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), SimpleNamespace(case=case))
    assert store.get_run(cid)["status"] == "CANCELLING"
    assert store.pending_stops()[0]["case_id"] == CASE_ID
    assert store.conversation_for_case(CASE_ID) == cid
    assert store.get_conversation(cid).messages[0].status == "UNUSED"
    with pytest.raises(AgentStoreError):
        store.record_dispatch(cid, "late-dispatch", {"operation": "INTAKE"})
    with pytest.raises(AgentStoreError):
        store.set_message_status(cid, message.message_id, "PROCESSING")
    store.finish_stop(cid, rid)
    assert store.get_status(cid).status == "CANCELLED"
    assert store.pending_stops() == []
    assert not any(event.type == "agent.failed" for event in store.list_events(cid))


def test_report_winning_stop_race_keeps_report_and_settles_stop_receipt(store, monkeypatch):
    created = store.create_conversation("create")
    cid, rid = created.conversation_id, created.run_id
    store.request_stop(cid, "stop", rid)
    monkeypatch.setattr("problem_locator.agent.store.project_artifact_summaries", lambda *a, **kw: [])
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=None,
        status=SimpleNamespace(value="RESOLVED"), archive_status="PENDING", case_revision=2,
        generic_result=object(), generic_result_v2=None)
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), SimpleNamespace(case=case, artifacts={}))
    assert store.get_status(cid).report_state == "READY"
    assert store.get_status(cid).archive_status == "PENDING"
    assert store.pending_stops() == []
    assert store.request_stop(cid, "stop", rid).status == "ALREADY_FINISHED"
    assert store.read_conversation(cid).capabilities.can_rediagnose
    summary = store.list_conversations(None).items[0]
    assert summary.capabilities.can_rediagnose is True
    assert summary.capabilities.can_stop is False


def test_owner_directory_cursor_and_rename_are_isolated(store):
    owner, other = "a" * 64, "b" * 64
    first = store.create_conversation("same-request", owner_key=owner)
    second = store.create_conversation("same-request", owner_key=other)
    assert first.conversation_id != second.conversation_id
    store.submit_message(first.conversation_id, "message", "  诊断描述\n" + "字" * 100)
    assert len(store.list_conversations(owner).items[0].title) == 80
    renamed = store.rename_conversation(first.conversation_id, "手工标题")
    assert renamed.title == "手工标题"
    assert renamed.current_run.run_id == first.run_id
    assert renamed.capabilities.can_send
    store.create_conversation("another", owner_key=owner)
    first_page = store.list_conversations(owner, limit=1)
    next_page = store.list_conversations(owner, cursor=first_page.next_cursor, limit=1)
    assert len(first_page.items) == len(next_page.items) == 1
    assert first_page.items[0].conversation_id != next_page.items[0].conversation_id
    assert {item.title for item in first_page.items + next_page.items} == {"手工标题", "新诊断"}
    assert next_page.next_cursor is None
    with pytest.raises(AgentStoreError):
        store.list_conversations(other, cursor=first_page.next_cursor)
    with pytest.raises(AgentStoreError):
        store.require_owner(first.conversation_id, other)
    legacy = store.create_conversation("unknown-owner")
    store.require_owner(legacy.conversation_id, None)
    with pytest.raises(AgentStoreError):
        store.require_owner(legacy.conversation_id, owner)


def test_history_is_bounded_chronological_and_uses_index_without_case_reads(store, monkeypatch):
    created = store.create_conversation("create")
    cid = created.conversation_id
    for index in range(55):
        store.submit_message(cid, str(index), "问题 " + str(index))
    monkeypatch.setattr(store.repository, "read_case_snapshot_with", lambda *a: pytest.fail("history must not read Case"))
    latest = store.read_conversation(cid, history=True)
    assert len(latest.history) == 50
    assert latest.history[0].message.text == "问题 5"
    assert latest.history[-1].message.text == "问题 54"
    assert latest.history_next_cursor
    earlier = store.read_conversation(cid, history=True, history_before=latest.history_next_cursor)
    assert [item.message.text for item in earlier.history] == ["问题 " + str(i) for i in range(5)]
    assert earlier.history_next_cursor is None
    light = store.read_conversation(cid)
    assert light.history is light.attachments is None
    assert light.selected_run_id == light.current_run.run_id == created.run_id
    with pytest.raises(AgentStoreError):
        store.read_conversation(cid, history=True, history_limit=101)
    with store.repository.database_read() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT body,run_id,sequence FROM agent_events WHERE conversation_id=? AND sequence<? AND json_extract(body, '$.type') IN ('message.accepted','assistant.question','result.available','conversation.completed') ORDER BY sequence DESC LIMIT ?", (cid, 999, 51)).fetchall()
    assert any("agent_history" in row[-1] for row in plan)
    detail = ConversationDetail(**{**latest.view.model_dump(), "schema_version": 3}, title=latest.title,
        current_run=latest.current_run, capabilities=latest.capabilities, selected_run_id=latest.selected_run_id,
        history=latest.history, attachments=latest.attachments, history_next_cursor=latest.history_next_cursor,
        included=["history"])
    assert len(detail.messages) == 50
    assert "messages" not in detail.model_dump()
    assert "messages" not in ConversationDetail.model_json_schema()["properties"]


def test_directory_reads_indexed_summaries_without_loading_private_drafts(store, monkeypatch):
    owner = "a" * 64
    created = store.create_conversation("create", owner_key=owner)
    store.set_draft(created.conversation_id, {"large_private_input": "x" * 1000000})
    monkeypatch.setattr(store, "_load", lambda *a, **kw: pytest.fail("directory must not load a full run"))
    monkeypatch.setattr(store.repository, "read_case_snapshot_with", lambda *a: pytest.fail("directory must not read Case"))
    result = store.list_conversations(owner)
    assert result.items[0].current_run.run_id == created.run_id
    assert len(result.model_dump_json()) < 2000
    with store.repository.database_read() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT conversation_id,updated_at FROM agent_conversations WHERE owner_key IS ? AND deleted_at IS NULL ORDER BY updated_at DESC,conversation_id DESC LIMIT ?", (owner, 21)).fetchall()
    assert any("agent_directory" in row[-1] for row in plan)


def test_work_discovery_keeps_uncovered_input_and_current_dispatch_without_idle_runs(store):
    created = store.create_conversation("create")
    empty = store.create_conversation("empty")
    cid = created.conversation_id
    assert store.pending_conversations() == []
    message = store.submit_message(cid, "first", "尚未整理的原始问题")
    assert store.pending_conversations() == [cid]
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=JOB_ID,
        status=SimpleNamespace(value="RUNNING"), archive_status="NOT_REQUIRED", case_revision=1)
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), SimpleNamespace(case=case))
    assert store.pending_conversations() == []
    case.status = SimpleNamespace(value="WAITING_INPUT")
    case.case_revision = 2
    case.diagnosis_state = SimpleNamespace(pending_requirements=[])
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, cid), SimpleNamespace(case=case))
    assert store.pending_conversations() == [cid]
    store.set_message_status(cid, message.message_id, "APPLIED")
    store.finish_intake(cid, [message.message_id])
    assert store.pending_conversations() == []
    store.record_dispatch(cid, "retry-current", {"operation": "CreateCase"})
    assert store.pending_conversations() == [cid]
    store.complete_dispatch("retry-current")
    assert store.pending_conversations() == []
    # A historical unfinished receipt must not wake the new run.
    store.record_dispatch(cid, "old-run-command", {"operation": "CreateCase"})
    store.fail_conversation(cid)
    newer = store.submit_message(cid, "new-run", "新的输入")
    store.set_message_status(cid, newer.message_id, "APPLIED")
    store.finish_intake(cid, [newer.message_id])
    assert store.pending_conversations() == []
    assert empty.conversation_id not in store.pending_conversations()


def test_work_discovery_uses_partial_indexes_in_one_read(store):
    statements = []
    with store.repository.database_read() as db:
        db.set_trace_callback(statements.append)
    try:
        assert store.pending_conversations() == []
    finally:
        with store.repository.database_read() as db:
            db.set_trace_callback(None)
    selects = [sql for sql in statements if sql.startswith("SELECT ")]
    assert len(selects) == 1
    with store.repository.database_read() as db:
        plans = db.execute("EXPLAIN QUERY PLAN " + selects[0]).fetchall()
    details = " ".join(row[-1] for row in plans)
    assert "agent_conversations_intake_work" in details
    assert "agent_dispatches_pending" in details
    assert not any(table in selects[0] for table in ("agent_messages", "agent_events", "completed_cases"))


def test_history_restores_each_failed_cancelled_and_interrupted_run_without_reports(store, monkeypatch):
    created = store.create_conversation("create")
    cid = created.conversation_id
    store.submit_message(cid, "first", "失败的问题")
    store.fail_conversation(cid)
    second = store.submit_message(cid, "second", "用户取消的问题")
    _stop(store, cid, second.run_id)
    third = store.submit_message(cid, "third", "因重启中断的问题")
    store.fail_conversation(cid, "AGENT_INTERRUPTED", interrupted=True)
    store.submit_message(cid, "fourth", "新的问题")
    monkeypatch.setattr(store.repository, "read_case_snapshot_with", lambda *a: pytest.fail("history must not read Case or reports"))
    history = store.read_conversation(cid, history=True).history
    outcomes = [entry for entry in history if entry.type == "diagnosis.result"]
    assert [entry.run_id for entry in outcomes] == [created.run_id, second.run_id, third.run_id]
    assert [entry.result.status for entry in outcomes] == ["FAILED", "CANCELLED", "INTERRUPTED"]
    assert all(entry.result.report_state == "UNAVAILABLE" and entry.result.case_id is None for entry in outcomes)
    assert outcomes[0].result.failure.code == "AGENT_EXECUTION_FAILED"
    assert outcomes[1].result.failure is None
    assert outcomes[2].result.failure.code == "AGENT_INTERRUPTED"


def test_attachment_upload_is_conversation_scoped_and_import_is_run_scoped(store):
    created = store.create_conversation("create")
    cid, rid = created.conversation_id, created.run_id
    store.request_stop(cid, "stop", rid)
    upload = store.reserve_attachment(cid, "upload", "日志.txt", "text/plain", 10, "a" * 64)
    store.complete_attachment(upload.attachment_id)
    assert store.get_run(cid)["run_id"] == rid
    with pytest.raises(AgentStoreError):
        store.bind_attachment(upload.attachment_id, CASE_ID, run_id=rid)
    store.finish_stop(cid, rid)
    newer = store.submit_message(cid, "new", "第二轮", [upload.attachment_id])
    store.bind_attachment(upload.attachment_id, CASE_ID, run_id=newer.run_id)
    _stop(store, cid, newer.run_id)
    third = store.submit_message(cid, "third", "第三轮显式复用附件", [upload.attachment_id])
    store.bind_attachment(upload.attachment_id, JOB_ID, run_id=third.run_id)
    assert store.get_attachment_import(upload.attachment_id, newer.run_id) == CASE_ID
    assert store.get_attachment_import(upload.attachment_id, third.run_id) == JOB_ID
    assert store.get_attachment_import(upload.attachment_id, rid) is None


def test_delete_is_immediate_authorized_durable_and_cannot_resurrect(store):
    owner = "a" * 64
    created = store.create_conversation("create", owner_key=owner)
    cid = created.conversation_id
    attachment = store.reserve_attachment(cid, "upload", "log", "text/plain", 1, "a" * 64)
    store.submit_message(cid, "message", "问题")
    store.record_dispatch(cid, "intake", {"operation": "INTAKE", "workspace_id": JOB_ID})
    store.bind_case(cid, CASE_ID)
    with pytest.raises(AgentStoreError):
        store.request_delete(cid, owner_key="b" * 64)
    assert store.request_delete(cid, owner_key=owner).status == "DELETING"
    assert store.request_delete(cid, owner_key=owner).status == "DELETING"
    assert store.list_conversations(owner).items == []
    for read in (lambda: store.get_status(cid), lambda: store.get_attachment(attachment.attachment_id),
                 lambda: store.create_conversation("create", owner_key=owner)):
        with pytest.raises(AgentStoreError):
            read()
    context = store.cleanup_context(cid)
    assert context["case_ids"] == [CASE_ID]
    assert context["workspace_ids"] == [JOB_ID]
    assert context["pending_stop"] is True
    assert store.conversation_for_case(CASE_ID) == cid
    assert store.claim_cleanup()["conversation_id"] == cid
    store.update_cleanup_manifest(cid, {**context, "cleanup_id": "receipt", "paths": ["frozen/path"]})
    store.fail_cleanup(cid, "BUSY")
    assert store.claim_cleanup()["manifest"]["paths"] == ["frozen/path"]
    store.finish_stop(cid, created.run_id)
    store.finish_cleanup(cid)
    assert store.request_delete(cid, owner_key=owner).status == "DELETED"
    assert store.conversation_for_case(CASE_ID) is None
    with pytest.raises(AgentStoreError):
        store.create_conversation("create", owner_key=owner)
    with store.repository.database_read() as db:
        assert db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone() == ("{}",)
        assert db.execute("SELECT request_id FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()[0].startswith("deleted:")
        assert db.execute("SELECT count(*) FROM agent_stop_requests WHERE conversation_id=?", (cid,)).fetchone()[0] == 0


def test_cleanup_busy_item_does_not_starve_later_deletions(store):
    first = store.create_conversation("first")
    second = store.create_conversation("second")
    store.request_delete(first.conversation_id)
    store.request_delete(second.conversation_id)
    assert store.claim_cleanup()["conversation_id"] == first.conversation_id
    store.fail_cleanup(first.conversation_id, "BUSY")
    assert store.claim_cleanup()["conversation_id"] == second.conversation_id


def test_delete_stops_failed_historical_runs_whose_cases_are_still_waiting(store):
    created = store.create_conversation("create")
    cid = created.conversation_id
    run_ids = []
    for index, case_id in enumerate((CASE_ID, JOB_ID)):
        message = store.submit_message(cid, "message:" + str(index), "需要诊断的问题")
        run_ids.append(message.run_id)
        case = SimpleNamespace(case_id=case_id, active_job_id=None,
            status=SimpleNamespace(value="WAITING_INPUT"), archive_status="NOT_REQUIRED", case_revision=1,
            diagnosis_state=SimpleNamespace(pending_requirements=[]))
        with store.repository.database_transaction() as db:
            store._project_case(db, store._load(db, cid), SimpleNamespace(case=case))
        store.fail_conversation(cid, "INTAKE_OUTPUT_INVALID", phase="INTAKE")
        assert store.get_run(cid)["status"] == "FAILED"
        assert store.get_run(cid)["case_status"] == "WAITING_INPUT"
    store.request_delete(cid)
    pending = store.pending_stops()
    assert {item["run_id"] for item in pending} == set(run_ids)
    assert {item["case_id"] for item in pending} == {CASE_ID, JOB_ID}
    assert all(item["status"] == "CANCELLING" and item["deleted"] for item in pending)
    assert store.cleanup_context(cid)["pending_stop"] is True
    for run_id in run_ids:
        store.finish_stop(cid, run_id)
    assert store.cleanup_context(cid)["pending_stop"] is False
    assert store.pending_stops() == []


def test_v1_upgrade_preserves_raw_history_and_projects_v2_on_read(store):
    created = store.create_conversation("legacy")
    cid = created.conversation_id
    store.submit_message(cid, "message", "保留原始字节")
    store.append_progress(cid, "INTAKE", dedupe_key="custom-legacy-key")
    with store.repository.database_transaction() as db:
        for table, payload_column in (("agent_messages", "body"), ("agent_events", "body")):
            rows = db.execute(f"SELECT rowid,{payload_column} FROM {table} WHERE conversation_id=?", (cid,)).fetchall()
            for rowid, raw in rows:
                payload = json.loads(raw)
                payload.pop("run_id", None)
                if table == "agent_events":
                    payload["schema_version"] = 1
                    payload["data"].pop("run_id", None)
                db.execute(f"UPDATE {table} SET {payload_column}=?,run_id=NULL WHERE rowid=?", (json.dumps(payload, ensure_ascii=True, indent=2), rowid))
        before = {table: db.execute(f"SELECT body FROM {table} WHERE conversation_id=? ORDER BY rowid", (cid,)).fetchall()
                  for table in ("agent_messages", "agent_events")}
        receipts = db.execute("SELECT receipt FROM agent_messages WHERE conversation_id=?", (cid,)).fetchall()
        original_request = db.execute("SELECT request_id FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
        db.execute("DELETE FROM agent_create_keys WHERE conversation_id=?", (cid,))
        db.execute("DELETE FROM agent_conversation_runs WHERE conversation_id=?", (cid,))
        db.execute("UPDATE agent_conversations SET current_run_id=NULL WHERE conversation_id=?", (cid,))
        db.execute("DELETE FROM metadata WHERE key='agent_storage_version'")
    with pytest.raises(AgentStoreError) as blocked:
        AgentStore(store.repository)
    assert blocked.value.code == "STATE_SCHEMA_UNSUPPORTED"
    with store.repository.database_transaction() as db:
        upgrade_agent_storage_v2(db, {cid: "a" * 64})
        for table in before:
            assert db.execute(f"SELECT body FROM {table} WHERE conversation_id=? ORDER BY rowid", (cid,)).fetchall() == before[table]
        assert db.execute("SELECT receipt FROM agent_messages WHERE conversation_id=?", (cid,)).fetchall() == receipts
        assert db.execute("SELECT request_id FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone() == original_request
    upgraded = AgentStore(store.repository, runtime_epoch="first")
    events = upgraded.list_events(cid)
    assert all(event.schema_version == 2 and event.run_id == cid for event in events)
    assert events[0].data["run_id"] == cid
    assert upgraded.read_conversation(cid).progress.stage == "INTAKE"
    assert upgraded.get_conversation(cid).messages[0].run_id == cid
    assert upgraded.list_conversations("a" * 64).items[0].conversation_id == cid
    replay = upgraded.create_conversation("legacy", owner_key="a" * 64)
    assert replay.conversation_id == cid and replay.run_id == cid
    assert upgraded.create_conversation("legacy", owner_key="b" * 64).conversation_id != cid
    with store.repository.database_read() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT body FROM agent_events WHERE conversation_id=? AND run_id=? AND json_extract(body, '$.type')='agent.progress' ORDER BY sequence DESC LIMIT 1", (cid, cid)).fetchall()
    assert any("agent_events_run_progress" in row[-1] for row in plan)
    upgraded.request_delete(cid, owner_key="a" * 64)
    upgraded.finish_stop(cid, cid)
    upgraded.finish_cleanup(cid)
    for owner in (None, "a" * 64):
        with pytest.raises(AgentStoreError) as deleted:
            upgraded.create_conversation("legacy", owner_key=owner)
        assert deleted.value.status_code == 404


def test_upgrade_does_not_assign_unknown_legacy_creation_requests(store):
    old = store.create_conversation("unknown-legacy")
    cid = old.conversation_id
    with store.repository.database_transaction() as db:
        db.execute("DELETE FROM agent_conversation_runs WHERE conversation_id=?", (cid,))
        db.execute("DELETE FROM agent_create_keys WHERE conversation_id=?", (cid,))
        db.execute("UPDATE agent_conversations SET current_run_id=NULL WHERE conversation_id=?", (cid,))
        upgrade_agent_storage_v2(db)
    assert store.create_conversation("unknown-legacy").conversation_id == cid
    newly_owned = store.create_conversation("unknown-legacy", owner_key="a" * 64)
    assert newly_owned.conversation_id != cid
    with pytest.raises(AgentStoreError):
        store.require_owner(cid, "a" * 64)


def test_public_event_requires_run_and_legacy_is_only_a_storage_projection():
    with pytest.raises(ValueError):
        AgentEvent(sequence=1, conversation_id=CASE_ID, created_at="now", type="run.started", data={"ordinal": 1})
    event = AgentEvent(sequence=1, conversation_id=CASE_ID, run_id=JOB_ID, created_at="now", type="run.started", data={"ordinal": 1})
    assert event.schema_version == 2
