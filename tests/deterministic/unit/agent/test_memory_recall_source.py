"""Fresh source checks fence cached content from later votes and deletion."""
import pytest

from problem_locator.memory import store as memory_module
from problem_locator.memory.retrieval import ExperienceRetriever
from tests.deterministic.unit.agent.test_generic_feedback import CARD, OWNER, put, system
from tests.deterministic.unit.agent.test_permanent_memory import new_report, vote


def ready(system):
    put(system)
    task = system.memory.claim_task()
    assert system.memory.finish_task(task["task_id"], CARD)
    selected = ExperienceRetriever(system.memory).select(system.source.skill_name, "请求排队服务繁忙")
    assert selected is not None
    return task, selected


@pytest.mark.parametrize("column,value", [
    ("active", 0), ("superseded", 1), ("status", "FAILED"),
    ("skill_name", "other-skill"), ("card_json", "{}"),
    ("card_sha256", "0" * 64), ("source_kind", "FOLLOWUP"),
])
def test_final_source_check_requires_current_state_and_exact_content(system, column, value):
    task, selected = ready(system)
    assert system.memory.is_card_active(system.source.skill_name, selected)
    assert not system.memory.is_card_active("other-skill", selected)
    with system.repository.database_transaction() as db:
        db.execute(f"UPDATE memory_tasks SET {column}=? WHERE task_id=?", (value, task["task_id"]))
    assert not system.memory.is_card_active(system.source.skill_name, selected)


def test_final_source_check_observes_vote_switches_and_explicit_delete(system):
    _, selected = ready(system)
    put(system, "down", "DISLIKE")
    assert not system.memory.is_card_active(system.source.skill_name, selected)
    put(system, "up")
    assert system.memory.is_card_active(system.source.skill_name, selected)
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert not system.memory.is_card_active(system.source.skill_name, selected)


@pytest.mark.parametrize("action", ["DISLIKE", "DELETE"])
def test_source_revoked_between_pages_is_not_returned_from_cached_content(system, monkeypatch, action):
    task, _ = ready(system)
    created, source = new_report(system, 1)
    vote(system, created, source, "other-like")
    other = system.memory.claim_task()
    assert system.memory.finish_task(other["task_id"], CARD)
    sources = {task["task_id"]: (system.created, system.source), other["task_id"]: (created, source)}
    target = min(sources)
    remaining = max(sources)
    with system.repository.database_transaction() as db:
        db.execute("UPDATE memory_tasks SET updated_at=? WHERE task_id=?", ("2026-09-22T00:00:00Z", target))
    monkeypatch.setattr(memory_module, "CARD_SCAN_BATCH_SIZE", 1)
    retriever = ExperienceRetriever(system.memory)
    assert retriever.select(source.skill_name, "请求排队服务繁忙").card_id == target
    original = system.memory.iter_active_cards
    def revoke_between_pages(skill_name):
        for number, row in enumerate(original(skill_name), 1):
            yield row
            if number == 1:
                victim, victim_source = sources[target]
                if action == "DELETE":
                    system.agent_store.request_delete(victim.conversation_id, owner_key=OWNER)
                else:
                    system.memory.put_feedback(victim.conversation_id, victim.run_id, owner_key=OWNER,
                        request_id="down-during-scan", rating="DISLIKE", source=victim_source)
    monkeypatch.setattr(system.memory, "iter_active_cards", revoke_between_pages)
    assert retriever.select(source.skill_name, "请求排队服务繁忙") is None
    monkeypatch.setattr(system.memory, "iter_active_cards", original)
    assert retriever.select(source.skill_name, "请求排队服务繁忙").card_id == remaining
