"""Pending corrections must not multiply recall work for duplicate card contents."""
from __future__ import annotations

import sqlite3

import pytest

from problem_locator.memory.store import MemoryStore, _SCHEMA
from problem_locator.memory.usage import initialize_usage

DIGEST = "a" * 64
STAMP = "2026-10-19T00:00:00Z"
CARD = '{"applicability":["连接池繁忙"],"limitations":["需用当前证据核实"],"problem_features":["请求排队"],"steps":["核对连接等待时间"]}'


@pytest.fixture
def database(tmp_path):
    db = sqlite3.connect(tmp_path / "memory-index.sqlite3")
    db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute(_SCHEMA[2])
    initialize_usage(db)
    db.executemany("INSERT INTO memory_tasks(task_id,conversation_id,run_id,source_job_id,skill_name,report_sha256,status,card_json,card_sha256,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(f"card-{index}", "conversation", f"run-{index}", "job", "generic", "report", "READY", CARD,
            DIGEST, 1, STAMP, STAMP) for index in range(128)])
    yield db
    db.close()


def revision(db, identity, *, published=0, skill="generic", digest=DIGEST, relation="GLOBAL_ERROR"):
    db.execute("INSERT INTO memory_revisions(correction_task_id,skill_name,old_card_id,old_card_sha256,relation,scope_terms_json,published) VALUES(?,?,?,?,?,?,?)",
        (identity, skill, "historical-card", digest, relation, "[]", published))


def projection(db):
    steps = 0
    def count():
        nonlocal steps
        steps += 1
        return 0
    # VM work measures query complexity, not machine load or filesystem speed.
    db.set_progress_handler(count, 1)
    try:
        visible = db.execute("SELECT count(*) FROM memory_tasks t " + MemoryStore._CARD_WHERE,
            ("generic",)).fetchone()[0]
    finally:
        db.set_progress_handler(None, 0)
    return visible, steps


def test_pending_same_content_corrections_do_not_multiply_recall_scan_work(database):
    visible, baseline_steps = projection(database)
    assert visible == 128
    for index in range(256):
        revision(database, f"pending-{index}")
    visible, pending_steps = projection(database)
    assert visible == 128
    assert pending_steps < baseline_steps * 3
    revision(database, "adopted", published=1)
    visible, published_steps = projection(database)
    assert visible == 0
    assert published_steps < baseline_steps * 3


def test_only_published_global_rejection_for_same_skill_and_content_hides_cards(database):
    revision(database, "pending")
    revision(database, "other-skill", published=1, skill="other")
    revision(database, "other-content", published=1, digest="b" * 64)
    revision(database, "scoped", published=1, relation="NOT_APPLICABLE")
    assert projection(database)[0] == 128
    database.execute("UPDATE memory_revisions SET published=1 WHERE correction_task_id='pending'")
    assert projection(database)[0] == 0
