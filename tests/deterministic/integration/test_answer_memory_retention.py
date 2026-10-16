"""Permanent answer feedback must coexist with disposable learning resources."""
from __future__ import annotations

import pytest

from problem_locator.entrypoints.postgres_import import _import_source_rows
from problem_locator.memory import store as memory_module
from problem_locator.memory.store import MemoryStore
from problem_locator.storage.database import table_columns
from tests.deterministic.unit.agent.test_answer_feedback_memory import (
    EVIDENCE, FID, OTHER_FID, answer_source, answer_vote, enqueue,
)
from tests.deterministic.unit.agent.test_generic_feedback import (
    CARD, OWNER, _expire_resources, put, system,
)


@pytest.mark.parametrize("accepted", [False, True])
def test_expired_correction_keeps_one_shot_and_replacement_receipt_without_using_task_quota(
    system, monkeypatch, accepted,
):
    put(system, rating="DISLIKE")
    correction = answer_source(system, correction=True)
    assert enqueue(system, correction)
    task = system.memory.claim_task()
    if accepted:
        assert system.memory.finish_task(task["task_id"], CARD, evidence_json=EVIDENCE)
    else:
        assert system.memory.fail_task(task["task_id"])
    _expire_resources(system)
    system.clock.value = "2026-12-30T00:00:00.000Z"
    system.memory.prune()
    with system.repository.database_read() as db:
        assert db.execute("SELECT problem_text,report_markdown,context_json,card_json,evidence_json "
            "FROM memory_tasks WHERE answer_key=?", (FID,)).fetchone() == (
                None, None, None, None, EVIDENCE if accepted else None)
    assert not enqueue(system, correction)
    assert system.memory.get_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, can_rate=False).rating == "DISLIKE"
    if accepted:
        put(system, "old-answer-up")
        assert system.memory.claim_task() is None
    # The permanent correction receipt must not exhaust live task capacity.
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    new_answer = answer_source(system, fid=OTHER_FID)
    answer_vote(system, new_answer, "new-answer")
    latest = system.memory.claim_task()
    assert latest is not None and latest["answer_key"] == OTHER_FID
    assert not enqueue(system, correction)
    assert system.memory.active_cards(system.source.skill_name) == []


def test_answer_scoped_extraction_receipts_survive_upgrade_import_projection_and_task_expiry(system):
    first, second = answer_source(system), answer_source(system, fid=OTHER_FID)
    answer_vote(system, first)
    system.memory.put_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, request_id="second-disabled", rating="LIKE", source=second,
        answer_key=OTHER_FID, extract_memory=False)
    assert first.report_sha256 == second.report_sha256
    with system.repository.database_transaction() as db:
        db.execute("ALTER TABLE memory_feedback DROP COLUMN extraction_attempted")
        columns = [row[1] for row in db.execute("PRAGMA table_info(memory_feedback)")]
        derived, defaults, rows = _import_source_rows(db, "memory_feedback", columns,
            set(columns) | {"extraction_attempted"})
        assert derived == ["extraction_attempted"] and defaults == {}
        projected = {row[1 + columns.index("answer_key")]: row[-1] for row in rows}
        assert projected == {FID: 1, OTHER_FID: 0}
        assert "extraction_attempted" not in table_columns(db, "memory_feedback")
    system.memory = MemoryStore(system.repository, system.clock)
    with system.repository.database_read() as db:
        assert dict(db.execute("SELECT answer_key,extraction_attempted FROM memory_feedback")) == projected
    system.clock.value = "2026-09-28T00:00:00.000Z"
    system.memory.prune()
    answer_vote(system, first, "first-after-expiry")
    assert system.memory.claim_task() is None
    answer_vote(system, second, "second-enabled")
    task = system.memory.claim_task()
    assert task is not None and task["answer_key"] == OTHER_FID
