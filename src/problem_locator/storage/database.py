"""Explicit PostgreSQL SQL helpers and pooled, thread-scoped connections.

SQLite connections remain native objects for offline tools and deterministic
reference tests. Production connections never translate SQL dialects: the only
rewrite is DB-API qmark parameter binding to psycopg's ``%s`` placeholders.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JSON_PATH = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


class DatabaseOwnershipError(ValueError):
    """Another Server still owns the database's process-local state domain."""


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid SQL identifier")
    return value


def _path(value: str) -> tuple[str, ...]:
    if not _JSON_PATH.fullmatch(value):
        raise ValueError("only named JSON object paths are supported")
    return tuple(value[2:].split("."))


@dataclass(frozen=True)
class SqlDialect:
    postgres: bool = False

    def json_text(self, column: str, path: str) -> str:
        parts = _path(path)
        if not self.postgres:
            return f"json_extract({column},'{path}')"
        return f"({column}::jsonb #>> '{{{','.join(parts)}}}')"

    def json_int(self, column: str, path: str) -> str:
        value = self.json_text(column, path)
        if not self.postgres:
            return value
        # SQLite JSON booleans are integers; retain that explicit query contract.
        return f"(CASE {value} WHEN 'true' THEN 1 WHEN 'false' THEN 0 ELSE ({value})::bigint END)"

    def json_document(self, column: str, path: str) -> str:
        parts = _path(path)
        if not self.postgres:
            return self.json_text(column, path)
        return f"NULLIF(({column}::jsonb #> '{{{','.join(parts)}}}'), 'null'::jsonb)::text"

    def json_object(self, projection: str) -> str:
        return f"json_build_object({projection})::text" if self.postgres else f"json_object({projection})"

    def json_array(self, column: str, path: str, alias: str) -> str:
        _identifier(alias)
        parts = _path(path)
        if not self.postgres:
            return f"json_each({column},'{path}') {alias}"
        return (f"jsonb_array_elements_text(coalesce(NULLIF({column}::jsonb #> "
                f"'{{{','.join(parts)}}}', 'null'::jsonb), '[]'::jsonb)) AS {alias}(value)")

    def order_column(self, alias: str = "") -> str:
        prefix = _identifier(alias) + "." if alias else ""
        return prefix + ("storage_order" if self.postgres else "rowid")

    def ordered_table(self, statement: str) -> str:
        """Add an explicit insertion sequence to one caller-selected table."""
        if not self.postgres:
            return statement
        if not re.match(r"\s*CREATE\s+TABLE\b", statement, re.IGNORECASE):
            return statement
        head, closing, suffix = statement.rpartition(")")
        if not closing or suffix.strip() not in {"", ";"}:
            raise ValueError("ordered_table requires one CREATE TABLE statement")
        return head + ", storage_order BIGINT GENERATED ALWAYS AS IDENTITY)" + suffix


_SQLITE = SqlDialect()
_POSTGRES = SqlDialect(postgres=True)


def dialect_for(db) -> SqlDialect:
    return _POSTGRES if isinstance(db, (PostgresConnection, PostgresConnectionProxy)) else _SQLITE


def table_names(db) -> set[str]:
    if dialect_for(db).postgres:
        return {row[0] for row in db.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def table_exists(db, name: str) -> bool:
    _identifier(name)
    if dialect_for(db).postgres:
        return db.execute("SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname=current_schema() AND tablename=?", (name,)).fetchone() is not None
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def table_columns(db, name: str) -> set[str]:
    _identifier(name)
    if dialect_for(db).postgres:
        return {row[0] for row in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (name,))}
    return {row[1] for row in db.execute(f"PRAGMA table_info({name})")}


def index_definition(db, name: str) -> str | None:
    _identifier(name)
    if dialect_for(db).postgres:
        row = db.execute("SELECT indexdef FROM pg_catalog.pg_indexes WHERE schemaname=current_schema() AND indexname=?", (name,)).fetchone()
    else:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
    return None if row is None else row[0]


def _advisory_key(namespace: str, key: str) -> int:
    digest = hashlib.sha256((namespace + "\0" + key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


_OWNER_KEY = _advisory_key("problem-locator", "server-owner")
_DRAIN_KEY = _advisory_key("problem-locator", "server-owner-drain")


def lock_key(db, namespace: str, key: str) -> None:
    if dialect_for(db).postgres and db.write_transaction:
        db.execute("SELECT pg_advisory_xact_lock(?)", (_advisory_key(namespace, str(key)),))


def lock_conversation(db, conversation_id: str) -> None:
    lock_key(db, "conversation", conversation_id)


def try_lock_conversation(db, conversation_id: str) -> bool:
    if not dialect_for(db).postgres or not db.write_transaction:
        return True
    return bool(db.execute("SELECT pg_try_advisory_xact_lock(?)", (_advisory_key("conversation", conversation_id),)).fetchone()[0])


@lru_cache(maxsize=2048)
def bind_qmarks(statement: str) -> str:
    """Bind positional parameters, preserving literals, identifiers and comments.

    SQL dialect selection belongs to callers. In particular this function does
    not recognize or rewrite SQLite keywords, JSON operators or table names.
    Existing percent signs are escaped for psycopg only when parameters exist.
    """
    output = []
    index, length = 0, len(statement)
    while index < length:
        char = statement[index]
        if char in "'\"":
            quote, start = char, index
            index += 1
            while index < length:
                if statement[index] == quote:
                    index += 1
                    if index < length and statement[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            output.append(statement[start:index].replace("%", "%%"))
        elif statement.startswith("--", index):
            end = statement.find("\n", index)
            end = length if end < 0 else end + 1
            output.append(statement[index:end].replace("%", "%%"))
            index = end
        elif statement.startswith("/*", index):
            start, depth = index, 1
            index += 2
            while index < length and depth:
                if statement.startswith("/*", index):
                    depth += 1
                    index += 2
                elif statement.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            output.append(statement[start:index].replace("%", "%%"))
        elif char == "$" and (match := re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", statement[index:])):
            tag = match.group()
            end = statement.find(tag, index + len(tag))
            if end < 0:
                raise ValueError("unterminated SQL dollar quote")
            end += len(tag)
            output.append(statement[index:end].replace("%", "%%"))
            index = end
        else:
            output.append("%s" if char == "?" else "%%" if char == "%" else char)
            index += 1
    return "".join(output)


class PostgresConnection:
    """Small DB-API surface with explicit transaction ownership."""

    def __init__(self, connection):
        self.raw = connection
        self.transaction_depth = 0
        self.write_transaction = False

    def execute(self, statement: str, values=None):
        return self.raw.execute(statement if values is None else bind_qmarks(statement), values)

    def executemany(self, statement: str, values):
        cursor = self.raw.cursor()
        cursor.executemany(bind_qmarks(statement), values)
        return cursor

    def executescript(self, statement: str):
        # No parameters: psycopg sends the explicitly selected native schema.
        return self.raw.execute(statement, prepare=False)

    @property
    def in_transaction(self) -> bool:
        return self.transaction_depth > 0


class PostgresConnectionProxy:
    """Repository compatibility facade; never owns or shares a connection."""

    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        connection = self.database.current
        if connection is None:
            raise RuntimeError("PostgreSQL access requires a database scope")
        return getattr(connection, name)


class ConnectionScope:
    """Reentrant lease, deliberately not a process-wide mutex."""

    def __init__(self, database):
        self.database = database
        self._local = threading.local()

    def __enter__(self):
        scope = self.database.connection()
        connection = scope.__enter__()
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append(scope)
        return connection

    def __exit__(self, *args):
        return self._local.stack.pop().__exit__(*args)

    def _is_owned(self):
        return bool(getattr(self._local, "stack", ()))


class PostgresDatabase:
    """One server owner and independent pooled connections for concurrent work."""

    def __init__(self, database_url: str, *, pool_size: int = 8):
        if not isinstance(database_url, str) or not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL 必须是 PostgreSQL 连接地址。")
        if isinstance(pool_size, bool) or not isinstance(pool_size, int) or not 2 <= pool_size <= 64:
            raise ValueError("数据库连接池大小必须为 2 至 64。")
        import psycopg
        from psycopg_pool import ConnectionPool

        self.errors = (sqlite3.Error, psycopg.Error, DatabaseOwnershipError)
        self._local = threading.local()
        self._owner_guard = threading.Lock()
        self._closed = False
        self._active = 0
        self._pool = None
        self._owner = psycopg.connect(database_url, autocommit=True, connect_timeout=10,
                                      options="-c search_path=public,pg_catalog -c synchronous_commit=on")
        self._owner_pid = self._owner.info.backend_pid
        try:
            if self._owner.info.server_version < 170000:
                raise ValueError("PostgreSQL 版本过低，请使用 PostgreSQL 17 或更高版本。")
            owned = self._owner.execute("SELECT pg_try_advisory_lock(%s)",
                (_OWNER_KEY,)).fetchone()[0]
            if not owned:
                raise DatabaseOwnershipError("该 PostgreSQL 数据库已由另一个 Problem Locator Server 使用。")
            # A lost owner connection must not permit takeover while one of its
            # pooled transactions is still committing. New work holds a shared
            # transaction lock; startup drains those leases before proceeding.
            self._owner.execute("SET lock_timeout='30s'")
            self._owner.execute("SELECT pg_advisory_lock(%s)", (_DRAIN_KEY,))
            self._owner.execute("SELECT pg_advisory_unlock(%s)", (_DRAIN_KEY,))
            self._pool = ConnectionPool(database_url, min_size=1, max_size=pool_size,
                kwargs={"autocommit": True, "connect_timeout": 10,
                        "options": "-c search_path=public,pg_catalog -c synchronous_commit=on"},
                timeout=30, open=False, name="problem-locator-database")
            self._pool.open(wait=True, timeout=15)
        except BaseException:
            if self._pool is not None:
                self._pool.close()
            self._owner.close()
            raise
        self.proxy = PostgresConnectionProxy(self)
        self.scope = ConnectionScope(self)

    @property
    def current(self):
        return getattr(self._local, "connection", None)

    @contextmanager
    def connection(self):
        current = self.current
        if current is not None:
            yield current
            return
        # Never reacquire a lost ownership session: its loss is a fencing error.
        with self._owner_guard:
            if self._closed:
                raise RuntimeError("database is closed")
            self._owner.execute("SELECT 1")
            self._active += 1
        try:
            with self._pool.connection() as raw:
                connection = PostgresConnection(raw)
                self._local.connection = connection
                try:
                    yield connection
                finally:
                    self._local.connection = None
        finally:
            with self._owner_guard:
                self._active -= 1
                if self._closed and self._active == 0:
                    self._owner.close()

    @contextmanager
    def transaction(self, *, readonly: bool = False):
        with self.connection() as db:
            if db.transaction_depth:
                if not readonly and not db.write_transaction:
                    raise RuntimeError("cannot write inside a read-only database scope")
                # A nested write gets a savepoint; a nested read sees the caller's
                # exact transaction, including its uncommitted projection.
                if readonly:
                    yield db
                else:
                    with db.raw.transaction():
                        yield db
                return
            with db.raw.transaction():
                db.transaction_depth = 1
                db.write_transaction = not readonly
                try:
                    if readonly:
                        db.raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    db.execute("SELECT pg_advisory_xact_lock_shared(?)", (_DRAIN_KEY,))
                    unsigned = _OWNER_KEY & ((1 << 64) - 1)
                    owner = db.execute("SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory' "
                        "AND pid=? AND database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()) "
                        "AND classid::bigint=? AND objid::bigint=? AND objsubid=1 "
                        "AND mode='ExclusiveLock' AND granted", (self._owner_pid, unsigned >> 32, unsigned & 0xffffffff)).fetchone()
                    if owner is None:
                        raise DatabaseOwnershipError("PostgreSQL 服务实例锁已失效，请重启服务。")
                    yield db
                finally:
                    db.transaction_depth = 0
                    db.write_transaction = False

    def close(self):
        with self._owner_guard:
            if self._closed:
                return
            self._closed = True
        self._pool.close()
        with self._owner_guard:
            # Checked-out connections may still be completing shutdown. Keep
            # ownership until the final lease ends so another server cannot
            # overlap a transaction from the retiring owner.
            if self._active == 0:
                self._owner.close()


__all__ = ["PostgresDatabase", "DatabaseOwnershipError", "SqlDialect", "bind_qmarks", "dialect_for", "table_names",
           "table_exists", "table_columns", "index_definition", "lock_key",
           "lock_conversation", "try_lock_conversation"]
