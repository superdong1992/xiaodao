"""Version metadata reads must not repeatedly decode historical report bodies."""
from __future__ import annotations

import json

import pytest

from tests.deterministic.unit.agent.test_report_continuation import OWNER, system


def _published_versions(s, count):
    ids = [s.rid]
    report = "# 历史报告\n" + "x" * 65_536
    with s.repository.database_transaction() as db:
        previous = s.store._load(db, s.cid)
        for version in range(2, count + 1):
            body = s.store._new_run(db, previous)
            body.update(status="COMPLETED", report_available=True, parent_run_id=previous["run_id"],
                root_run_id=s.rid, report_version=version,
                continuation_context={"parent_report_markdown": report})
            s.store._save(db, body)
            ids.append(body["run_id"])
            previous = body
    return ids


def test_versions_scan_and_decode_large_historical_bodies_once(system, monkeypatch):
    s = system
    ids = _published_versions(s, 32)
    historical = set(ids[1:-1])
    decoded = {rid: 0 for rid in historical}
    original_loads = json.loads

    def loads(raw, *args, **kwargs):
        value = original_loads(raw, *args, **kwargs)
        if isinstance(value, dict) and value.get("run_id") in decoded:
            decoded[value["run_id"]] += 1
        return value

    monkeypatch.setattr("problem_locator.continuation.service.json.loads", loads)
    queries = []
    with s.repository.database_read() as db:
        db.set_trace_callback(queries.append)
    try:
        result = s.service.versions(s.cid, ids[-1], owner_key=OWNER)
    finally:
        with s.repository.database_read() as db:
            db.set_trace_callback(None)
    full_reads = [sql for sql in queries
        if sql.startswith("SELECT body FROM agent_conversation_runs WHERE conversation_id=")
        and " AND run_id=" not in sql]
    assert len(full_reads) == 1, full_reads
    assert set(decoded.values()) == {1}, decoded
    assert [item.version for item in result.versions] == list(range(1, 33))
    assert result.latest_run_id == ids[-1] and result.can_continue
    assert "parent_report_markdown" not in result.model_dump_json()


@pytest.mark.parametrize("selection,current_state,expected", [
    ("latest", "COMPLETED", True),
    ("old", "COMPLETED", False),
    ("latest", "INTAKE", False),
    ("latest", "FAILED", True),
])
def test_reusing_version_rows_preserves_continue_eligibility(system, selection, current_state, expected):
    s = system
    ids = _published_versions(s, 3)
    if current_state != "COMPLETED":
        with s.repository.database_transaction() as db:
            current = s.store._new_run(db, s.store._load(db, s.cid))
            current.update(status=current_state)
            s.store._save(db, current)
    selected = ids[-1] if selection == "latest" else ids[0]
    result = s.service.versions(s.cid, selected, owner_key=OWNER)
    assert result.can_continue is expected
    assert result.latest_run_id == ids[-1]
    assert [item.version for item in result.versions] == [1, 2, 3]
