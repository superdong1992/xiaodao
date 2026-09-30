"""Binding, resource-root safety and independent database lease regressions."""
from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from problem_locator.contracts import ApplicationPortError, ErrorCode
from problem_locator.diagnostics import JsonDiagnosticFormatter, log_event
from problem_locator.entrypoints.settings import load_database_configuration
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.database import (PostgresDatabase, SqlDialect, bind_qmarks,
    dialect_for, table_names, table_columns, index_definition)
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES, StorageLayout, UnsupportedDataFormatError
from problem_locator.storage.postgres_layout import (initialize_postgres_root, preflight_postgres_root,
    validate_postgres_root)
from problem_locator.storage.state_repository import CaseStateRepository
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FixedClock
from tests.postgres_helpers import PostgresTestDatabaseUrl


@pytest.mark.parametrize(("server_version", "supported"), [
    (140015, False),
    (149999, False),
    (150000, True),
    (150001, True),
    (150019, True),
    (160000, True),
    (170011, True),
])
def test_postgres_startup_version_boundary_and_rejected_connection_cleanup(
    monkeypatch, server_version, supported,
):
    import psycopg
    import psycopg_pool

    owner = Mock(info=SimpleNamespace(server_version=server_version, backend_pid=123))
    owner.execute.return_value.fetchone.return_value = (True,)
    pool = Mock()
    pool_factory = Mock(return_value=pool)
    monkeypatch.setattr(psycopg, "connect", Mock(return_value=owner))
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", pool_factory)

    if supported:
        database = PostgresDatabase("postgresql://localhost/version_test")
        try:
            pool.open.assert_called_once_with(wait=True, timeout=15)
            owner.close.assert_not_called()
        finally:
            database.close()
        pool.close.assert_called_once_with()
    else:
        with pytest.raises(ValueError, match="PostgreSQL 15 或更高版本"):
            PostgresDatabase("postgresql://localhost/version_test")
        pool_factory.assert_not_called()
        owner.execute.assert_not_called()
    owner.close.assert_called_once_with()


def test_qmark_binding_never_changes_literals_comments_or_sql_dialect():
    statement = "SELECT ?, 'it''s ? 50%', \"column?%\", $$? %$$ /* ? /* ? */ % */ -- ? %\nFROM x WHERE n % 2=?"
    assert bind_qmarks(statement) == (
        "SELECT %s, 'it''s ? 50%%', \"column?%%\", $$? %%$$ /* ? /* ? */ %% */ -- ? %%\nFROM x WHERE n %% 2=%s")
    assert bind_qmarks("INSERT OR IGNORE INTO x VALUES (?)") == "INSERT OR IGNORE INTO x VALUES (%s)"


def test_reference_helpers_preserve_native_sqlite_queries_and_indexes():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE records(id TEXT PRIMARY KEY,body TEXT)")
        db.execute("CREATE INDEX record_type ON records(json_extract(body,'$.type'))")
        db.execute("INSERT INTO records VALUES (?,?)", ("one", '{"type":"event","ready":true,"object":{"key":1}}'))
        sql = dialect_for(db)
        assert not sql.postgres
        assert db.execute(f"SELECT {sql.json_text('body', '$.type')},{sql.json_int('body', '$.ready')},{sql.json_document('body', '$.object')} FROM records").fetchone() == ('event', 1, '{"key":1}')
        assert table_names(db) == {"records"}
        assert table_columns(db, "records") == {"id", "body"}
        assert "json_extract" in index_definition(db, "record_type")
        assert sql.order_column("r") == "r.rowid"
    finally:
        db.close()


def test_postgres_schema_order_is_explicit_and_json_types_are_selected():
    sql = SqlDialect(postgres=True)
    assert sql.order_column("r") == "r.storage_order"
    assert sql.ordered_table("CREATE TABLE t (id TEXT PRIMARY KEY)") == (
        "CREATE TABLE t (id TEXT PRIMARY KEY, storage_order BIGINT GENERATED ALWAYS AS IDENTITY)")
    assert sql.json_document("body", "$.data.value") == "NULLIF((body::jsonb #> '{data,value}'), 'null'::jsonb)::text"
    with pytest.raises(ValueError):
        sql.json_text("body", "$.key'); DROP TABLE metadata; --")


def test_postgres_rejects_sqlite_root_without_changing_its_bytes(tmp_path):
    marker = tmp_path / "data-format.json"
    marker.write_bytes(DATA_FORMAT_MARKER_BYTES)
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(UnsupportedDataFormatError):
        preflight_postgres_root(StorageLayout.at(tmp_path))
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original


def test_postgres_marker_binds_installation_and_refuses_rebinding(tmp_path):
    layout = StorageLayout.at(tmp_path)
    installation = "10000000-0000-0000-0000-000000000001"
    initialize_postgres_root(layout, installation, FakeFileSync())
    original = layout.data_format_marker.read_bytes()
    assert validate_postgres_root(layout)["installation_id"] == installation
    with pytest.raises(UnsupportedDataFormatError):
        initialize_postgres_root(layout, "10000000-0000-0000-0000-000000000002", FakeFileSync())
    assert layout.data_format_marker.read_bytes() == original
    assert not (tmp_path / "completed.sqlite3").exists()


def test_postgres_initialization_diagnostic_excludes_driver_password(tmp_path, caplog):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    password = "synthetic-secret-marker"
    database_url = f"postgresql://test_user:{password}%@localhost/test_db"
    configured_url, pool_size = load_database_configuration({"DATABASE_URL": database_url})
    # The real libpq parser rejects this before opening a network connection,
    # and its original diagnostic includes the synthetic password.
    with pytest.raises(psycopg.ProgrammingError) as driver_error:
        conninfo_to_dict(configured_url)
    assert password in str(driver_error.value)

    with pytest.raises(ApplicationPortError) as failure:
        CaseStateRepository(tmp_path / "data", StorageCoordinationLock(),
            FixedClock(), DeterministicIdGenerator(),
            database_url=configured_url, database_pool_size=pool_size)
    assert failure.value.error.code is ErrorCode.STATE_CORRUPT
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__
    # Service assembly wraps port errors before emitting the structured event.
    with caplog.at_level(logging.ERROR, logger="problem_locator.dfx"):
        try:
            raise RuntimeError("服务装配失败") from failure.value
        except RuntimeError as error:
            log_event("service.assembly_failed", level=logging.ERROR, error=error)
    diagnostic = JsonDiagnosticFormatter().format(caplog.records[-1])
    assert "ApplicationPortError" in diagnostic
    assert password not in diagnostic
    assert database_url not in diagnostic
    assert "ProgrammingError" not in diagnostic


def test_postgres_test_url_repr_protects_pytest_failure_arguments():
    password = "synthetic-fixture-secret"
    database_url = f"postgresql://test_user:{password}@localhost/test_db"

    def fail(connection_url):
        raise AssertionError("synthetic test failure")

    def render_failure(connection_url):
        try:
            fail(connection_url)
        except AssertionError:
            return str(pytest.ExceptionInfo.from_current().getrepr(
                style="long", showlocals=False, funcargs=True))

    assert password in render_failure(database_url)
    protected_url = PostgresTestDatabaseUrl(database_url)
    assert str(protected_url) == database_url
    assert password not in render_failure(protected_url)
    assert "PostgreSQL test database URL redacted" in render_failure(protected_url)


class _RawConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return self

    def fetchone(self):
        return (1,)

    @contextmanager
    def transaction(self):
        self.statements.append("BEGIN")
        try:
            yield
        except BaseException:
            self.statements.append("ROLLBACK")
            raise
        else:
            self.statements.append("COMMIT")


class _Pool:
    @contextmanager
    def connection(self):
        yield _RawConnection()


def _database():
    # Exercise production scope/transaction code without claiming a PostgreSQL
    # integration result. Real database concurrency is covered by its own flow.
    database = PostgresDatabase.__new__(PostgresDatabase)
    database._local = threading.local()
    database._owner_guard = threading.Lock()
    database._closed = False
    database._active = 0
    database._owner = _RawConnection()
    database._owner_pid = 123
    database._pool = _Pool()
    return database


def test_unrelated_transactions_get_independent_connections_and_overlap():
    database = _database()
    rendezvous = threading.Barrier(2, timeout=5)

    def write():
        with database.transaction() as connection:
            with database.transaction(readonly=True) as nested:
                assert nested is connection
            rendezvous.wait()
            return connection

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: write(), range(2)))
    assert first is not second
    assert first.raw.statements[0] == second.raw.statements[0] == "BEGIN"
    assert first.raw.statements[-1] == second.raw.statements[-1] == "COMMIT"
    assert database.current is None


def test_read_scope_keeps_one_snapshot_and_rejects_nested_writes():
    database = _database()
    with database.transaction(readonly=True) as connection:
        with database.transaction(readonly=True) as second:
            assert connection is second
        with pytest.raises(RuntimeError, match="read-only"):
            with database.transaction():
                pass
    assert connection.raw.statements[:2] == ["BEGIN", "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"]
    assert connection.raw.statements[-1] == "COMMIT"


def test_failed_transaction_rolls_back_and_releases_thread_lease():
    database = _database()
    with pytest.raises(ValueError, match="injected"):
        with database.transaction() as connection:
            raise ValueError("injected")
    assert connection.raw.statements[-1] == "ROLLBACK"
    assert database.current is None
