"""Production PostgreSQL locks preserve the follow-up-to-diagnosis transaction."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent import test_report_continuation as continuation_tests
from tests.deterministic.unit.agent import test_followup_handoff as handoff_tests


@pytest.fixture
def system(postgres_repository, tmp_path, monkeypatch):
    monkeypatch.setattr(continuation_tests, "_open", lambda _: postgres_repository)
    yield from continuation_tests.system.__wrapped__(tmp_path)


@pytest.fixture
def state(system):
    return handoff_tests.state.__wrapped__(system)


def test_postgres_handoff_write_failure_rolls_back_run_message_and_events(state, monkeypatch):
    handoff_tests.test_handoff_answer_commit_failure_rolls_back_run_message_and_events(state, monkeypatch)


def test_postgres_stop_during_preparation_commits_before_handoff_without_deadlock(state, monkeypatch):
    s = state
    receipt = handoff_tests.ask(s)
    task = s.followups.store.claim_task()
    prepared, proceed = threading.Event(), threading.Event()
    original = s.service._baseline
    def baseline(*args):
        result = original(*args)
        prepared.set()
        assert proceed.wait(5)
        return result
    monkeypatch.setattr(s.service, "_baseline", baseline)
    with ThreadPoolExecutor(max_workers=1) as executor:
        handoff = executor.submit(s.followups.continue_diagnosis, task, handoff_tests.DECISION)
        try:
            assert prepared.wait(5)
            stopped = s.followups.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=handoff_tests.OWNER)
            assert stopped.status in {"CANCELLING", "CANCELLED"}
        finally:
            proceed.set()
        assert not handoff.result(timeout=5)
    assert handoff_tests.counts(s) == (1, 0, 0)
    assert handoff_tests.item(s).status == "CANCELLED"


def test_postgres_duplicate_handoff_creates_only_one_run(state):
    handoff_tests.test_handoff_retry_is_one_run_and_one_message_even_when_called_concurrently(state)


def test_postgres_existing_snapshot_context_uses_confirmed_facts(state):
    handoff_tests.test_existing_snapshot_does_not_hide_confirmed_facts_from_the_followup_model(state)
