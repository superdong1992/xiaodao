"""Pressure-candidate keyset pagination on the actual PostgreSQL adapter."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import uuid

from problem_locator.storage.database import PostgresConnection, PostgresDatabase, dialect_for
from problem_locator.storage.web_resource_retention import WebResourceRetention
from tests.postgres_helpers import postgres_database_url


def test_postgres_pressure_batches_keep_equal_instants_ordered_while_rows_disappear(postgres_database_url, tmp_path):
    database = PostgresDatabase(postgres_database_url)
    batches, queries = [], []

    class Query:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            batches.append(len(rows))
            return rows

    class Connection(PostgresConnection):
        def __init__(self, db):
            self.db = db

        def execute(self, sql, params=()):
            if not queries:
                plan = self.db.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0]
                (tmp_path / "pressure-candidates-explain.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            queries.append(sql)
            return Query(self.db.execute(sql, params))

    @contextmanager
    def read():
        with database.transaction(readonly=True) as db:
            yield Connection(db)

    try:
        ids = [str(uuid.UUID(int=index + 1)) for index in range(397)]
        with database.transaction() as db:
            db.executescript("""
                CREATE TABLE agent_conversations(conversation_id TEXT PRIMARY KEY, deleted_at TEXT);
                CREATE TABLE agent_conversation_runs(
                    conversation_id TEXT, run_id TEXT PRIMARY KEY, case_id TEXT, status TEXT, body TEXT);
            """)
            conversations, runs = [], []
            for index, rid in reversed(list(enumerate(ids))):
                timestamp = "2026-10-01T08:00:00+08:00" if index % 2 else "2026-10-01T00:00:00.000Z"
                conversations.append((rid,))
                runs.append((rid, rid, json.dumps({"completed_at": timestamp, "resources_expired": False})))
            db.executemany("INSERT INTO agent_conversations VALUES (?,NULL)", conversations)
            db.executemany("INSERT INTO agent_conversation_runs VALUES (?,?,NULL,'COMPLETED',?)", runs)
            # Match the existing production partial index; do not add a new
            # timestamp-expression index merely to make this plan look better.
            d = dialect_for(db)
            completed = f"coalesce({d.json_text('body', '$.completed_at')},{d.json_text('body', '$.updated_at')})"
            db.execute(f"CREATE INDEX agent_web_resource_pending ON agent_conversation_runs({completed},run_id) "
                "WHERE status IN ('COMPLETED','FAILED','INTERRUPTED','CANCELLED') "
                f"AND coalesce({d.json_int('body', '$.resources_expired')},0)=0")
            db.execute("ANALYZE agent_conversations")
            db.execute("ANALYZE agent_conversation_runs")
        web = WebResourceRetention(SimpleNamespace(repository=SimpleNamespace(database_read=read)))
        observed = []
        for cid, rid, case_id in web.candidates("2026-10-01T00:00:00.000Z"):
            assert cid == rid and case_id is None
            observed.append(rid)
            # A completed cleanup removes eligibility during pagination. OFFSET
            # pagination would skip later rows as this result set shrinks.
            if len(observed) % 128 == 0:
                with database.transaction() as db:
                    db.execute("DELETE FROM agent_conversation_runs WHERE run_id<=?", (rid,))
        assert observed == ids
        assert batches == [128, 128, 128, 13]
        assert len(queries) == 4 and all("LIMIT" in sql for sql in queries)
    finally:
        database.close()
