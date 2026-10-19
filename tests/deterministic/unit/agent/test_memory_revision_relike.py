"""Reliking an already reviewed correction publishes its historical relation once."""
from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from problem_locator.memory.retrieval import ExperienceRetriever
from tests.deterministic.unit.agent.test_generic_feedback import OWNER, system
from tests.deterministic.unit.agent.test_memory_revision_store import FIXED, evidence, ready, target


def inactive_review(state, relation):
    selected = ready(state)
    created, original, _ = target(state, selected)
    correction = replace(original, answer_key=str(uuid.uuid4()), source_kind="CORRECTION",
        replaces_answer_key="")
    with state.repository.database_transaction() as db:
        assert state.memory.enqueue_correction_in_transaction(db,
            created.conversation_id, created.run_id, correction)
    task = state.memory.claim_task()
    vote = dict(conversation_id=created.conversation_id, run_id=created.run_id, owner_key=OWNER,
        source=correction, answer_key=correction.answer_key)
    state.memory.put_feedback(**vote, request_id="correction-dislike", rating="DISLIKE")
    assert state.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected, relation))
    assert state.memory.is_card_active(original.skill_name, selected)
    assert state.memory.claim_task() is None
    with state.repository.database_read() as db:
        assert db.execute("SELECT active,status FROM memory_tasks WHERE task_id=?",
            (task["task_id"],)).fetchone() == (0, "READY")
    return selected, task, vote


@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE", "DIAGNOSIS_OMISSION"])
def test_relike_publishes_inactive_correction_relation_once_without_new_learning(system, relation):
    selected, task, vote = inactive_review(system, relation)
    first = system.memory.put_feedback(**vote, request_id="correction-relike", rating="LIKE")
    assert system.memory.put_feedback(**vote, request_id="correction-relike", rating="LIKE") == first
    assert system.memory.put_feedback(**vote, request_id="correction-like-again", rating="LIKE") == first
    assert system.memory.claim_task() is None
    with system.repository.database_read() as db:
        assert db.execute("SELECT relation,old_card_id,old_card_sha256 FROM memory_revisions WHERE correction_task_id=?",
            (task["task_id"],)).fetchall() == [(relation, selected.card_id, selected.card_sha256)]
    retriever = ExperienceRetriever(system.memory)
    assert retriever.select(vote["source"].skill_name, "请求排队服务繁忙").card_json == FIXED
    assert system.memory.is_card_active(vote["source"].skill_name, selected) is (relation != "GLOBAL_ERROR")
    if relation != "GLOBAL_ERROR":
        assert retriever.select(vote["source"].skill_name, "请求排队").card_id == selected.card_id

@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE", "DIAGNOSIS_OMISSION"])
def test_deleted_historical_source_cannot_be_reactivated_by_later_correction_like(system, relation):
    _, task, vote = inactive_review(system, relation)
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    result = system.memory.put_feedback(**vote, request_id="late-relike", rating="LIKE")
    assert result.rating == "LIKE"
    with system.repository.database_read() as db:
        assert db.execute("SELECT active FROM memory_tasks WHERE task_id=?", (task["task_id"],)).fetchone() == (0,)
        assert db.execute("SELECT published FROM memory_revisions WHERE correction_task_id=?",
            (task["task_id"],)).fetchone() == (0,)
    assert system.memory.active_cards(vote["source"].skill_name) == []


def test_superseded_inactive_correction_cannot_publish_its_old_relation_on_relike(system):
    selected, task, vote = inactive_review(system, "GLOBAL_ERROR")
    source = replace(vote["source"], answer_key=str(uuid.uuid4()), replaces_answer_key=vote["source"].answer_key)
    with system.repository.database_transaction() as db:
        assert system.memory.enqueue_correction_in_transaction(db, vote["conversation_id"], vote["run_id"], source)
    newer = system.memory.claim_task()
    assert system.memory.finish_task(newer["task_id"], FIXED, evidence_json='{"proof":true}')
    assert system.memory.put_feedback(**vote, request_id="superseded-relike", rating="LIKE").rating == "LIKE"
    assert system.memory.is_card_active(source.skill_name, selected)
    with system.repository.database_read() as db:
        assert db.execute("SELECT active,superseded FROM memory_tasks WHERE task_id=?",
            (task["task_id"],)).fetchone() == (0, 1)
        assert db.execute("SELECT published FROM memory_revisions WHERE correction_task_id=?",
            (task["task_id"],)).fetchone() == (0,)


def test_deleting_correction_source_discards_unpublished_relation(system):
    from problem_locator.agent.models import AgentStoreError
    selected, task, vote = inactive_review(system, "GLOBAL_ERROR")
    system.agent_store.request_delete(vote["conversation_id"], owner_key=OWNER)
    with pytest.raises(AgentStoreError):
        system.memory.put_feedback(**vote, request_id="deleted-relike", rating="LIKE")
    assert system.memory.is_card_active(vote["source"].skill_name, selected)
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_revisions WHERE correction_task_id=?",
            (task["task_id"],)).fetchone() == (0,)


@pytest.mark.parametrize("relation", ["GLOBAL_ERROR", "NOT_APPLICABLE"])
def test_repeated_vote_switch_reuses_published_revision_and_global_rejection_stays(system, relation):
    selected, task, vote = inactive_review(system, relation)
    system.memory.put_feedback(**vote, request_id="first-publish", rating="LIKE")
    system.memory.put_feedback(**vote, request_id="disable-published", rating="DISLIKE")
    assert system.memory.is_card_active(vote["source"].skill_name, selected) is (relation != "GLOBAL_ERROR")
    system.memory.put_feedback(**vote, request_id="republish", rating="LIKE")
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*),sum(published) FROM memory_revisions WHERE correction_task_id=?",
            (task["task_id"],)).fetchone() == (1, 1)
    assert system.memory.claim_task() is None
