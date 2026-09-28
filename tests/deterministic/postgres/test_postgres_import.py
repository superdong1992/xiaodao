"""Offline imports retain source bytes, SQLite WAL, resources and row order."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.contracts import ApplicationPortError, ArtifactKind, ErrorCode
from problem_locator.entrypoints import data_upgrade as safety
from problem_locator.entrypoints import postgres_import as migration
from problem_locator.followup.store import FollowupStore
from problem_locator.memory.store import MemoryStore
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.postgres_layout import validate_postgres_root
from problem_locator.storage.state_repository import CaseStateRepository
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FixedClock
from tests.deterministic.unit.storage.test_state_repository import _open
from tests.deterministic.integration.test_async_archive import create_pending_archive
from tests.deterministic.integration.test_report_followups import GenericV2Backend, _exercise, _install
from tests.deterministic.integration.test_website_agent import OWNER_KEY, _post, _preupload, website
from tests.postgres_helpers import postgres_database_url


@pytest.fixture
def migration_host(monkeypatch):
    if sys.platform != "linux":
        monkeypatch.setattr(safety, "_require_linux", lambda: None)

        @contextmanager
        def locked(root):
            (root / ".instance.lock").read_bytes()
            yield lambda: None

        def publish(source, target):
            if target.exists():
                raise FileExistsError(target)
            os.rename(source, target)

        monkeypatch.setattr(safety, "_source_lock", locked)
        monkeypatch.setattr(safety, "_publish_directory", publish)


@pytest.fixture
def sqlite_source(tmp_path):
    root = tmp_path / "source"
    repository = _open(root)
    (root / ".instance.lock").write_bytes(b"offline source lock\n")
    store = AgentStore(repository)
    MemoryStore(repository)
    FollowupStore(repository)
    conversation = store.create_conversation("history")
    content = "原始日志，不改写换行。\r\n".encode()
    attachment = store.reserve_attachment(conversation.conversation_id, "attachment", "trace.log",
        "text/plain", len(content), hashlib.sha256(content).hexdigest())
    resource = root / "resources" / "conversations" / attachment.attachment_id / "payload"
    resource.parent.mkdir()
    resource.write_bytes(content)
    resource.chmod(0o444)
    store.complete_attachment(attachment.attachment_id, str(resource))
    store.request_stop(conversation.conversation_id, "stop", conversation.run_id)
    store.finish_stop(conversation.conversation_id, conversation.run_id)
    repository.close()
    return root, attachment.attachment_id, content


def _manifest(root):
    return safety._inventory(root)


def _verify_table_proofs(repository, result):
    with repository.database_read() as db:
        for table, proof in result["tables"].items():
            selected = ",".join('"' + name + '"' for name in proof["columns"])
            assert migration._digest(db.execute(f'SELECT {selected} FROM "{table}"')) == proof["target"]


def test_postgres_import_plan_never_opens_sqlite_or_postgres_or_writes_target(sqlite_source, tmp_path, migration_host, monkeypatch):
    source, _, _ = sqlite_source
    before = _manifest(source)
    target = tmp_path / "target"

    def forbidden(*args, **kwargs):
        raise AssertionError("plan-only must not connect to a database")

    monkeypatch.setattr(migration.sqlite3, "connect", forbidden)
    monkeypatch.setattr(migration, "_empty_postgres_database", forbidden)
    result = migration.import_data_root(source, target, database_url_env="UNSET_IMPORT_TEST_URL")
    assert result["status"] == "PLANNED"
    assert result["database_and_resource_validation"] == "PENDING_EXECUTE"
    assert _manifest(source) == before
    assert not target.exists()
    assert not list(tmp_path.glob(".target.postgres-import-*"))


def test_postgres_import_rejects_existing_target_before_any_database_access(sqlite_source, tmp_path, migration_host, monkeypatch):
    source, _, _ = sqlite_source
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("已有数据", encoding="utf-8")
    monkeypatch.setattr(migration, "_empty_postgres_database", lambda *_: pytest.fail("database was contacted"))
    with pytest.raises(safety.DataUpgradeError, match="尚不存在"):
        migration.import_data_root(source, target, execute=True)
    assert marker.read_text("utf-8") == "已有数据"


def test_postgres_import_preserves_source_wal_json_resources_and_rebinds_only_attachment_root(
        sqlite_source, tmp_path, migration_host, postgres_database_url, monkeypatch):
    source, aid, content = sqlite_source
    target = tmp_path / "target"
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    # Keep a committed row solely in WAL, as a clean main-file copy would lose it.
    live_copy = sqlite3.connect(source / "completed.sqlite3")
    try:
        live_copy.execute("PRAGMA journal_mode=WAL")
        live_copy.execute("PRAGMA wal_autocheckpoint=0")
        live_copy.execute("INSERT INTO agent_deleted_requests(request_hash,conversation_id) VALUES (?,?)",
            ("wal-only-record", "00000000-0000-0000-0000-000000000099"))
        live_copy.commit()
        before = _manifest(source)
        result = migration.import_data_root(source, target, execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
        assert _manifest(source) == before
        assert result["status"] == "IMPORTED" and result["source_wal_present"] is True
        assert not (target / "completed.sqlite3").exists()
        assert not (target / "data-format.json.tmp").exists()
        assert (target / "resources" / "conversations" / aid / "payload").read_bytes() == content
        marker = validate_postgres_root(StorageLayout.at(target))
        assert marker["installation_id"] == result["installation_id"]
        repository = CaseStateRepository(target, StorageCoordinationLock(), FixedClock("2026-09-28T00:00:00.000Z"), DeterministicIdGenerator(),
            database_url=postgres_database_url)
        try:
            with repository.database_read() as db:
                assert db.execute("SELECT conversation_id FROM agent_deleted_requests WHERE request_hash=?",
                    ("wal-only-record",)).fetchone() == ("00000000-0000-0000-0000-000000000099",)
                assert db.execute("SELECT storage_path FROM agent_attachments WHERE attachment_id=?", (aid,)).fetchone()[0] == str(
                    target / "resources" / "conversations" / aid / "payload")
                for table, proof in result["tables"].items():
                    selected = ",".join('"' + name + '"' for name in proof["columns"])
                    assert migration._digest(db.execute(f'SELECT {selected} FROM "{table}"')) == proof["target"]
        finally:
            repository.close()
    finally:
        live_copy.close()


def test_postgres_import_rejects_nonempty_database_without_changing_it(
        sqlite_source, tmp_path, migration_host, postgres_database_url, monkeypatch):
    import psycopg
    source, _, _ = sqlite_source
    before = _manifest(source)
    with psycopg.connect(postgres_database_url, autocommit=True) as db:
        db.execute("CREATE TABLE keep_me (value TEXT)")
        db.execute("INSERT INTO keep_me VALUES ('existing')")
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    with pytest.raises(migration.PostgresImportError) as failure:
        migration.import_data_root(source, tmp_path / "target", execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert failure.value.code == "TARGET_DATABASE_NOT_EMPTY"
    with psycopg.connect(postgres_database_url, autocommit=True) as db:
        assert db.execute("SELECT value FROM keep_me").fetchall() == [("existing",)]
    assert _manifest(source) == before
    assert not (tmp_path / "target").exists()


def test_postgres_import_rejects_active_tasks_and_preserves_failed_evidence(
        sqlite_source, tmp_path, migration_host, postgres_database_url, monkeypatch):
    source, _, _ = sqlite_source
    with sqlite3.connect(source / "completed.sqlite3") as db:
        db.execute("INSERT INTO history_cleanup_jobs(cleanup_id,manifest) VALUES (?,?)",
            ("00000000-0000-0000-0000-000000000055", "{}"))
    before = _manifest(source)
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    with pytest.raises(migration.PostgresImportError) as failure:
        migration.import_data_root(source, tmp_path / "target", execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert failure.value.code == "SOURCE_NOT_DRAINED"
    assert failure.value.staging_root is not None
    assert (failure.value.staging_root / "source-database" / "completed.sqlite3").is_file()
    assert (failure.value.staging_root / "payload" / "data-format.json.tmp").is_file()
    assert _manifest(source) == before
    assert not (tmp_path / "target").exists()


def test_postgres_import_publish_failure_fences_database_and_keeps_source(
        sqlite_source, tmp_path, migration_host, postgres_database_url, monkeypatch):
    import psycopg
    source, _, _ = sqlite_source
    before = _manifest(source)
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)

    def fail_publish(*_):
        raise OSError("injected publication failure")

    monkeypatch.setattr(safety, "_publish_directory", fail_publish)
    with pytest.raises(migration.PostgresImportError) as failure:
        migration.import_data_root(source, tmp_path / "target", execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert failure.value.staging_root is not None
    with psycopg.connect(postgres_database_url) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='storage_backend'").fetchone()[0] == "postgresql-import-pending"
    assert _manifest(source) == before
    assert not (tmp_path / "target").exists()
    assert (failure.value.staging_root / "payload" / "data-format.json.tmp").exists()


def test_postgres_import_retains_completed_case_bytea_archive_payload_and_resources(
        tmp_path, migration_host, postgres_database_url, monkeypatch):
    stack, case_id = create_pending_archive(tmp_path / "archive-source")
    source = stack.data_root
    try:
        assert stack.archive.run_once()
        aggregate = stack.repository.read_case(case_id)
        assert aggregate.case.archive_status == "READY"
        expected_case = aggregate.model_dump_json()
        artifacts = [item for item in aggregate.artifacts.values()
            if item.kind in {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}]
        assert {item.kind for item in artifacts} == {ArtifactKind.USER_RESULT, ArtifactKind.USER_RESULT_ARCHIVE}
        expected_files = {item.storage_key: (source / item.storage_key).read_bytes() for item in artifacts}
        with stack.repository.database_read() as db:
            snapshot = db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (case_id,)).fetchone()[0]
            archive = db.execute("SELECT status,payload FROM archive_tasks WHERE case_id=?", (case_id,)).fetchone()
        assert isinstance(snapshot, bytes) and snapshot
        assert archive[0] == "READY" and isinstance(archive[1], bytes) and archive[1]
    finally:
        stack.shutdown()
    (source / ".instance.lock").write_bytes(b"offline archive source\n")
    before = _manifest(source)
    target = tmp_path / "archive-target"
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    result = migration.import_data_root(source, target, execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert _manifest(source) == before
    assert result["tables"]["completed_cases"]["target"]["rows"] == 1
    assert result["tables"]["archive_tasks"]["target"]["rows"] == 1
    assert {key: (target / key).read_bytes() for key in expected_files} == expected_files
    repository = CaseStateRepository(target, StorageCoordinationLock(), stack.clock, stack.ids,
        database_url=postgres_database_url)
    try:
        _verify_table_proofs(repository, result)
        assert repository.read_case(case_id).model_dump_json() == expected_case
        with repository.database_read() as db:
            assert db.execute("SELECT snapshot,pg_typeof(snapshot)::text FROM completed_cases WHERE case_id=?",
                (case_id,)).fetchone() == (snapshot, "bytea")
            assert db.execute("SELECT status,payload,pg_typeof(payload)::text FROM archive_tasks WHERE case_id=?",
                (case_id,)).fetchone() == (*archive, "bytea")
    finally:
        repository.close()


def test_postgres_import_retains_valid_followup_snapshot_answers_and_memory_history(
        website, tmp_path, migration_host, postgres_database_url, monkeypatch):
    stack, store, _, agent, client = website
    followups, backend = _install(website)
    monkeypatch.setattr(stack.catalog, "_route_skill_refs", [])
    monkeypatch.setattr(stack.catalog, "_generic_logparse_product", "compact")
    monkeypatch.setattr(stack.runtime._generic_locator_executor, "_backend", GenericV2Backend())
    cid = agent.create_conversation("migration-followup", owner_key=OWNER_KEY).conversation_id
    prefix = f"/api/v1/agent/conversations/{cid}"
    attachment = _preupload(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "problem", "text": "设备为何反复重启？", "attachment_ids": [attachment]})
    assert agent.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    assert store.get_status(cid).case_status == "WAITING_ATTACHMENT"
    assert agent.run_once(cid)
    assert stack.scheduler.wait_until_idle(20)
    assert store.get_status(cid).report_state == "READY"
    _exercise(website, followups, backend, cid, prefix, "GENERIC")
    memory = MemoryStore(stack.repository, stack.clock)
    task = memory.claim_task()
    assert task is not None
    assert memory.finish_task(task["task_id"], json.dumps({
        "problem_features": ["连接池排队"], "applicability": ["高负载服务"],
        "steps": ["核对连接池容量与等待指标"], "limitations": ["仍需当前日志证据"]}, ensure_ascii=False))
    cards = memory.active_cards(task["skill_name"])
    assert len(cards) == 1
    rid = store.get_run(cid)["run_id"]
    snapshot = followups.store.snapshot(rid)
    assert snapshot["status"] == "READY"
    manifest = json.loads(snapshot["manifest_json"])
    assert manifest["files"]
    source = stack.data_root
    relative_snapshot = f"jobs/{snapshot['source_job_id']}/followup-inputs"
    expected_files = {f"{relative_snapshot}/{item['path']}":
        (source / relative_snapshot / item["path"]).read_bytes() for item in manifest["files"]}
    expected_files[f"{relative_snapshot}/manifest.json"] = (source / relative_snapshot / "manifest.json").read_bytes()
    retained_tables = ("agent_followup_snapshots", "agent_followup_tasks", "agent_followup_events",
        "memory_feedback", "memory_feedback_requests", "memory_tasks")
    expected_rows = {}
    with stack.repository.database_read() as db:
        for table in retained_tables:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            expected_rows[table] = (columns, migration._digest(db.execute(f'SELECT * FROM "{table}"')))
            assert expected_rows[table][1]["rows"] > 0
        assert db.execute("SELECT COUNT(*) FROM agent_followup_tasks WHERE status='COMPLETED'").fetchone()[0] == 2
    assert agent.shutdown(2)
    stack.shutdown()
    (source / ".instance.lock").write_bytes(b"offline followup source\n")
    before = _manifest(source)
    target = tmp_path / "followup-target"
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    result = migration.import_data_root(source, target, execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert _manifest(source) == before
    assert {key: (target / key).read_bytes() for key in expected_files} == expected_files
    repository = CaseStateRepository(target, StorageCoordinationLock(), stack.clock, stack.ids,
        database_url=postgres_database_url)
    try:
        _verify_table_proofs(repository, result)
        with repository.database_read() as db:
            for table, (columns, expected) in expected_rows.items():
                selected = ",".join('"' + column + '"' for column in columns)
                assert migration._digest(db.execute(f'SELECT {selected} FROM "{table}"')) == expected
        imported_followups = FollowupStore(repository, stack.clock)
        assert imported_followups.snapshot(rid) == snapshot
        assert MemoryStore(repository, stack.clock).active_cards(task["skill_name"]) == cards
    finally:
        repository.close()


def test_postgres_import_reinstates_both_fences_after_activation_and_barrier_removal(
        sqlite_source, tmp_path, migration_host, postgres_database_url, monkeypatch):
    import psycopg
    source, _, _ = sqlite_source
    before = _manifest(source)
    target = tmp_path / "target"
    monkeypatch.setenv("POSTGRES_IMPORT_TEST_URL", postgres_database_url)
    original_sync = migration.PlatformFileSync.sync_directory
    injected = []

    def fail_final_directory_sync(sync, path):
        if path == target and target.exists() and not (target / "data-format.json.tmp").exists() and not injected:
            with psycopg.connect(postgres_database_url) as db:
                assert db.execute("SELECT value FROM metadata WHERE key='storage_backend'").fetchone()[0] == "postgresql-v1"
            injected.append(True)
            raise OSError("injected directory fsync failure after activation")
        return original_sync(sync, path)

    monkeypatch.setattr(migration.PlatformFileSync, "sync_directory", fail_final_directory_sync)
    with pytest.raises(migration.PostgresImportError) as failure:
        migration.import_data_root(source, target, execute=True, database_url_env="POSTGRES_IMPORT_TEST_URL")
    assert injected == [True]
    assert failure.value.staging_root is not None
    assert target.is_dir() and (target / "data-format.json.tmp").is_file()
    assert (target / migration.RECEIPT_FILENAME).is_file()
    assert (failure.value.staging_root / "source-database" / "completed.sqlite3").is_file()
    assert _manifest(source) == before
    with psycopg.connect(postgres_database_url) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='storage_backend'").fetchone()[0] == "postgresql-import-pending"
        assert db.execute("SELECT COUNT(*) FROM agent_attachments").fetchone()[0] == 1
    with pytest.raises(ApplicationPortError) as blocked:
        CaseStateRepository(target, StorageCoordinationLock(), FixedClock("2026-09-28T00:00:00.000Z"),
            DeterministicIdGenerator(), database_url=postgres_database_url)
    assert blocked.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
