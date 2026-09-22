"""Feedback identity, one-shot extraction, deletion fences, and HTTP contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.application.reports import read_published_report
from problem_locator.contracts import DiagnosisMode, JobStatus
from problem_locator.memory import store as memory_module
from problem_locator.memory.models import FeedbackRequest, FeedbackSource
from problem_locator.memory.store import MemoryStore
from tests.deterministic.unit.application.test_reports import generic_report
from tests.deterministic.unit.interfaces.test_agent_http import app_for
from tests.deterministic.unit.storage.test_state_repository import _open

OWNER = "a" * 64
OTHER = "b" * 64
NOW = "2026-09-21T00:00:00.000Z"
CARD = json.dumps({"problem_features": ["请求排队"], "applicability": ["服务繁忙"],
                   "steps": ["核对队列长度"], "limitations": ["需要当前日志支持"]}, ensure_ascii=False)


@pytest.fixture
def system(tmp_path):
    repository = _open(tmp_path)
    clock = SimpleNamespace(value=NOW)
    clock.now = lambda: clock.value
    agent_store = AgentStore(repository, clock=clock, runtime_epoch="test")
    memory = MemoryStore(repository, clock)
    agent_store.memory_store = memory
    created = agent_store.create_conversation("create", owner_key=OWNER)
    report = "# 诊断报告\n历史业务原文，只可供后台提炼。\n"
    source = FeedbackSource(case_id="00000000-0000-0000-0000-000000000001",
        source_job_id="00000000-0000-0000-0000-000000000011", skill_name="generic-test",
        problem_text="请求排队，请检查。", report_markdown=report,
        report_sha256=hashlib.sha256(report.encode()).hexdigest())
    with repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (source.case_id, created.run_id))
    value = SimpleNamespace(repository=repository, agent_store=agent_store, memory=memory,
                            clock=clock, created=created, source=source)
    yield value
    repository.close()


def put(system, request_id="like", rating="LIKE", **kwargs):
    return system.memory.put_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, request_id=request_id, rating=rating, source=system.source, **kwargs)


def task_rows(system):
    with system.repository.database_read() as db:
        return db.execute("SELECT task_id,status,problem_text,report_markdown,card_json,active FROM memory_tasks").fetchall()


def test_replay_and_same_vote_do_not_increment_or_restore_an_old_vote(system):
    initial = system.memory.get_feedback(system.created.conversation_id, system.created.run_id,
                                        owner_key=OWNER, can_rate=True)
    assert initial.rating is initial.updated_at is None
    first = put(system)
    system.clock.value = "2026-09-22T00:00:00.000Z"
    assert put(system) == first
    assert put(system, "same-vote") == first
    assert len(task_rows(system)) == 1
    down = put(system, "down", "DISLIKE")
    assert down.updated_at == system.clock.value and down.rating == "DISLIKE"
    assert put(system) == down
    assert len(task_rows(system)) == 1
    with pytest.raises(AgentStoreError, match="request_id") as conflict:
        put(system, "like", "DISLIKE")
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"


def test_latest_vote_controls_late_completion_and_relike_reuses_card(system):
    put(system)
    task = system.memory.claim_task()
    assert task["problem_text"] == system.source.problem_text
    assert task["report_markdown"] == system.source.report_markdown
    assert system.memory.claim_task() is None
    put(system, "down", "DISLIKE")
    assert system.memory.finish_task(task["task_id"], CARD)
    assert system.memory.active_cards(system.source.skill_name) == []
    assert task_rows(system)[0][2:4] == (None, None)
    put(system, "up-again")
    cards = system.memory.active_cards(system.source.skill_name)
    assert len(cards) == 1
    assert cards[0]["card_sha256"] == hashlib.sha256(cards[0]["card_json"].encode()).hexdigest()
    assert system.memory.active_cards("another-skill") == []
    assert system.memory.claim_task() is None
    assert not system.memory.finish_task(task["task_id"], CARD)


def test_downvote_first_creates_no_task_and_claim_is_atomic(system):
    put(system, "first-down", "DISLIKE")
    assert task_rows(system) == []
    put(system)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: system.memory.claim_task(), range(4)))
    assert sum(item is not None for item in claimed) == 1


def test_concurrent_same_request_is_one_vote_and_one_task(system):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: put(system), range(4)))
    assert all(result == results[0] for result in results)
    assert len(task_rows(system)) == 1
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback_requests").fetchone()[0] == 1


def test_request_identity_is_owner_scoped_and_cannot_move_to_another_run(system):
    put(system)
    other = system.agent_store.create_conversation("other", owner_key=OWNER)
    other_source = replace(system.source, case_id="00000000-0000-0000-0000-000000000099")
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (other_source.case_id, other.run_id))
    with pytest.raises(AgentStoreError) as error:
        system.memory.put_feedback(other.conversation_id, other.run_id, owner_key=OWNER,
            request_id="like", rating="LIKE", source=other_source)
    assert error.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    with system.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversations SET owner_key=? WHERE conversation_id=?", (OTHER, other.conversation_id))
    assert system.memory.put_feedback(other.conversation_id, other.run_id, owner_key=OTHER,
        request_id="like", rating="LIKE", source=other_source).rating == "LIKE"


def test_feedback_and_task_insert_roll_back_together_when_capacity_is_full(system, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_TASKS", 0)
    with pytest.raises(AgentStoreError) as error:
        put(system)
    assert error.value.status_code == 429
    assert task_rows(system) == []
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_feedback_requests").fetchone()[0] == 0


def test_idempotency_capacity_keeps_old_receipts_and_current_vote(system, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_REQUESTS_PER_REPORT", 2)
    put(system)
    put(system, "same")
    with pytest.raises(AgentStoreError) as error:
        put(system, "down", "DISLIKE")
    assert error.value.code == "AGENT_FEEDBACK_LIMIT_EXCEEDED"
    assert put(system).rating == "LIKE"
    assert len(task_rows(system)) == 1


def test_delete_clears_private_material_and_fences_late_worker_until_cleanup(system):
    put(system)
    task = system.memory.claim_task()
    system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    row = task_rows(system)[0]
    assert row[1:] == ("DELETED", None, None, None, 0)
    assert not system.memory.finish_task(task["task_id"], CARD)
    system.memory.prune()
    assert len(task_rows(system)) == 1
    with pytest.raises(AgentStoreError) as error:
        put(system)
    assert error.value.status_code == 404
    system.agent_store.finish_cleanup(system.created.conversation_id)
    system.memory.prune()
    assert task_rows(system) == []
    assert not system.memory.finish_task(task["task_id"], CARD)


def test_delete_is_atomic_with_memory_revocation(system, monkeypatch):
    put(system)
    task = system.memory.claim_task()
    system.memory.finish_task(task["task_id"], CARD)
    original = system.memory.revoke_conversation
    def fail_after_revoke(db, conversation_id):
        original(db, conversation_id)
        raise RuntimeError("transaction rollback")
    monkeypatch.setattr(system.memory, "revoke_conversation", fail_after_revoke)
    with pytest.raises(RuntimeError):
        system.agent_store.request_delete(system.created.conversation_id, owner_key=OWNER)
    assert system.memory.get_feedback(system.created.conversation_id, system.created.run_id,
        owner_key=OWNER, can_rate=True).rating == "LIKE"
    assert len(system.memory.active_cards(system.source.skill_name)) == 1


def test_failed_or_interrupted_task_clears_raw_material_and_never_retries(system):
    put(system)
    task = system.memory.claim_task()
    reopened = MemoryStore(system.repository, system.clock)
    assert reopened.recover() == 1
    assert task_rows(system)[0][1:4] == ("FAILED", None, None)
    assert not reopened.finish_task(task["task_id"], CARD)
    put(system, "up-again")
    assert reopened.claim_task() is None


def test_task_failure_and_source_expiry_clear_raw_material(system):
    put(system)
    task = system.memory.claim_task()
    assert system.memory.fail_task(task["task_id"])
    assert task_rows(system)[0][1:4] == ("FAILED", None, None)
    assert not system.memory.fail_task(task["task_id"])


def test_pending_source_expires_after_seven_days(system):
    put(system)
    system.clock.value = "2026-09-28T00:00:00.000Z"
    assert system.memory.prune()["expired_sources"] == 1
    assert task_rows(system)[0][1:4] == ("FAILED", None, None)
    assert system.memory.claim_task() is None
    put(system, "up-again")
    assert system.memory.claim_task() is None


def test_card_survives_natural_source_retention_but_expires_after_ninety_days(system):
    put(system)
    task = system.memory.claim_task()
    system.memory.finish_task(task["task_id"], CARD)
    with system.repository.database_transaction() as db:
        db.execute("DELETE FROM agent_conversation_runs WHERE run_id=?", (system.created.run_id,))
    system.memory.prune()
    assert len(system.memory.active_cards(system.source.skill_name)) == 1
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_feedback_requests").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_feedback").fetchone()[0] == 0
    system.clock.value = "2026-12-20T00:00:00.000Z"
    assert system.memory.active_cards(system.source.skill_name) == []
    system.memory.prune()
    assert task_rows(system) == []


@pytest.mark.parametrize("payload", ["[]", "not json", "{\"x\":NaN}", json.dumps({"large": "x" * 4096})])
def test_store_rejects_invalid_or_oversize_card(system, payload):
    put(system)
    task = system.memory.claim_task()
    with pytest.raises(ValueError):
        system.memory.finish_task(task["task_id"], payload)
    assert system.memory.active_cards(system.source.skill_name) == []


def configured_service(system, monkeypatch, *, enabled=True, version=2, mode=DiagnosisMode.GENERIC):
    aggregate, resources, report = generic_report(version)
    job = SimpleNamespace(job_id=report.source_job_id, status=JobStatus.SUCCEEDED,
        diagnosis_mode=mode, generic_skill_name=report.skill_name, generic_problem_text="定位原始问题")
    aggregate.jobs[job.job_id] = job
    captured = SimpleNamespace(view=SimpleNamespace(case_id=aggregate.case.case_id),
                               snapshot=SimpleNamespace(cases={aggregate.case.case_id: aggregate}))
    def capture(conversation_id, *, run_id, **kwargs):
        assert conversation_id == system.created.conversation_id and run_id == system.created.run_id
        return captured
    monkeypatch.setattr(system.agent_store, "read_conversation", capture)
    application = SimpleNamespace(read_conversation_delivery=lambda *a, **kw:
        (aggregate.case, read_published_report(aggregate, resources), None))
    service = AgentConversationService(system.agent_store, application, None, system.repository.layout,
                                      memory_store=system.memory, memory_enabled=enabled)
    return service, captured, resources


def request(service, system, method="GET", *, owner=OWNER, run_id=None, body=None, query=""):
    path = f"/api/v1/agent/conversations/{system.created.conversation_id}/runs/{run_id or system.created.run_id}/feedback{query}"
    async def run():
        headers = {} if owner is None else {"X-Agent-Owner-Key": owner}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(service)), base_url="http://local") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def test_http_roundtrip_checks_real_source_job_and_exposes_only_minimal_state(system, monkeypatch):
    service, _, _ = configured_service(system, monkeypatch)
    response = request(service, system)
    assert response.status_code == 200
    assert response.json()["data"] == dict(schema_version=1, conversation_id=system.created.conversation_id,
        run_id=system.created.run_id, can_rate=True, rating=None, updated_at=None)
    response = request(service, system, "PUT", body={"request_id": "http-like", "rating": "LIKE"})
    assert response.status_code == 200 and response.json()["data"]["rating"] == "LIKE"
    assert set(response.json()["data"]) == {"schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"}
    assert "原始问题" not in response.text and "card" not in response.text
    schema = app_for(service).openapi()
    route = schema["paths"]["/api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback"]
    assert set(route) == {"get", "put"}
    assert "429" in route["put"]["responses"]
    assert any(item["name"] == "X-Agent-Owner-Key" and item["required"] for item in route["put"]["parameters"])


@pytest.mark.parametrize("version,mode", [(1, DiagnosisMode.GENERIC), (2, DiagnosisMode.SPECIALIZED)])
def test_legacy_or_specialized_markdown_report_is_not_eligible(system, monkeypatch, version, mode):
    service, _, _ = configured_service(system, monkeypatch, version=version, mode=mode)
    response = request(service, system)
    assert response.status_code == 200 and response.json()["data"]["can_rate"] is False
    response = request(service, system, "PUT", body={"request_id": "no", "rating": "LIKE"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_FEEDBACK_UNSUPPORTED"
    assert task_rows(system) == []


def test_disabled_feedback_is_readable_but_cannot_write_or_extract(system, monkeypatch):
    put(system)
    service, _, _ = configured_service(system, monkeypatch, enabled=False)
    response = request(service, system)
    assert response.status_code == 200
    assert response.json()["data"]["rating"] == "LIKE"
    assert response.json()["data"]["can_rate"] is False
    response = request(service, system, "PUT", body={"request_id": "down", "rating": "DISLIKE"})
    assert response.status_code == 409
    assert task_rows(system)[0][1] == "PENDING"


@pytest.mark.parametrize("owner,expected", [(None, 400), ("bad", 400), (OTHER, 404)])
def test_http_requires_trusted_owner_without_revealing_feedback(system, monkeypatch, owner, expected):
    service, _, _ = configured_service(system, monkeypatch)
    assert request(service, system, owner=owner).status_code == expected
    assert request(service, system, "PUT", owner=owner,
                   body={"request_id": "like", "rating": "LIKE"}).status_code == expected
    assert task_rows(system) == []


@pytest.mark.parametrize("body", [
    {"request_id": "", "rating": "LIKE"}, {"request_id": " ", "rating": "LIKE"},
    {"request_id": "x" * 129, "rating": "LIKE"}, {"request_id": "x", "rating": None},
    {"request_id": "x", "rating": "CANCEL"}, {"request_id": "x", "rating": "LIKE", "comment": "no"},
])
def test_http_rejects_unsupported_feedback_inputs(system, monkeypatch, body):
    service, _, _ = configured_service(system, monkeypatch)
    assert request(service, system, "PUT", body=body).status_code == 400
    assert task_rows(system) == []


def test_feedback_request_allows_exact_unicode_character_limit():
    assert FeedbackRequest(request_id="汉" * 128, rating="LIKE").request_id == "汉" * 128


def test_http_rejects_queries_and_foreign_run_before_mutation(system, monkeypatch):
    service, _, _ = configured_service(system, monkeypatch, enabled=False)
    assert request(service, system, query="?rating=LIKE").status_code == 400
    assert request(service, system, run_id="ffffffff-ffff-ffff-ffff-ffffffffffff").status_code == 404
    assert task_rows(system) == []


def test_feedback_get_rejects_nonempty_body(system, monkeypatch):
    service, _, _ = configured_service(system, monkeypatch)
    assert request(service, system, body={"rating": "LIKE"}).status_code == 400
    assert task_rows(system) == []


def test_source_size_eligibility_matches_put_gate(system, monkeypatch):
    service, captured, _ = configured_service(system, monkeypatch)
    aggregate = next(iter(captured.snapshot.cases.values()))
    job = next(iter(aggregate.jobs.values()))
    job.generic_problem_text = "字" * 21846
    assert request(service, system).json()["data"]["can_rate"] is False
    assert request(service, system, "PUT", body={"request_id": "large", "rating": "LIKE"}).status_code == 409
    assert task_rows(system) == []


def test_store_revalidates_card_privacy_without_trusting_its_caller(system):
    put(system)
    task = system.memory.claim_task()
    value = json.loads(CARD)
    value["steps"] = ["访问 https://private.example/path 获取日志"]
    with pytest.raises(ValueError):
        system.memory.finish_task(task["task_id"], json.dumps(value))
    assert system.memory.active_cards(system.source.skill_name) == []
