"""Pressure reclamation bounds SQL batches and rechecks physical free space."""
from contextlib import contextmanager
import json
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest

from problem_locator.bootstrap import RetentionService
from problem_locator.storage.history_retention import HistoryRetentionService
from problem_locator.storage.web_resource_retention import WebResourceRetention
from tests.deterministic.unit.storage.test_history_retention import _close_run, stack as resource_stack
from tests.deterministic.unit.storage.test_web_resource_retention import upload


class _Query:
    def __init__(self, cursor, batches):
        self.cursor, self.batches = cursor, batches

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.batches.append(len(rows))
        return rows


class _Repository:
    def __init__(self, connection):
        self.connection = connection
        self.queries, self.batches = [], []

    @contextmanager
    def database_read(self):
        yield self

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        if "FROM agent_conversation_runs r " in sql and "ORDER BY" in sql:
            self.queries.append(sql)
            return _Query(cursor, self.batches)
        return cursor


@pytest.fixture
def pressure(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE agent_conversations(conversation_id TEXT PRIMARY KEY, deleted_at TEXT);
        CREATE TABLE agent_conversation_runs(
            conversation_id TEXT, run_id TEXT PRIMARY KEY, case_id TEXT, status TEXT, body TEXT);
        CREATE TABLE history_cleanup_jobs(cleanup_id TEXT PRIMARY KEY, manifest TEXT);
    """)
    repository = _Repository(connection)
    history = object.__new__(HistoryRetentionService)
    history.repository = repository
    history._processing = threading.Lock()
    history._retry_paths = lambda **kwargs: (False, [])
    history.web = WebResourceRetention(history)
    service = object.__new__(RetentionService)
    service._stop, service._wake, service._lock = threading.Event(), threading.Event(), threading.Lock()
    service._capacity_targets = {}
    service.cleaner = SimpleNamespace(_layout=SimpleNamespace(data_root=tmp_path))
    service.history = history
    service._run_once = lambda: None
    state = SimpleNamespace(free=0, target=1, attempts=[], busy=set(), failures=set(), after=None, reads=[])

    def snapshot(path):
        state.reads.append(state.free)
        return SimpleNamespace(free=state.free, target=state.target, under_pressure=state.free < state.target)

    service.capacity = SimpleNamespace(snapshot=snapshot)

    def expire(cid, rid, case_id, *, reason):
        assert reason == "DISK_PRESSURE"
        state.attempts.append(rid)
        if rid in state.busy:
            return False
        if rid in state.failures:
            raise OSError("physical deletion failed")
        connection.execute("UPDATE agent_conversation_runs SET body=json_set(body,'$.resources_expired',1) WHERE run_id=?", (rid,))
        state.free += 1
        if state.after is not None:
            state.after()
        return True

    history.web.expire_run = expire

    def seed(count, *, same_instant=False):
        ids = [str(uuid.UUID(int=index + 1)) for index in range(count)]
        for index, rid in reversed(list(enumerate(ids))):
            # Distinct encodings of the same instant must share the run-id tie breaker.
            timestamp = ("2026-10-01T08:00:00+08:00" if same_instant and index % 2
                else "2026-10-01T00:00:00.000Z")
            connection.execute("INSERT INTO agent_conversations VALUES (?,NULL)", (rid,))
            connection.execute("INSERT INTO agent_conversation_runs VALUES (?,?,NULL,'COMPLETED',?)",
                (rid, rid, json.dumps({"completed_at": timestamp, "resources_expired": False})))
        state.target = count
        return ids

    yield SimpleNamespace(service=service, history=history, repository=repository,
        state=state, seed=seed, connection=connection)
    connection.close()


def test_pressure_fetches_bounded_batches_instead_of_rescanning_for_each_run(pressure):
    ids = pressure.seed(397)
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids
    assert pressure.state.free == len(ids)
    assert len(pressure.repository.queries) <= 4
    assert max(pressure.repository.batches) <= 128
    assert all("LIMIT" in sql for sql in pressure.repository.queries)
    # Check actual disk space between every physical deletion, not only per batch.
    assert set(range(1, len(ids) + 1)).issubset(pressure.state.reads)


def test_candidate_iterator_does_not_fetch_all_rows_before_first_yield(pressure):
    ids = pressure.seed(397)
    candidates = iter(pressure.history.web.candidates())
    assert next(candidates) == (ids[0], ids[0], None)
    assert pressure.repository.batches == [128]


def test_equal_instants_with_different_offsets_keep_run_id_order_across_batches(pressure):
    ids = pressure.seed(397, same_instant=True)
    assert [row[1] for row in pressure.history.web.candidates()] == ids
    assert max(pressure.repository.batches) <= 128
    assert len(pressure.repository.queries) <= 4


def test_busy_runs_are_skipped_once_and_revisited_next_pressure_session(pressure):
    ids = pressure.seed(397)
    pressure.state.busy.update((ids[0], ids[130]))
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids
    assert pressure.state.free == len(ids) - 2
    assert len(pressure.repository.queries) <= 4
    pressure.state.busy.clear()
    pressure.state.attempts.clear()
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == [ids[0], ids[130]]
    assert pressure.state.free == len(ids)


def test_failed_physical_delete_is_not_counted_as_released_space(pressure):
    ids = pressure.seed(397)
    pressure.state.failures.add(ids[0])
    pressure.state.target = 2
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids[:3]
    assert pressure.state.free == 2
    assert len(pressure.repository.queries) == 1


@pytest.mark.parametrize("stop", [False, True])
def test_target_or_shutdown_stops_before_fetching_another_batch(pressure, stop):
    ids = pressure.seed(397)
    if stop:
        pressure.state.after = pressure.service._stop.set
    else:
        pressure.state.target = 1
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids[:1]
    assert pressure.repository.batches == [128]


def test_all_busy_candidates_terminate_without_repeated_attempts(pressure):
    ids = pressure.seed(397)
    pressure.state.busy.update(ids)
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids
    assert pressure.state.free == 0
    assert len(pressure.repository.queries) <= 4


def test_default_reclaim_limit_still_releases_only_one_run(pressure):
    ids = pressure.seed(397)
    assert pressure.history.reclaim_web_resources()
    assert pressure.state.attempts == ids[:1]
    assert pressure.state.free == 1


def test_pending_deletion_retries_stop_at_watermark_before_new_candidates(pressure):
    pressure.seed(397)
    pressure.state.target = 1
    for key in ("first", "second"):
        pressure.connection.execute("INSERT INTO history_cleanup_jobs VALUES (?, '{}')", (key,))
    retries = []

    def retry(cleanup_id, raw):
        retries.append(cleanup_id)
        pressure.connection.execute("DELETE FROM history_cleanup_jobs WHERE cleanup_id=?", (cleanup_id,))
        pressure.state.free += 1
        return True

    pressure.history._retry_path = retry
    pressure.history._retry_paths = lambda **kwargs: HistoryRetentionService._retry_paths(pressure.history, **kwargs)
    pressure.service._reclaim_capacity(precleaned=True)
    assert retries == ["first"]
    assert pressure.state.attempts == []
    assert pressure.repository.queries == []
    assert pressure.connection.execute("SELECT cleanup_id FROM history_cleanup_jobs").fetchall() == [("second",)]


def test_failed_pending_deletion_is_tried_once_before_new_candidates(pressure):
    ids = pressure.seed(397)
    pressure.state.target = 3
    pressure.connection.execute("INSERT INTO history_cleanup_jobs VALUES ('failed', '{}')")
    retries = []

    def retry(cleanup_id, raw):
        assert pressure.state.attempts == []
        retries.append(cleanup_id)
        raise OSError("quarantine filesystem unavailable")

    pressure.history._retry_path = retry
    pressure.history._retry_paths = lambda **kwargs: HistoryRetentionService._retry_paths(pressure.history, **kwargs)
    pressure.service._reclaim_capacity(precleaned=True)
    assert retries == ["failed"]
    assert pressure.state.attempts == ids[:3]
    assert pressure.connection.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 1


def test_free_space_is_rechecked_even_after_a_deletion_error(pressure):
    ids = pressure.seed(397)

    def failed_delete(cid, rid, case_id, *, reason):
        pressure.state.attempts.append(rid)
        # A partly completed unlink or another writer may change real free space.
        pressure.state.free = pressure.state.target
        raise OSError("cleanup receipt remains pending")

    pressure.history.web.expire_run = failed_delete
    pressure.service._reclaim_capacity(precleaned=True)
    assert pressure.state.attempts == ids[:1]
    assert pressure.state.reads[-1] == pressure.state.target
    assert pressure.repository.batches == [128]


def test_existing_watermark_prevents_even_the_first_candidate_query(pressure):
    pressure.seed(397)
    pressure.state.free = pressure.state.target
    assert not pressure.history.reclaim_web_resources(limit=None,
        should_continue=lambda: pressure.state.free < pressure.state.target)
    assert pressure.repository.queries == []


def test_physical_delete_failure_keeps_receipt_and_retries_before_next_candidate_pass(resource_stack, monkeypatch):
    stack = resource_stack
    runs = []
    for index in range(2):
        created = stack.store.create_conversation(f"pressure-{index}")
        cid, rid = created.conversation_id, created.run_id
        aid, path = upload(stack, cid)
        stack.store.submit_message(cid, "question", "正文应永久保留", [aid])
        _close_run(stack.store, cid, rid)
        runs.append((rid, cid, aid, path))
    failed, successful = sorted(runs)
    state = {"freed": 0, "fail": True}
    delete = stack.quarantine.delete
    candidate_passes = []
    candidates = stack.history.web.candidates

    def observe_candidates(*args, **kwargs):
        candidate_passes.append(1)
        yield from candidates(*args, **kwargs)

    def delete_or_fail(path):
        if failed[2] in str(path) and state["fail"]:
            raise OSError("injected physical unlink failure")
        delete(path)
        state["freed"] += 1

    monkeypatch.setattr(stack.history.web, "candidates", observe_candidates)
    monkeypatch.setattr(stack.quarantine, "delete", delete_or_fail)
    assert stack.history.reclaim_web_resources(limit=None, should_continue=lambda: state["freed"] < 1)
    assert state["freed"] == 1 and not successful[3].exists()
    with stack.repository.database_read() as db:
        receipt = db.execute("SELECT cleanup_id,manifest FROM history_cleanup_jobs").fetchall()
    assert len(receipt) == 1 and json.loads(receipt[0][1])["run_id"] == failed[0]
    assert any(path.name == "payload" for path in stack.repository.layout.quarantine.rglob("payload"))
    assert stack.store.get_run(failed[1], failed[0])["resources_expired"]
    state["fail"] = False
    assert stack.history.reclaim_web_resources(limit=None, should_continue=lambda: state["freed"] < 2)
    assert state["freed"] == 2 and candidate_passes == [1]
    with stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 0
