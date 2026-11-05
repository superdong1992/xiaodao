"""Report eligibility must not materialize unrelated run backgrounds."""
from __future__ import annotations

import json

import pytest

from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.agent.test_report_continuation import system


def test_head_does_not_read_or_decode_historical_backgrounds(system, monkeypatch):
    s = system
    marker = "historical-background-must-stay-in-database:"
    with s.repository.database_transaction() as db:
        previous = s.store._load(db, s.cid)
        for version in range(2, 5):
            body = s.store._new_run(db, previous)
            body.update(status="COMPLETED", report_available=True, root_run_id=body["run_id"],
                continuation_context={"followup_context_json": marker + "x" * 1024**2} if version < 4 else {})
            s.store._save(db, body)
            previous = body
        latest_id = previous["run_id"]

    original_loads = json.loads

    def guarded_loads(raw, *args, **kwargs):
        assert marker not in raw, "eligibility decoded an unrelated run's full background"
        return original_loads(raw, *args, **kwargs)

    statements = []
    with monkeypatch.context() as scoped:
        scoped.setattr(json, "loads", guarded_loads)
        with s.repository.database_read() as db:
            db.set_trace_callback(statements.append)
            try:
                current, parent = s.service._head(db, s.cid, s.rid)
            finally:
                db.set_trace_callback(None)
    assert current["run_id"] == latest_id and parent["run_id"] == s.rid
    assert not [sql for sql in statements
        if sql.startswith("SELECT body FROM agent_conversation_runs WHERE conversation_id=")
        and " AND run_id=" not in sql]


def test_head_loads_current_parent_once(system, monkeypatch):
    s = system
    original_load, loads = s.store._load, []

    def counted_load(db, cid, **kwargs):
        loads.append(kwargs.get("run_id"))
        return original_load(db, cid, **kwargs)

    monkeypatch.setattr(s.store, "_load", counted_load)
    with s.repository.database_read() as db:
        current, parent = s.service._head(db, s.cid, s.rid)
    assert current == parent and current["run_id"] == s.rid
    assert loads == [None]


@pytest.mark.parametrize("other_root,published,busy,expected", [
    ("same", True, False, "AGENT_REPORT_CHANGED"),
    ("other", True, False, None),
    ("same", False, False, None),
    ("same", False, True, "AGENT_DIAGNOSIS_BUSY"),
    ("same", True, True, "AGENT_REPORT_CHANGED"),
])
def test_supplied_runs_keep_version_and_busy_precedence(system, other_root, published, busy, expected):
    s = system
    parent = s.store.get_run(s.cid)
    assert "root_run_id" not in parent  # Older reports use their run ID as root.
    if busy:
        with s.repository.database_transaction() as db:
            current = s.store._new_run(db, s.store._load(db, s.cid))
            current.update(status="INTAKE", report_available=False)
            s.store._save(db, current)
    rows = [{"run_id": "other-run", "root_run_id": s.rid if other_root == "same" else "another-root",
        "ordinal": parent["ordinal"] + 1, "report_available": published}]
    statements = []
    with s.repository.database_read() as db:
        db.set_trace_callback(statements.append)
        try:
            if expected:
                with pytest.raises(AgentStoreError) as failure:
                    s.service._head(db, s.cid, s.rid, parent=parent, runs=iter(rows))
                assert failure.value.code == expected
            else:
                _, selected = s.service._head(db, s.cid, s.rid, parent=parent, runs=iter(rows))
                assert selected is parent
        finally:
            db.set_trace_callback(None)
    assert not [sql for sql in statements if "agent_conversation_runs" in sql and " AND run_id=" not in sql]
