"""Read-only import projections preserve legacy one-shot extraction receipts."""
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest

from problem_locator.entrypoints import postgres_import as migration
from problem_locator.memory.store import _SCHEMA
from tests.deterministic.unit.agent.test_generic_feedback import put, system


@pytest.mark.parametrize("task_kind,expected", [("matching", 1), ("different-report", 0), ("none", 0)])
def test_legacy_feedback_import_derives_attempt_only_from_same_report(system, task_kind, expected):
    put(system, extract_memory=task_kind != "none")
    with system.repository.database_transaction() as db:
        if task_kind == "different-report":
            db.execute("UPDATE memory_tasks SET report_sha256=?", ("f" * 64,))
        db.execute("ALTER TABLE memory_feedback DROP COLUMN extraction_attempted")
    with system.repository.database_read() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(memory_feedback)")]
        original = db.execute("SELECT rowid,* FROM memory_feedback").fetchall()
        db.execute("PRAGMA query_only=ON")
        try:
            derived, rows = migration._import_source_rows(db, "memory_feedback", columns,
                set(columns) | {"extraction_attempted"})
            imported = list(rows)
        finally:
            db.execute("PRAGMA query_only=OFF")
        assert derived == ["extraction_attempted"]
        assert imported == [(*original[0], expected)]
        assert db.execute("SELECT rowid,* FROM memory_feedback").fetchall() == original
        assert migration._digest(row[1:1 + len(columns)] for row in imported) == migration._digest(
            row[1:] for row in original)


def test_current_feedback_import_keeps_receipt_after_task_was_pruned(system):
    put(system)
    system.clock.value = "2026-09-28T00:00:00.000Z"
    system.memory.prune()
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 0
        columns = [row[1] for row in db.execute("PRAGMA table_info(memory_feedback)")]
        original = db.execute("SELECT rowid,* FROM memory_feedback").fetchall()
        derived, rows = migration._import_source_rows(db, "memory_feedback", columns, set(columns))
        assert derived == []
        assert list(rows) == original
        assert original[0][1 + columns.index("extraction_attempted")] == 1


@pytest.mark.parametrize("table", ["memory_feedback", "memory_tasks"])
def test_import_does_not_accept_other_schema_differences(system, table):
    with system.repository.database_read() as db:
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
        with pytest.raises(migration.PostgresImportError) as error:
            migration._import_source_rows(db, table, columns, set(columns) | {"unknown"})
    assert error.value.code == "VERSION_UNSUPPORTED"


class _ImportTarget:
    """Execute the complete import loop locally; only PG control SQL is fake."""

    def __init__(self, connection):
        self.connection = connection
        self.raw = self

    def execute(self, query, parameters=()):
        if query.startswith("LOCK TABLE "):
            return None
        if query.startswith("SELECT column_name,data_type FROM information_schema.columns "):
            return [(row[1], row[2].lower()) for row in self.connection.execute(
                f'PRAGMA table_info("{parameters[0]}")')]
        return self.connection.execute(query, parameters)

    def executemany(self, query, rows):
        return self.connection.executemany(query, rows)

    @contextmanager
    def cursor(self, *, name):
        with closing(self.connection.cursor()) as cursor:
            yield cursor

    @contextmanager
    def database_transaction(self):
        with self.connection:
            yield self


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("had_task", [False, True])
def test_import_rows_retains_all_values_and_hashes_with_derived_receipt(system, tmp_path, legacy, had_task):
    put(system, extract_memory=had_task)
    if legacy:
        with system.repository.database_transaction() as db:
            db.execute("ALTER TABLE memory_feedback DROP COLUMN extraction_attempted")
    tables = {"metadata", "memory_feedback", "memory_feedback_requests", "memory_tasks"}
    with closing(sqlite3.connect(":memory:")) as target:
        target.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        for statement in _SCHEMA:
            target.execute(statement)
        adapter = _ImportTarget(target)
        repository = SimpleNamespace(database_transaction=adapter.database_transaction)
        with system.repository.database_read() as source:
            before = {table: list(source.execute(f'SELECT * FROM "{table}"')) for table in tables}
            proofs = migration._import_rows(source, repository, tables, tmp_path / "source",
                tmp_path / "target", {})
            assert {table: list(source.execute(f'SELECT * FROM "{table}"')) for table in tables} == before
        for table in tables:
            proof = proofs[table]
            assert proof["source"] == migration._digest(before[table])
            columns = ",".join('"' + name + '"' for name in proof["columns"])
            rows = list(target.execute(f'SELECT {columns} FROM "{table}"'))
            if table == "metadata":
                # The proof includes the final backend activation value.
                rows = [(key, "postgresql-v1" if key == "storage_backend" else value) for key, value in rows]
            assert proof["target"] == migration._digest(rows)
        expected = before["memory_feedback"][0]
        if legacy:
            expected = (*expected, int(had_task))
            assert proofs["memory_feedback"]["derived_columns"] == ["extraction_attempted"]
        else:
            assert "derived_columns" not in proofs["memory_feedback"]
        assert target.execute("SELECT * FROM memory_feedback").fetchall() == [expected]
