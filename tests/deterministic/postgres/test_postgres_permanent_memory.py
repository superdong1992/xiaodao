"""Permanent card and bounded admission regressions on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hashlib

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.memory import store as memory_module
from problem_locator.memory.models import FeedbackSource
from problem_locator.memory.store import MemoryStore
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent.test_generic_feedback import CARD, NOW, OWNER
from tests.deterministic.unit.agent.test_permanent_memory import (
    new_report, vote,
    test_card_survives_old_ttl_cleanup_restart_and_vote_switch,
    test_terminal_history_immediately_releases_worker_capacity,
    test_pending_and_running_keep_capacity_until_finished,
    test_old_accepted_correction_still_replaces_original_and_can_be_disabled,
    test_resource_expiry_keeps_permanent_card_but_explicit_delete_revokes_it,
    test_card_scan_passes_task_limit_and_page_boundaries_without_hiding_old_match,
    test_identical_cards_keep_independent_vote_and_delete_sources,
)


@pytest.fixture
def system(postgres_repository):
    clock = SimpleNamespace(value=NOW)
    clock.now = lambda: clock.value
    agents = AgentStore(postgres_repository, clock=clock, runtime_epoch="permanent-pg")
    memory = MemoryStore(postgres_repository, clock)
    agents.memory_store = memory
    created = agents.create_conversation("create", owner_key=OWNER)
    report = "历史诊断报告，仅供提炼参考。"
    source = FeedbackSource(case_id="00000000-0000-0000-0000-000000000001",
        source_job_id="00000000-0000-0000-0000-000000000011", skill_name="generic-test",
        problem_text="请求排队，请检查。", report_markdown=report,
        report_sha256=hashlib.sha256(report.encode()).hexdigest())
    with postgres_repository.database_transaction() as db:
        db.execute("UPDATE agent_conversation_runs SET case_id=? WHERE run_id=?", (source.case_id, created.run_id))
    return SimpleNamespace(repository=postgres_repository, agent_store=agents, memory=memory,
        clock=clock, created=created, source=source)


def test_postgres_old_cards_do_not_block_concurrent_bounded_admission(system, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_TASKS", 1)
    for n in range(3):
        created, source = new_report(system, n)
        vote(system, created, source, f"old-{n}")
        task = system.memory.claim_task()
        assert task is not None
        system.memory.finish_task(task["task_id"], CARD)
    reports = [new_report(system, n) for n in range(3, 7)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: vote(system, *pair, f"up-{pair[0].run_id}"), reports))
    assert all(result.rating == "LIKE" for result in results)
    with system.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM memory_tasks WHERE status='PENDING'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM memory_tasks WHERE status='READY'").fetchone()[0] == 3
