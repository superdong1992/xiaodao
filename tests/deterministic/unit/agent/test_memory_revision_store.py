"""Verified feedback follows an actually used card across diagnostic runs."""
import hashlib
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from problem_locator.contracts import DiagnosisMode
from problem_locator.memory.extraction import parse_card_json
from problem_locator.memory.retrieval import ExperienceRetriever
from problem_locator.memory.store import MemoryStore
from tests.deterministic.unit.agent.test_generic_feedback import CARD, OWNER, put, system
from tests.deterministic.unit.agent.test_permanent_memory import new_report, vote

FIXED = parse_card_json(json.dumps({"problem_features": ["请求排队"], "applicability": ["服务繁忙"],
    "steps": ["先区分慢查询占用和连接未释放"], "limitations": ["不能仅凭排队判定连接泄漏"]}, ensure_ascii=False))


def ready(state):
    put(state)
    task = state.memory.claim_task()
    assert state.memory.finish_task(task["task_id"], CARD)
    return ExperienceRetriever(state.memory).select(state.source.skill_name, "请求排队服务繁忙")


def target(state, selection, number=1):
    created, source = new_report(state, number)
    source = replace(source, source_job_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"diagnosis-{number}")))
    job = SimpleNamespace(job_id=source.source_job_id, case_id=source.case_id,
        generic_skill_name=source.skill_name, diagnosis_mode=DiagnosisMode.GENERIC)
    state.memory.record_usage(job, selection)
    return created, source, job


def comment(state, created, source, number=1):
    state.memory.put_feedback(created.conversation_id, created.run_id, owner_key=OWNER,
        request_id=f"down-{number}", rating="DISLIKE", source=source)
    state.memory.post_feedback_comment(created.conversation_id, created.run_id, owner_key=OWNER,
        request_id=f"comment-{number}", text="不能仅凭排队判断连接泄漏", analysis_text="检查发现慢查询长期占用连接。",
        source=source)
    return state.memory.claim_task()


def evidence(selection, relation="GLOBAL_ERROR", **updates):
    review = {"card_id": selection.card_id, "card_sha256": selection.card_sha256,
        "relation": relation, "evidence_indexes": [0],
        "scope_terms": ["排队", "繁忙"] if relation in {"NOT_APPLICABLE", "DIAGNOSIS_OMISSION"} else []}
    review.update(updates)
    return json.dumps({"evidence": [{"sha256": "a" * 64, "start_line": 1, "end_line": 2}],
        "memory_review": review})


def test_usage_is_immutable_skill_scoped_and_survives_restart(system):
    selected = ready(system)
    created, source, job = target(system, selected)
    system.memory.record_usage(job, selected)
    task = comment(system, created, source)
    system.memory = MemoryStore(system.repository, system.clock)
    usage = system.memory.usage_for_task(task)
    assert usage["card_id"] == selected.card_id and usage["card_json"] == selected.card_json
    assert system.memory.usage_for_task({**task, "skill_name": "other"}) is None
    assert system.memory.usage_for_task({**task, "run_id": system.created.run_id}) is None
    assert system.memory.usage_for_task({**task, "replaces_answer_key": str(uuid.uuid4())}) is None
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_usages").fetchone()[0] == 1
    with pytest.raises(ValueError):
        system.memory.record_usage(job, replace(selected, card_sha256="0" * 64))


def test_plain_dislike_does_not_revoke_used_history(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    comment(system, created, source)
    assert system.memory.is_card_active(source.skill_name, selected)


def test_verified_global_error_fences_duplicates_future_cards_and_relikes(system):
    selected = ready(system)
    duplicate, duplicate_source = new_report(system, 2)
    vote(system, duplicate, duplicate_source, "duplicate")
    system.memory.finish_task(system.memory.claim_task()["task_id"], CARD)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    assert system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))
    assert not system.memory.is_card_active(source.skill_name, selected)
    assert all(row["card_sha256"] != selected.card_sha256 for row in system.memory.active_cards(source.skill_name))
    put(system, "old-down", "DISLIKE")
    put(system, "old-up")
    later, later_source = new_report(system, 3)
    vote(system, later, later_source, "later")
    system.memory.finish_task(system.memory.claim_task()["task_id"], CARD)
    assert [row["card_json"] for row in system.memory.active_cards(source.skill_name)] == [FIXED]
    recalled = ExperienceRetriever(system.memory).select(source.skill_name, "请求排队服务繁忙")
    assert recalled.card_json == FIXED


@pytest.mark.parametrize("relation", ["NOT_APPLICABLE", "DIAGNOSIS_OMISSION"])
def test_scoped_correction_overrides_only_matching_conditions(system, relation):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected, relation))
    assert system.memory.is_card_active(source.skill_name, selected)
    retriever = ExperienceRetriever(system.memory)
    assert retriever.select(source.skill_name, "请求排队服务繁忙").card_json == FIXED
    assert retriever.select(source.skill_name, "请求排队").card_id == selected.card_id
    assert retriever.select("another-skill", "请求排队服务繁忙") is None


@pytest.mark.parametrize("change", [{"card_id": str(uuid.uuid4())}, {"card_sha256": "0" * 64},
    {"evidence_indexes": []}, {"evidence_indexes": [1]}])
def test_forged_review_cannot_revoke_other_history(system, change):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    with pytest.raises(ValueError):
        system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected, **change))
    assert system.memory.is_card_active(source.skill_name, selected)


def test_old_feedback_late_result_cannot_revoke_used_history(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.memory.post_feedback_comment(created.conversation_id, created.run_id, owner_key=OWNER,
        request_id="newer-comment", text="补充核查说明", analysis_text="最新排查已排除泄漏。", source=source)
    assert not system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))
    assert system.memory.is_card_active(source.skill_name, selected)


def test_deleted_usage_source_cannot_publish_late_revision(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert system.memory.usage_for_task(task) is None
    with pytest.raises(ValueError):
        system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))


def test_specialized_execution_never_records_usage(system):
    selected = ready(system)
    job = SimpleNamespace(job_id=system.source.source_job_id, case_id=system.source.case_id,
        generic_skill_name=system.source.skill_name, diagnosis_mode=DiagnosisMode.SPECIALIZED)
    system.memory.record_usage(job, selected)
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_usages").fetchone()[0] == 0


def test_global_rejection_survives_correction_source_deletion(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))
    system.agent_store.request_delete(created.conversation_id, owner_key=OWNER)
    system.memory = MemoryStore(system.repository, system.clock)
    later, later_source = new_report(system, 9)
    vote(system, later, later_source, "later-after-delete")
    system.memory.finish_task(system.memory.claim_task()["task_id"], CARD)
    assert not system.memory.active_cards(source.skill_name)


def test_global_rejection_isolated_by_skill(system):
    selected = ready(system)
    other, other_source = new_report(system, 10)
    other_source = replace(other_source, skill_name="another-generic")
    vote(system, other, other_source, "other-skill")
    system.memory.finish_task(system.memory.claim_task()["task_id"], CARD)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))
    assert len(system.memory.active_cards("another-generic")) == 1


def test_review_without_actual_use_is_not_authorized(system):
    selected = ready(system)
    created, source = new_report(system, 8)
    source = replace(source, source_job_id=str(uuid.uuid4()))
    task = comment(system, created, source)
    with pytest.raises(ValueError, match="使用记录"):
        system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected))
    assert system.memory.is_card_active(source.skill_name, selected)


def test_unrelated_feedback_keeps_historical_card_and_creates_no_revision(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    system.memory.finish_task(task["task_id"], FIXED, evidence_json=evidence(selected, "UNRELATED", evidence_indexes=[]))
    assert system.memory.is_card_active(source.skill_name, selected)
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_revisions").fetchone()[0] == 0


def test_usage_survives_natural_resource_expiry(system):
    selected = ready(system)
    created, source, _ = target(system, selected)
    task = comment(system, created, source)
    with system.repository.database_transaction() as db:
        system.memory.expire_sources(db, system.created.conversation_id)
    assert system.memory.usage_for_task(task)["card_sha256"] == selected.card_sha256


def test_usage_schema_upgrade_and_offline_import_remain_explicit(system):
    from problem_locator.entrypoints.postgres_import import _source_schema, PostgresImportError
    from problem_locator.memory.usage import USAGE_TABLES, USAGE_VERSION_KEY
    with system.repository.database_transaction() as db:
        for name in USAGE_TABLES:
            db.execute("DROP TABLE " + name)
        db.execute("DELETE FROM metadata WHERE key=?", (USAGE_VERSION_KEY,))
        tables, _ = _source_schema(db)
        assert not tables.intersection(USAGE_TABLES)
    system.memory = MemoryStore(system.repository, system.clock)
    with system.repository.database_transaction() as db:
        tables, _ = _source_schema(db)
        assert USAGE_TABLES <= tables
        db.execute("DELETE FROM metadata WHERE key=?", (USAGE_VERSION_KEY,))
        with pytest.raises(PostgresImportError):
            _source_schema(db)


@pytest.mark.parametrize("size", [2, 20])
def test_recall_revision_metadata_does_not_add_per_card_queries(system, monkeypatch, size):
    from contextlib import contextmanager
    ready(system)
    for n in range(1, size):
        created, source = new_report(system, n)
        vote(system, created, source, f"bulk-{n}")
        system.memory.finish_task(system.memory.claim_task()["task_id"], CARD)
    statements = []
    original = system.repository.database_read
    class Observed:
        def __init__(self, db): self.db = db
        def execute(self, sql, parameters=()):
            statements.append(sql)
            return self.db.execute(sql, parameters)
        def __getattr__(self, name): return getattr(self.db, name)
    @contextmanager
    def counted():
        with original() as db:
            yield Observed(db)
    monkeypatch.setattr(system.repository, "database_read", counted)
    assert ExperienceRetriever(system.memory).select(system.source.skill_name, "请求排队服务繁忙") is not None
    assert len(statements) == 2  # One indexed page and one final fresh source check.
