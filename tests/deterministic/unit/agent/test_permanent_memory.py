"""Long-lived cards keep independent sources without consuming worker slots."""
from dataclasses import replace
import json
import uuid

import pytest

from problem_locator.memory import store as memory_module
from problem_locator.memory.extraction import parse_card_json
from problem_locator.memory.retrieval import ExperienceRetriever
from problem_locator.memory.store import MemoryStore
from tests.deterministic.unit.agent.test_generic_feedback import (
    CARD, OWNER, put, system, _expire_resources,
)
from tests.deterministic.unit.agent.test_answer_feedback_memory import (
    EVIDENCE, FID, answer_source, answer_vote, enqueue,
)


def new_report(state, number):
    created = state.agent_store.create_conversation(f"report-{number}", owner_key=OWNER)
    source = replace(state.source, case_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"report-{number}")))
    with state.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (source.case_id, created.run_id))
    return created, source


def vote(state, created, source, request):
    return state.memory.put_feedback(created.conversation_id, created.run_id,
        owner_key=OWNER, request_id=request, rating="LIKE", source=source)


@pytest.mark.parametrize("future", ["2026-12-20T00:00:00.000Z", "2027-09-21T08:00:00.000+08:00", "2036-09-21T00:00:00.000Z"])
def test_card_survives_old_ttl_cleanup_restart_and_vote_switch(system, future):
    put(system)
    task = system.memory.claim_task()
    assert system.memory.finish_task(task["task_id"], CARD)
    before = system.memory.active_cards(system.source.skill_name)
    system.clock.value = future
    assert system.memory.prune()["expired_cards"] == 0
    system.memory = MemoryStore(system.repository, system.clock)
    assert system.memory.active_cards(system.source.skill_name) == before
    assert list(system.memory.iter_active_cards(system.source.skill_name)) == before
    put(system, "down-later", "DISLIKE")
    assert system.memory.active_cards(system.source.skill_name) == []
    put(system, "up-later")
    assert system.memory.active_cards(system.source.skill_name)[0]["card_id"] == task["task_id"]
    assert system.memory.claim_task() is None


@pytest.mark.parametrize("terminal", ["READY", "DISLIKED", "SUPERSEDED", "FAILED", "DELETED"])
def test_terminal_history_immediately_releases_worker_capacity(system, monkeypatch, terminal):
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    put(system)
    task = system.memory.claim_task()
    if terminal in {"READY", "DISLIKED", "SUPERSEDED"}:
        system.memory.finish_task(task["task_id"], CARD)
        if terminal == "DISLIKED":
            put(system, "down", "DISLIKE")
        elif terminal == "SUPERSEDED":
            with system.repository.database_transaction() as db:
                db.execute("UPDATE memory_tasks SET superseded=1,active=0 WHERE task_id=?", (task["task_id"],))
    elif terminal == "FAILED":
        system.memory.fail_task(task["task_id"])
    else:
        system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    created, source = new_report(system, 1)
    assert vote(system, created, source, "next-up").rating == "LIKE"
    new = system.memory.claim_task()
    assert new is not None and new["run_id"] == created.run_id
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks WHERE status IN ('PENDING','RUNNING')").fetchone()[0] == 1


@pytest.mark.parametrize("running", [False, True])
def test_pending_and_running_keep_capacity_until_finished(system, monkeypatch, running):
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    put(system)
    task = system.memory.claim_task() if running else None
    created, source = new_report(system, 1)
    assert vote(system, created, source, "while-full").rating == "LIKE"
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks").fetchone()[0] == 1
    task = task or system.memory.claim_task()
    assert system.memory.finish_task(task["task_id"], CARD)
    # Replaying an already acknowledged vote is not another learning request.
    vote(system, created, source, "while-full")
    assert system.memory.claim_task() is None
    vote(system, created, source, "capacity-available")
    assert system.memory.claim_task()["run_id"] == created.run_id


def test_old_accepted_correction_still_replaces_original_and_can_be_disabled(system):
    put(system)
    old = system.memory.claim_task()
    system.memory.finish_task(old["task_id"], CARD)
    correction = answer_source(system, correction=True)
    assert enqueue(system, correction)
    new = system.memory.claim_task()
    assert system.memory.finish_task(new["task_id"], CARD, evidence_json=EVIDENCE)
    system.clock.value = "2036-09-21T00:00:00.000Z"
    system.memory.prune()
    put(system, "old-revote")
    assert [r["answer_key"] for r in system.memory.active_cards(system.source.skill_name)] == [FID]
    answer_vote(system, correction, "correction-down", "DISLIKE")
    assert system.memory.active_cards(system.source.skill_name) == []
    put(system, "old-revote-again")
    assert system.memory.active_cards(system.source.skill_name) == []
    answer_vote(system, correction, "correction-up")
    assert [r["answer_key"] for r in system.memory.active_cards(system.source.skill_name)] == [FID]
    assert system.memory.claim_task() is None


def test_resource_expiry_keeps_permanent_card_but_explicit_delete_revokes_it(system):
    put(system)
    task = system.memory.claim_task()
    system.memory.finish_task(task["task_id"], CARD)
    _expire_resources(system)
    system.clock.value = "2036-09-21T00:00:00.000Z"
    system.memory.prune()
    assert [r["card_id"] for r in system.memory.active_cards(system.source.skill_name)] == [task["task_id"]]
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert list(system.memory.iter_active_cards(system.source.skill_name)) == []
    assert not system.memory.finish_task(task["task_id"], CARD)


def test_card_scan_passes_task_limit_and_page_boundaries_without_hiding_old_match(system, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    monkeypatch.setattr(memory_module, "CARD_SCAN_BATCH_SIZE", 2)
    expected = []
    special = json.dumps({"problem_features": ["ECONNREFUSED"], "applicability": ["连接建立阶段"],
        "steps": ["先核对目标进程监听状态"], "limitations": ["需结合本次证据确认"]}, ensure_ascii=False)
    for n in range(5):
        created, source = new_report(system, n)
        vote(system, created, source, f"up-{n}")
        task = system.memory.claim_task()
        assert task is not None
        system.memory.finish_task(task["task_id"], special if n == 0 else CARD)
        expected.append(task["task_id"])
    rows = list(system.memory.iter_active_cards(system.source.skill_name))
    assert [r["card_id"] for r in rows] == sorted(expected)
    assert len(system.memory.active_cards(system.source.skill_name)) == 5
    assert list(system.memory.iter_active_cards("other-skill")) == []
    selection = ExperienceRetriever(system.memory).select(system.source.skill_name, "ECONNREFUSED")
    assert selection is not None and selection.card_id == expected[0]
    assert selection.card_json == parse_card_json(special)


def test_identical_cards_keep_independent_vote_and_delete_sources(system):
    special = json.dumps({"problem_features": ["ECONNREFUSED"], "applicability": ["连接建立阶段"],
        "steps": ["核对目标进程监听状态"], "limitations": ["需结合本次证据确认"]}, ensure_ascii=False)
    put(system)
    first = system.memory.claim_task()
    system.memory.finish_task(first["task_id"], special)
    created, source = new_report(system, 1)
    vote(system, created, source, "other-source")
    second = system.memory.claim_task()
    system.memory.finish_task(second["task_id"], special)
    retriever = ExperienceRetriever(system.memory)
    assert retriever.select(system.source.skill_name, "ECONNREFUSED") is not None
    put(system, "down-first", "DISLIKE")
    assert retriever.select(system.source.skill_name, "ECONNREFUSED").card_id == second["task_id"]
    system.agent_store.request_delete(created.conversation_id, owner_key=OWNER)
    assert retriever.select(system.source.skill_name, "ECONNREFUSED") is None
    put(system, "first-back")
    assert retriever.select(system.source.skill_name, "ECONNREFUSED").card_id == first["task_id"]
