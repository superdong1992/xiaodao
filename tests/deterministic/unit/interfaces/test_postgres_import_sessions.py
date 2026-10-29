"""Native session metadata imports without changing the SQLite source."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.entrypoints import postgres_import as migration
from problem_locator.followup.sessions import FollowupSessions
from problem_locator.followup.store import FollowupStore
from tests.deterministic.unit.interfaces.test_postgres_import_memory import _ImportTarget
from tests.deterministic.unit.storage.test_state_repository import _open


@pytest.fixture
def session_source(tmp_path):
    repository = _open(tmp_path / "source")
    agent = AgentStore(repository)
    FollowupStore(repository)
    conversation = agent.create_conversation("session-import")
    try:
        yield repository, conversation.conversation_id
    finally:
        repository.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_source_schema_accepts_optional_native_session_tables(session_source, legacy):
    repository, _ = session_source
    with repository.database_transaction() as db:
        if legacy:
            db.execute("DROP TABLE agent_followup_sessions")
            db.execute("DROP TABLE agent_followup_session_metadata")
        tables, _ = migration._source_schema(db)
        assert ("agent_followup_sessions" in tables) is not legacy


@pytest.mark.parametrize("invalid", ["missing-table", "missing-marker", "future-version", "missing-column"])
def test_source_schema_rejects_invalid_native_session_storage(session_source, invalid):
    repository, _ = session_source
    with repository.database_transaction() as db:
        if invalid == "missing-table":
            db.execute("DROP TABLE agent_followup_sessions")
        elif invalid == "missing-marker":
            db.execute("DELETE FROM agent_followup_session_metadata")
        elif invalid == "future-version":
            db.execute("UPDATE agent_followup_session_metadata SET value='2'")
        else:
            db.execute("ALTER TABLE agent_followup_sessions DROP COLUMN report_sha256")
        with pytest.raises(migration.PostgresImportError):
            migration._source_schema(db)


@pytest.mark.parametrize("has_session_tables", [False, True])
def test_import_rows_preserves_session_checkpoint_and_initialized_marker(session_source, tmp_path, has_session_tables):
    repository, cid = session_source
    if has_session_tables:
        with repository.database_transaction() as db:
            db.execute("INSERT INTO agent_followup_sessions(conversation_id,session_id,last_order) VALUES (?,?,?)",
                (cid, "00000000-0000-0000-0000-000000000123", 27))
    tables = {"agent_followup_session_metadata", "agent_followup_sessions"} if has_session_tables else set()
    with closing(sqlite3.connect(":memory:")) as target:
        target.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        adapter = _ImportTarget(target)
        target_repository = SimpleNamespace(database_transaction=adapter.database_transaction)
        FollowupSessions(SimpleNamespace(repository=target_repository))
        with repository.database_read() as source:
            target.execute("INSERT INTO metadata(key,value) VALUES ('installation_id',?)",
                source.execute("SELECT value FROM metadata WHERE key='installation_id'").fetchone())
            before = {table: list(source.execute(f'SELECT * FROM "{table}"')) for table in tables}
            proofs = migration._import_rows(source, target_repository, tables,
                tmp_path / "source", tmp_path / "target", {})
            assert {table: list(source.execute(f'SELECT * FROM "{table}"')) for table in tables} == before
        assert target.execute("SELECT value FROM agent_followup_session_metadata WHERE key='storage_version'").fetchone() == ("1",)
        assert set(proofs) == tables
        for table in tables:
            rows = list(target.execute(f'SELECT * FROM "{table}"'))
            assert rows == before[table]
            assert proofs[table]["source"] == proofs[table]["target"] == migration._digest(rows)
