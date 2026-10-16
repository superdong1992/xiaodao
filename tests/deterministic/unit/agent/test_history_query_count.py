"""History pages hydrate a bounded batch without per-entry SQL round trips."""
from __future__ import annotations

import json
import uuid

import pytest

from problem_locator.agent.models import AgentMessage, AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.storage.database import dialect_for
from tests.deterministic.unit.storage.test_state_repository import _open


@pytest.fixture
def store(tmp_path):
    repository = _open(tmp_path)
    yield AgentStore(repository, runtime_epoch="history-query-count")
    repository.close()


def seed_history(store, kinds, *, request="history"):
    """Build valid history rows in one transaction; no slow service setup."""
    created = store.create_conversation(request, owner_key="owner-" + request)
    cid, expected = created.conversation_id, []
    with store.repository.database_transaction() as db:
        body = store._load(db, cid)
        for index, kind in enumerate(kinds):
            if body["status"] == "FAILED":
                body = store._new_run(db, body)
            label = f"正文 {index}"
            if kind == "message.accepted":
                message = AgentMessage(message_id=str(uuid.uuid4()), request_id=f"message-{index}",
                    text=label, attachment_ids=[], status="APPLIED", created_at=store._now(), run_id=body["run_id"])
                event = store._append(db, body, kind, message.model_dump(mode="json"), str(index))
                db.execute("INSERT INTO agent_messages(message_id,conversation_id,request_id,fingerprint,body,receipt,run_id,event_sequence) VALUES (?,?,?,?,?,?,?,?)",
                    (message.message_id, cid, message.request_id, "fixture", message.model_dump_json(), "{}", body["run_id"], event.sequence))
            elif kind == "conversation.completed":
                body.update(status="FAILED", case_status="FAILED",
                    failure={"code": "HISTORY_FIXTURE", "message": label, "retryable": False})
                event = store._append(db, body, kind, {"status": "FAILED"}, str(index))
            elif kind == "assistant.question":
                event = store._append(db, body, kind, {"questions": [label]}, str(index))
            else:
                event = store._append(db, body, kind,
                    {"status": "RESOLVED", "artifacts": [], "result_field": "generic_result"}, str(index))
            store._save(db, body)
            expected.append((str(event.sequence), body["run_id"], kind, label))
    return cid, expected


def read_history(store, monkeypatch, cid, *, limit=50, cursor=None):
    statements = []
    with store.repository.database_read() as db:
        if dialect_for(db).postgres:
            original = db.execute

            def execute(statement, values=None):
                statements.append(statement)
                return original(statement, values)

            with monkeypatch.context() as patch:
                patch.setattr(db, "execute", execute)
                result = store._history(db, cid, cursor, limit)
        else:
            db.set_trace_callback(statements.append)
            try:
                result = store._history(db, cid, cursor, limit)
            finally:
                db.set_trace_callback(None)
    return result, [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]


@pytest.mark.parametrize("kinds", [
    ["message.accepted"] * 101,
    ["conversation.completed"] * 101,
    ["message.accepted", "conversation.completed"] * 51,
], ids=["messages", "terminal-runs", "mixed"])
def test_history_sql_count_is_bounded_for_one_fifty_and_hundred_entries(store, monkeypatch, kinds):
    cid, expected = seed_history(store, kinds)
    counts = []
    for limit in (1, 50, 100):
        (page, cursor), queries = read_history(store, monkeypatch, cid, limit=limit)
        counts.append(len(queries))
        assert [(entry.id, entry.run_id) for entry in page] == [row[:2] for row in expected[-limit:]]
        assert store._decode_cursor(cursor) == [cid, int(expected[-limit][0])]
    assert max(counts) <= 3, f"History SELECT counts for page sizes 1/50/100: {counts}"


def test_batched_history_preserves_mixed_pages_and_conversation_boundaries(store, monkeypatch):
    kinds = ["message.accepted", "assistant.question", "conversation.completed", "result.available"] * 3
    cid, expected = seed_history(store, kinds)
    foreign, _ = seed_history(store, kinds, request="foreign")
    cursor, captured = None, []
    while True:
        (page, cursor), queries = read_history(store, monkeypatch, cid, limit=5, cursor=cursor)
        assert len(queries) <= 3
        captured[:0] = page
        if cursor is None:
            break
    assert [(entry.id, entry.run_id) for entry in captured] == [row[:2] for row in expected]
    for entry, (_, _, kind, label) in zip(captured, expected):
        if kind == "message.accepted":
            assert entry.message.text == label and entry.message.status == "APPLIED"
        elif kind == "assistant.question":
            assert entry.questions == [label]
        elif kind == "conversation.completed":
            assert entry.result.status == entry.result.case_status == "FAILED"
            assert entry.result.failure.message == label
        else:
            assert entry.result.status == "COMPLETED" and entry.result.report_state == "READY"
            assert entry.result.source_job_id is None
    with pytest.raises(AgentStoreError) as error:
        read_history(store, monkeypatch, foreign, cursor=store._cursor([cid, 2]))
    assert error.value.code == "AGENT_INVALID_CURSOR"
    with pytest.raises(AgentStoreError):
        store.require_owner(cid, "owner-foreign")


@pytest.mark.parametrize("kind", ["message.accepted", "conversation.completed"])
@pytest.mark.parametrize("foreign", [False, True], ids=["missing", "other-conversation"])
def test_batched_history_never_substitutes_missing_or_foreign_dependencies(store, monkeypatch, kind, foreign):
    cid, _ = seed_history(store, [kind])
    other, _ = seed_history(store, [], request="other")
    table = "agent_messages" if kind == "message.accepted" else "agent_conversation_runs"
    with store.repository.database_transaction() as db:
        if foreign:
            tail = ",ordinal=999" if kind == "conversation.completed" else ""
            db.execute(f"UPDATE {table} SET conversation_id=?{tail} WHERE conversation_id=?", (other, cid))
        else:
            db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (cid,))
    # The existing contract fails closed when an event's required row is absent.
    with pytest.raises(TypeError):
        read_history(store, monkeypatch, cid)


def test_history_does_not_hydrate_the_extra_cursor_row_or_unreferenced_messages(store, monkeypatch):
    cid, _ = seed_history(store, ["message.accepted", "assistant.question"])
    with store.repository.database_transaction() as db:
        db.execute("UPDATE agent_messages SET body=? WHERE conversation_id=?", ("invalid JSON", cid))
    (page, cursor), queries = read_history(store, monkeypatch, cid, limit=1)
    assert page[0].questions == ["正文 1"] and cursor is not None
    assert len(queries) == 1
    with pytest.raises(json.JSONDecodeError):
        read_history(store, monkeypatch, cid, limit=1, cursor=cursor)


def test_empty_history_uses_one_query(store, monkeypatch):
    cid, _ = seed_history(store, [])
    (page, cursor), queries = read_history(store, monkeypatch, cid)
    assert page == [] and cursor is None and len(queries) == 1
