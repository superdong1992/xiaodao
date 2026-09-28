"""Real PostgreSQL fixtures; every example owns a new database and keeps evidence."""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest

from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import CaseStateRepository


class PostgresTestDatabaseUrl(str):
    """Keep the connection value usable without exposing it in pytest arguments."""

    def __repr__(self):
        return "<PostgreSQL test database URL redacted>"


@pytest.fixture
def postgres_database_url(tmp_path):
    import psycopg
    from psycopg import sql

    source = os.environ.get("PROBLEM_LOCATOR_TEST_DATABASE_URL")
    if not source:
        pytest.fail("PostgreSQL 专项需要 PROBLEM_LOCATOR_TEST_DATABASE_URL，不能以 SQLite 替代。")
    parts = urlsplit(source)
    if parts.scheme not in {"postgresql", "postgres"} or "_test" not in parts.path:
        pytest.fail("测试连接必须指向名称含 _test 的独立管理库。")
    name = "pl_test_" + uuid.uuid4().hex
    # Only this fixed prefix is created; existing databases are never reset,
    # reused or dropped. The test's receipt contains no credentials.
    with psycopg.connect(source, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlunsplit(parts._replace(path="/" + name))
    (tmp_path / "postgres-database.txt").write_text(name + "\n", encoding="utf-8")
    yield PostgresTestDatabaseUrl(target)


@pytest.fixture
def postgres_repository(tmp_path, postgres_database_url):
    repository = CaseStateRepository(tmp_path / "data", StorageCoordinationLock(),
        SimpleNamespace(now=lambda: "2026-09-28T00:00:00.000Z"),
        SimpleNamespace(new=lambda kind: str(uuid.uuid4())),
        database_url=postgres_database_url)
    try:
        yield repository
    finally:
        repository.close()
