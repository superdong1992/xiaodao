from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import ApplicationPortError, ArtifactKind, StateFile, canonical_json_bytes
from problem_locator.entrypoints import data_upgrade as upgrade
from problem_locator.storage.platform import FileInstanceLock
from tests.deterministic.unit.storage.test_state_repository import CASE_ID, JOB_ID, _finish, _open, _populate


SOURCE_REVISION = "v11-contract-r1"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000088"
REPORT_BYTES = b'{"contract_revision":"v11-contract-r1","historical_report":"preserve exactly"}\r\n'


@pytest.fixture
def migration_host(monkeypatch):
    if sys.platform != "linux":
        # Exercise storage/data checks on development hosts. Native flock and
        # non-replacing rename have dedicated Linux tests and the real Test Flow.
        monkeypatch.setattr(upgrade, "_require_linux", lambda: None)

        @contextmanager
        def existing_lock(root):
            (root / ".instance.lock").read_bytes()
            yield lambda: None

        def publish(source, destination):
            if destination.exists():
                raise FileExistsError(destination)
            os.rename(source, destination)

        monkeypatch.setattr(upgrade, "_source_lock", existing_lock)
        monkeypatch.setattr(upgrade, "_publish_directory", publish)


def _all_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _rows(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if row[0] in upgrade._CORE_TABLES | upgrade._AGENT_TABLES]
    lengths = {"agent_conversations": 7, "agent_messages": 6, "agent_events": 4, "agent_dispatches": 6}
    return {table: [tuple(row[:lengths.get(table, len(row))]) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        if not (table == "metadata" and row[0] == "agent_storage_version")] for table in tables}


def _legacy_agent_schema(db):
    """Build actual old Agent v1 fixtures, never downgrade a production root."""
    keep = {"agent_messages_conversation", "agent_events_progress", "agent_dispatches_conversation_status"}
    for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index'").fetchall():
        if sql is not None and name.startswith("agent_") and name not in keep:
            db.execute(f'DROP INDEX "{name}"')
    for table in upgrade._AGENT_V2_TABLES:
        db.execute(f'DROP TABLE IF EXISTS "{table}"')
    lengths = {"agent_conversations": 7, "agent_messages": 6, "agent_events": 4, "agent_dispatches": 6}
    for table, length in lengths.items():
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
        for column in reversed(columns[length:]):
            db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    for table, columns in {"agent_conversations": ("body",), "agent_messages": ("body", "receipt"), "agent_events": ("body",)}.items():
        for column in columns:
            for rowid, raw in db.execute(f'SELECT rowid,"{column}" FROM "{table}"').fetchall():
                value = json.loads(raw)
                for key in ("run_id", "ordinal", "stop_requested"):
                    value.pop(key, None)
                if value.get("status") == "CANCELLED":
                    value["status"] = "FAILED"
                if table == "agent_events" and isinstance(value.get("data"), dict):
                    value["schema_version"] = 1
                    value["data"].pop("run_id", None)
                    if value["data"].get("status") == "CANCELLED" and value.get("type") == "conversation.completed":
                        value["data"]["status"] = "FAILED"
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (json.dumps(value, ensure_ascii=False), rowid))
    db.execute("DELETE FROM metadata WHERE key='agent_storage_version'")
    db.execute("UPDATE agent_conversations SET status='FAILED' WHERE status='CANCELLED'")


@pytest.fixture
def legacy_root(tmp_path):
    root = tmp_path / "source"
    repository = _open(root)
    (root / ".instance.lock").write_bytes(b"existing lock\n")
    _populate(repository)
    store = AgentStore(repository, runtime_epoch="historical-epoch")
    conversation = store.create_conversation("historical-conversation").conversation_id
    store.bind_case(conversation, CASE_ID)
    content = "原始附件\r\n".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    attachment = store.reserve_attachment(conversation, "upload-1", "trace.log", "text/plain", len(content), digest)
    path = root / "resources/conversations" / attachment.attachment_id / "payload"
    path.parent.mkdir()
    path.write_bytes(content)
    path.chmod(0o444)
    store.complete_attachment(attachment.attachment_id, str(path))
    receipt = store.submit_message(conversation, "message-1", "历史问题", [attachment.attachment_id])
    store.record_dispatch(conversation, "dispatch-1", {"operation": "SubmitSupplement",
        "command": {"idempotency_key": "historical-supplement"}, "old_revision": SOURCE_REVISION})
    store.set_message_status(conversation, receipt.message_id, "PROCESSING")
    store.begin_adoption(conversation, receipt.message_id, "dispatch-1")
    store.complete_dispatch("dispatch-1", {"accepted": True})
    store.finish_adoption(conversation, receipt.message_id, True)
    _finish(repository)

    raw = repository._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (CASE_ID,)).fetchone()[0]
    state = json.loads(raw)
    resource_key = f"resources/cases/{CASE_ID}/artifacts/{ARTIFACT_ID}/payload"
    report = root / resource_key
    report.parent.mkdir(parents=True)
    report.write_bytes(REPORT_BYTES)
    report.chmod(0o444)
    artifact = {"artifact_id": ARTIFACT_ID, "case_id": CASE_ID, "kind": "DIAGNOSTIC_EXPORT",
        "name": "historical-report.json", "content_type": "application/json", "resource_kind": "FILE",
        "size": len(REPORT_BYTES), "sha256": hashlib.sha256(REPORT_BYTES).hexdigest(),
        "storage_key": resource_key, "metadata": {"schema_version": 1, "format_id": "historical-report",
            "description": "Historical report bytes"}, "created_by_job_id": JOB_ID,
        "created_at": state["created_at"]}
    state["cases"][CASE_ID]["artifacts"][ARTIFACT_ID] = artifact
    StateFile.model_validate(state)
    repository._db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?", (canonical_json_bytes(state), CASE_ID))
    repository._db.execute("INSERT INTO object_index VALUES (?,?)", (ARTIFACT_ID, CASE_ID))
    repository._db.execute("INSERT INTO resource_index VALUES (?,?)", (resource_key, CASE_ID))
    # r1 had no optional ConversationView failure projection. Its original
    # stored body and historical event payloads must remain unchanged.
    body = json.loads(repository._db.execute("SELECT body FROM agent_conversations").fetchone()[0])
    body.pop("failure", None)
    repository._db.execute("UPDATE agent_conversations SET body=?", (json.dumps(body, ensure_ascii=False),))
    repository.close()

    # Keep the r1 snapshot exclusively in an uncheckpointed WAL. Copying just
    # completed.sqlite3 would see r2 and must not qualify as a migration.
    db = sqlite3.connect(root / "completed.sqlite3", isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    _legacy_agent_schema(db)
    state["contract_revision"] = SOURCE_REVISION
    db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?", (canonical_json_bytes(state), CASE_ID))
    marker = json.loads((root / "data-format.json").read_bytes())
    marker["contract_revision"] = SOURCE_REVISION
    marker.pop("agent_storage_version", None)
    (root / "data-format.json").write_bytes(canonical_json_bytes(marker))
    yield root, db, conversation, attachment.attachment_id, resource_key
    db.close()


def test_plan_only_never_opens_source_sqlite_or_creates_target(legacy_root, tmp_path, migration_host, monkeypatch):
    source, _, _, _, _ = legacy_root
    before = _all_files(source)
    target = tmp_path / "planned"

    def forbidden(*args, **kwargs):
        raise AssertionError("plan-only must not open SQLite")

    monkeypatch.setattr(upgrade.sqlite3, "connect", forbidden)
    result = upgrade.upgrade_data_root(source, target)
    assert result["status"] == "PLANNED"
    assert result["source_wal_present"]
    assert result["database_and_resource_validation"] == "PENDING_EXECUTE"
    assert not target.exists() and not list(tmp_path.glob(".planned.upgrade-*"))
    assert _all_files(source) == before


def test_current_r2_agent_v1_upgrade_preserves_snapshot_and_imports_explicit_owner(legacy_root, tmp_path, migration_host):
    source, db, conversation, _, _ = legacy_root
    native_create_key = db.execute("SELECT request_id FROM agent_conversations WHERE conversation_id=?", (conversation,)).fetchone()[0]
    raw = json.loads(db.execute("SELECT snapshot FROM completed_cases").fetchone()[0])
    raw["contract_revision"] = "v11-contract-r2"
    original = json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8")
    db.execute("UPDATE completed_cases SET snapshot=?", (original,))
    (source / "data-format.json").write_bytes(upgrade._marker("v11-contract-r2"))
    old_receipt = b'{"previous_upgrade":"keep bytes"}\r\n'
    (source / "data-upgrade.receipt.json").write_bytes(old_receipt)
    ownership = tmp_path / "owners.json"
    ownership.write_text(json.dumps({conversation: "a" * 64}), encoding="utf-8")
    workspace_id = "90000000-0000-0000-0000-000000000001"
    runtime = source / "tmp" / "workspaces" / workspace_id / "runtime"
    runtime.mkdir(parents=True)
    content = b'{"historical":"intake"}'
    for name in ("intake-response-original.txt", "intake-response-effective.json"):
        (runtime / name).write_bytes(content)
    (runtime / "intake-response-extraction.json").write_text(json.dumps({"conversation_id": conversation,
        "diagnostic_id": workspace_id, "phase": "INTAKE", "raw_size_bytes": len(content),
        "effective_size_bytes": len(content), "raw_sha256": hashlib.sha256(content).hexdigest(),
        "effective_sha256": hashlib.sha256(content).hexdigest()}), encoding="utf-8")
    unknown_workspace = "90000000-0000-0000-0000-000000000002"
    unverified = source / "tmp" / "workspaces" / unknown_workspace / "runtime"
    unverified.mkdir(parents=True)
    (unverified / "intake-response-original.txt").write_bytes(content)
    (unverified / "intake-response-effective.json").write_bytes(content)
    (unverified / "intake-response-extraction.json").write_text(json.dumps({"conversation_id": conversation,
        "diagnostic_id": unknown_workspace, "phase": "INTAKE", "raw_size_bytes": len(content),
        "effective_size_bytes": len(content), "raw_sha256": "0" * 64,
        "effective_sha256": hashlib.sha256(content).hexdigest()}), encoding="utf-8")
    before = _all_files(source)
    target = tmp_path / "agent-v2"
    result = upgrade.upgrade_data_root(source, target, execute=True, ownership_map=ownership)
    assert result["source_revision"] == "v11-contract-r2"
    assert result["target_agent_storage_version"] == 2
    assert result["linked_legacy_intake_workspaces"] == 1
    assert (result["assigned_conversations"], result["unassigned_conversations"]) == (1, 0)
    assert result["ownership_map_sha256"] == hashlib.sha256(ownership.read_bytes()).hexdigest()
    assert "a" * 64 not in json.dumps(result)
    assert _all_files(source) == before
    assert (target / "data-upgrade.receipt.json").read_bytes() == old_receipt
    assert (target / unverified.relative_to(source) / "intake-response-original.txt").read_bytes() == content
    with sqlite3.connect(target / "completed.sqlite3") as copied:
        assert copied.execute("SELECT snapshot FROM completed_cases").fetchone()[0] == original
        assert copied.execute("SELECT owner_key,current_run_id FROM agent_conversations").fetchone() == ("a" * 64, conversation)
        assert copied.execute("SELECT value FROM metadata WHERE key='agent_storage_version'").fetchone()[0] == "2"
        assert json.loads(copied.execute("SELECT body FROM agent_conversation_runs").fetchone()[0])["legacy_workspace_ids"] == [workspace_id]
        assert copied.execute("SELECT request_id FROM agent_conversations WHERE conversation_id=?", (conversation,)).fetchone()[0] == native_create_key
    reopened = _open(target)
    try:
        reader = AgentStore(reopened, runtime_epoch="migrated-create-replay")
        replay = reader.create_conversation(native_create_key, owner_key="a" * 64)
        assert replay.conversation_id == replay.run_id == conversation
        assert reader.create_conversation(native_create_key, owner_key="b" * 64).conversation_id != conversation
        reader.request_delete(conversation, owner_key="a" * 64)
        with pytest.raises(AgentStoreError) as deleting:
            reader.create_conversation(native_create_key, owner_key="a" * 64)
        assert deleting.value.status_code == 404
        # The metadata cleanup acknowledgement must retain every create alias.
        reader.finish_cleanup(conversation)
        with pytest.raises(AgentStoreError) as deleted:
            reader.create_conversation(native_create_key, owner_key="a" * 64)
        assert deleted.value.status_code == 404
    finally:
        reopened.close()
    assert _all_files(source) == before
    assert json.loads((target / "data-format.json").read_bytes())["agent_storage_version"] == 2


@pytest.mark.parametrize("mapping", ["duplicate", "bad-owner", "unknown-conversation"])
def test_invalid_owner_mapping_never_changes_source(legacy_root, tmp_path, migration_host, mapping):
    source, _, conversation, _, _ = legacy_root
    ownership = tmp_path / "owners.json"
    if mapping == "duplicate":
        raw = '{"' + conversation + '":"' + "a" * 64 + '","' + conversation + '":"' + "b" * 64 + '"}'
    else:
        raw = json.dumps({conversation if mapping == "bad-owner" else ARTIFACT_ID:
            "user-alice" if mapping == "bad-owner" else "a" * 64})
    ownership.write_text(raw, encoding="utf-8")
    before = _all_files(source)
    target = tmp_path / "bad-owner"
    with pytest.raises(upgrade.DataUpgradeError) as caught:
        upgrade.upgrade_data_root(source, target, execute=True, ownership_map=ownership)
    assert caught.value.code == "OWNERSHIP_INVALID"
    assert _all_files(source) == before and not target.exists()


def test_upgrade_preserves_wal_history_and_artifacts_and_only_maps_allowed_fields(legacy_root, tmp_path, migration_host, monkeypatch):
    source, source_db, conversation, attachment_id, resource_key = legacy_root
    target = tmp_path / "upgraded"
    rows = _rows(source_db)
    files = _all_files(source)
    real_connect = sqlite3.connect

    def target_only(path, *args, **kwargs):
        assert source not in Path(path).parents
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(upgrade.sqlite3, "connect", target_only)
    result = upgrade.upgrade_data_root(source, target, execute=True)
    assert result["status"] == "UPGRADED"
    assert result["completed_cases"] == result["conversations"] == result["verified_resources"] == 1
    assert result["mapped_attachment_paths"] == 1
    assert result["old_exports_and_replay_supported"] is False
    assert _all_files(source) == files
    assert (target / resource_key).read_bytes() == REPORT_BYTES
    assert json.loads((target / "data-format.json").read_bytes())["contract_revision"] == "v11-contract-r2"
    assert json.loads((target / upgrade.RECEIPT_FILENAME).read_bytes()) == result
    assert not (target / "data-format.json.tmp").exists()
    with real_connect(target / "completed.sqlite3") as db:
        after = _rows(db)
        assert db.execute("SELECT owner_key,current_run_id FROM agent_conversations").fetchone() == (None, conversation)
    for table in rows.keys() - {"completed_cases", "agent_attachments"}:
        assert after[table] == rows[table]
    old_snapshot, new_snapshot = json.loads(rows["completed_cases"][0][1]), json.loads(after["completed_cases"][0][1])
    assert old_snapshot.pop("contract_revision") == SOURCE_REVISION
    assert new_snapshot.pop("contract_revision") == "v11-contract-r2"
    assert old_snapshot == new_snapshot
    assert after["agent_attachments"][0][:-1] == rows["agent_attachments"][0][:-1]
    assert after["agent_attachments"][0][-1] == str(target / "resources/conversations" / attachment_id / "payload")
    # This is post-publication acceptance, never part of offline migration.
    reopened = _open(target)
    try:
        website = AgentStore(reopened, runtime_epoch="upgraded-epoch")
        website.recover()
        assert reopened.read_case(CASE_ID).artifacts[ARTIFACT_ID].sha256 == hashlib.sha256(REPORT_BYTES).hexdigest()
        assert website.get_conversation(conversation).status == "FAILED"
        assert _rows(reopened._db) == after
    finally:
        reopened.close()


def test_normal_restart_interrupted_history_preserves_orphan_case_and_job_ids(tmp_path, migration_host):
    source, target = tmp_path / "interrupted", tmp_path / "retained"
    repository = _open(source)
    (source / ".instance.lock").write_bytes(b"existing lock\n")
    _populate(repository)
    website = AgentStore(repository, runtime_epoch="before-restart")
    cid = website.create_conversation("interrupted-conversation").conversation_id
    website.submit_message(cid, "message", "重启前的原始问题")
    website.bind_case(cid, CASE_ID)
    website.record_dispatch(cid, "dispatch", {"operation": "intake"})
    website.append_case_progress(CASE_ID, JOB_ID, "DIAGNOSE")
    repository.close()
    recovered = _open(source)
    try:
        after_restart = AgentStore(recovered, runtime_epoch="after-restart")
        after_restart.recover()
        assert after_restart.get_conversation(cid).status == "INTERRUPTED"
        assert recovered._db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0
        _legacy_agent_schema(recovered._db)
        before_rows = _rows(recovered._db)
    finally:
        recovered.close()
    marker = json.loads((source / "data-format.json").read_bytes())
    marker["contract_revision"] = SOURCE_REVISION
    marker.pop("agent_storage_version", None)
    (source / "data-format.json").write_bytes(canonical_json_bytes(marker))
    before_files = _all_files(source)
    upgrade.upgrade_data_root(source, target, execute=True)
    assert _all_files(source) == before_files
    migrated = _open(target)
    try:
        reader = AgentStore(migrated, runtime_epoch="after-upgrade")
        reader.recover()
        view = reader.get_conversation(cid)
        assert (view.status, view.case_id) == ("INTERRUPTED", CASE_ID)
        assert any(event.job_id == JOB_ID for event in reader.list_events(cid))
        assert _rows(migrated._db) == before_rows
    finally:
        migrated.close()


@pytest.mark.parametrize("damage", [None, "source-artifact", "report-type", "plan"])
def test_completed_report_ready_archive_and_website_history_survive_upgrade(tmp_path, migration_host, damage):
    from tests.deterministic.integration.test_async_archive import create_pending_archive

    stack, case_id = create_pending_archive(tmp_path / "archive")
    try:
        website = AgentStore(stack.repository, runtime_epoch="historical-report")
        cid = website.create_conversation("archived-conversation").conversation_id
        website.submit_message(cid, "historical-message", "保留已交付报告")
        website.bind_case(cid, case_id)
        assert stack.archive.run_once()
        aggregate = stack.repository.read_case(case_id)
        assert aggregate.case.archive_status == "READY"
        assert website.get_conversation(cid).status == "COMPLETED"
        reports = {item.kind: item for item in aggregate.artifacts.values()
            if item.kind in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}}
        assert len(reports) == 2
        report_bytes = {kind: (stack.data_root / item.storage_key).read_bytes() for kind, item in reports.items()}
        source = stack.data_root
    finally:
        stack.shutdown()
    (source / ".instance.lock").write_bytes(b"existing lock\n")
    with sqlite3.connect(source / "completed.sqlite3") as db:
        for selected, raw in db.execute("SELECT case_id,snapshot FROM completed_cases"):
            value = json.loads(raw)
            value["contract_revision"] = SOURCE_REVISION
            db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?", (canonical_json_bytes(value), selected))
        if damage is not None:
            payload = json.loads(db.execute("SELECT payload FROM archive_tasks").fetchone()[0])
            if damage == "source-artifact":
                payload["source_job_id"] = reports[ArtifactKind.USER_RESULT].artifact_id
            elif damage == "report-type":
                payload["report_artifact_id"] = reports[ArtifactKind.USER_RESULT_ARCHIVE].artifact_id
            else:
                payload["plan"]["logs"][0]["sha256"] = "0" * 64
            db.execute("UPDATE archive_tasks SET payload=?", (canonical_json_bytes(payload),))
        _legacy_agent_schema(db)
        db.commit()
        before_rows = _rows(db)
    marker = json.loads((source / "data-format.json").read_bytes())
    marker["contract_revision"] = SOURCE_REVISION
    marker.pop("agent_storage_version", None)
    (source / "data-format.json").write_bytes(canonical_json_bytes(marker))
    before_files = _all_files(source)
    target = source.with_name("archived-upgraded")
    if damage is not None:
        with pytest.raises(upgrade.DataUpgradeError):
            upgrade.upgrade_data_root(source, target, execute=True)
        assert _all_files(source) == before_files and not target.exists()
        return
    upgrade.upgrade_data_root(source, target, execute=True)
    assert _all_files(source) == before_files
    reopened = _open(target)
    try:
        reader = AgentStore(reopened, runtime_epoch="upgraded-report")
        reader.recover()
        retained = reopened.read_case(case_id)
        assert retained.case.archive_status == "READY"
        assert reader.get_conversation(cid).status == "COMPLETED"
        after_rows = _rows(reopened._db)
        for table in before_rows.keys() - {"completed_cases"}:
            assert after_rows[table] == before_rows[table]
        for kind, artifact in reports.items():
            assert retained.artifacts[artifact.artifact_id] == artifact
            assert (target / artifact.storage_key).read_bytes() == report_bytes[kind]
        archive = reports[ArtifactKind.USER_RESULT_ARCHIVE]
        with zipfile.ZipFile(target / archive.storage_key) as zipped:
            assert zipped.testzip() is None
            for source_file in reports[ArtifactKind.USER_RESULT].metadata.archive_plan.logs:
                data = zipped.read(source_file.archive_name)
                assert (len(data), hashlib.sha256(data).hexdigest()) == (source_file.size, source_file.sha256)
    finally:
        reopened.close()


@pytest.mark.parametrize("damage", ["snapshot", "index", "resource", "attachment-path", "events", "database"])
def test_invalid_history_never_changes_source_or_publishes_target(legacy_root, tmp_path, migration_host, damage):
    source, db, _, _, resource_key = legacy_root
    if damage == "snapshot":
        db.execute("UPDATE completed_cases SET snapshot=?", (b"{}",))
    elif damage == "index":
        db.execute("DELETE FROM object_index WHERE object_id=?", (ARTIFACT_ID,))
    elif damage == "resource":
        path = source / resource_key
        path.chmod(0o600)
        path.write_bytes(b"changed report")
        path.chmod(0o444)
    elif damage == "attachment-path":
        db.execute("UPDATE agent_attachments SET storage_path=?", (str(tmp_path / "foreign/payload"),))
    elif damage == "events":
        db.execute("DELETE FROM agent_events WHERE sequence=1")
    else:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # The live fixture connection is idle; corrupt only fixture-owned data.
        (source / "completed.sqlite3").write_bytes(b"not a database")
    before = _all_files(source)
    target = tmp_path / ("bad-" + damage)
    with pytest.raises(upgrade.DataUpgradeError):
        upgrade.upgrade_data_root(source, target, execute=True)
    assert _all_files(source) == before
    assert not target.exists()


@pytest.mark.parametrize("pending", ["conversation", "archive"])
def test_upgrade_requires_all_work_to_be_drained(legacy_root, tmp_path, migration_host, pending):
    source, db, _, _, _ = legacy_root
    if pending == "conversation":
        body = json.loads(db.execute("SELECT body FROM agent_conversations").fetchone()[0])
        body["status"] = "RUNNING"
        db.execute("UPDATE agent_conversations SET status='RUNNING',body=?", (json.dumps(body),))
    else:
        db.execute("INSERT INTO archive_tasks VALUES (?,'PENDING',?,NULL)", (CASE_ID, b"{}"))
    before = _all_files(source)
    target = tmp_path / "pending"
    with pytest.raises(upgrade.DataUpgradeError) as caught:
        upgrade.upgrade_data_root(source, target, execute=True)
    assert caught.value.code == "SOURCE_NOT_DRAINED"
    assert _all_files(source) == before and not target.exists()


@pytest.mark.parametrize("history", ["idle", "failed-orphan", "pending-dispatch", "reserved-upload", "uploading"])
def test_idle_and_closed_history_is_retained_without_reactivating_work(tmp_path, migration_host, history):
    source, target = tmp_path / "history", tmp_path / "retained"
    repository = _open(source)
    (source / ".instance.lock").write_bytes(b"existing lock\n")
    website = AgentStore(repository, runtime_epoch="original-epoch")
    cid = website.create_conversation("original-request").conversation_id
    attachment = None
    if history != "idle":
        website.submit_message(cid, "message", "保留失败历史")
        if history == "failed-orphan":
            _populate(repository)
            website.bind_case(cid, CASE_ID)
            website.append_case_progress(CASE_ID, JOB_ID, "DIAGNOSE")
        elif history == "pending-dispatch":
            website.record_dispatch(cid, "dispatch", {"operation": "intake"})
        else:
            attachment = website.reserve_attachment(cid, "upload", "trace.log", "text/plain", 3,
                hashlib.sha256(b"log").hexdigest())
            if history == "uploading":
                website.set_attachment_status(attachment.attachment_id, "UPLOADING")
        website.fail_conversation(cid)
    repository.close()
    restarted = _open(source)
    try:
        recovered = AgentStore(restarted, runtime_epoch="restarted-epoch")
        recovered.recover()
        assert recovered.get_conversation(cid).status == ("INTAKE" if history == "idle" else "FAILED")
        if history == "failed-orphan":
            assert restarted._db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0
        if history == "pending-dispatch":
            assert restarted._db.execute("SELECT status FROM agent_dispatches").fetchone()[0] == "PENDING"
        if attachment is not None:
            assert recovered.get_attachment(attachment.attachment_id).status == (
                "UPLOADING" if history == "uploading" else "RESERVED")
        _legacy_agent_schema(restarted._db)
        before_rows = _rows(restarted._db)
    finally:
        restarted.close()
    marker = json.loads((source / "data-format.json").read_bytes())
    marker["contract_revision"] = SOURCE_REVISION
    marker.pop("agent_storage_version", None)
    (source / "data-format.json").write_bytes(canonical_json_bytes(marker))
    before_files = _all_files(source)
    upgrade.upgrade_data_root(source, target, execute=True)
    assert _all_files(source) == before_files
    reopened = _open(target)
    try:
        reader = AgentStore(reopened, runtime_epoch="upgraded-epoch")
        assert _rows(reopened._db) == before_rows
        reader.recover()
        after_rows = _rows(reopened._db)
        if history == "idle":
            # Normal startup claims a still-empty conversation for its new
            # process epoch; migration itself preserved the original row.
            before_rows["agent_conversations"] = [
                (*row[:2], "upgraded-epoch", *row[3:]) for row in before_rows["agent_conversations"]]
        assert after_rows == before_rows
        assert reader.get_conversation(cid).status == ("INTAKE" if history == "idle" else "FAILED")
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["before-publish", "after-publish"])
def test_incomplete_r2_staging_cannot_be_opened_as_data_root(legacy_root, tmp_path, migration_host, monkeypatch, failure):
    source, _, _, _, _ = legacy_root
    target = tmp_path / "incomplete"
    before = _all_files(source)
    if failure == "before-publish":
        def reject_publication(staging, destination):
            assert json.loads((staging / "data-format.json").read_bytes())["contract_revision"] == "v11-contract-r2"
            raise OSError("injected pre-publication failure")
        monkeypatch.setattr(upgrade, "_publish_directory", reject_publication)
    else:
        sync_directory = upgrade.PlatformFileSync.sync_directory
        failed = False

        def reject_final_sync(sync, directory):
            nonlocal failed
            if Path(directory) == target and not failed:
                failed = True
                assert not (target / "data-format.json.tmp").exists()
                raise OSError("injected final directory sync failure")
            return sync_directory(sync, directory)

        monkeypatch.setattr(upgrade.PlatformFileSync, "sync_directory", reject_final_sync)
    with pytest.raises(upgrade.DataUpgradeError) as caught:
        upgrade.upgrade_data_root(source, target, execute=True)
    staging = caught.value.staging_root
    assert staging.is_dir() and not target.exists() and _all_files(source) == before
    assert (staging / "data-format.json.tmp").is_file()
    with pytest.raises(ApplicationPortError):
        _open(staging)


def test_copy_failure_keeps_unpublished_evidence_and_source_untouched(legacy_root, tmp_path, migration_host, monkeypatch):
    source, _, _, _, _ = legacy_root
    before = _all_files(source)
    copy = upgrade._file

    def interrupted(path, destination=None):
        if destination is not None and path.name == "completed.sqlite3":
            raise OSError("injected copy failure")
        return copy(path, destination)

    monkeypatch.setattr(upgrade, "_file", interrupted)
    target = tmp_path / "copy-failed"
    with pytest.raises(upgrade.DataUpgradeError) as caught:
        upgrade.upgrade_data_root(source, target, execute=True)
    assert not target.exists() and _all_files(source) == before
    assert caught.value.staging_root.is_dir()


def test_missing_lock_is_never_created_and_existing_target_is_never_adopted(legacy_root, tmp_path, migration_host):
    source, _, _, _, _ = legacy_root
    (source / ".instance.lock").unlink()
    before = _all_files(source)
    with pytest.raises(upgrade.DataUpgradeError):
        upgrade.upgrade_data_root(source, tmp_path / "missing-lock", execute=True)
    assert _all_files(source) == before
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(upgrade.DataUpgradeError) as caught:
        upgrade.upgrade_data_root(source, target, execute=True)
    assert caught.value.code == "TARGET_EXISTS" and list(target.iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux flock and atomic directory publication")
def test_linux_upgrade_rejects_live_source_lock_and_concurrent_target(legacy_root, tmp_path, monkeypatch):
    source, _, _, _, _ = legacy_root
    before = _all_files(source)
    target = tmp_path / "linux-lock"
    with FileInstanceLock(source / ".instance.lock"):
        with pytest.raises(upgrade.DataUpgradeError) as caught:
            upgrade.upgrade_data_root(source, target, execute=True)
        assert caught.value.code == "SOURCE_LOCKED"
    assert not target.exists() and _all_files(source) == before
    publish = upgrade._publish_directory

    def competing(staging, destination):
        destination.mkdir()
        (destination / "other-owner").write_bytes(b"keep")
        publish(staging, destination)

    monkeypatch.setattr(upgrade, "_publish_directory", competing)
    with pytest.raises(upgrade.DataUpgradeError):
        upgrade.upgrade_data_root(source, target, execute=True)
    assert list(target.iterdir()) == [target / "other-owner"]
    assert _all_files(source) == before


@pytest.mark.skipif(sys.platform == "linux", reason="non-Linux CLI admission")
def test_upgrade_command_rejects_unsupported_platform(tmp_path, capsys):
    assert upgrade.main(["--source-root", str(tmp_path), "--target-root", str(tmp_path / "target"), "--plan-only"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "PLATFORM_UNSUPPORTED"
