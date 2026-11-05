"""Production PostgreSQL locks preserve the follow-up-to-diagnosis transaction."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.deterministic.unit.agent import test_followup_material_selection as material_tests

from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.agent import test_report_continuation as continuation_tests
from tests.deterministic.unit.agent import test_followup_handoff as handoff_tests
from tests.deterministic.unit.agent import test_followup_generic_dispatch as generic_tests


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
    handoff_tests.test_existing_snapshot_does_not_hide_confirmed_facts_from_generic_reference(state)


def test_postgres_explicit_selection_can_keep_only_one_original_archive(state):
    material_tests.test_explicit_selection_can_remove_one_original_archive_without_new_upload(state)


def test_postgres_latest_failed_attempt_context_and_selection(state):
    material_tests.test_latest_failed_attempt_preserves_user_corrections_and_materials_only(state)


def test_postgres_explicit_selection_rechecks_material_before_commit(state, monkeypatch):
    material_tests.test_explicit_selection_revalidates_availability_before_atomic_handoff(state, monkeypatch)


def test_postgres_repeated_failure_retains_last_effective_problem(state):
    material_tests.test_repeated_failure_keeps_correction_in_last_adopted_problem_text(state)


def test_postgres_long_attempt_survives_without_retired_launcher_requirements(state):
    material_tests.test_long_attempt_context_is_complete_without_retired_launcher_tool_requirements(state)


def test_postgres_every_question_dispatches_generic_without_a_preliminary_model(state):
    generic_tests.test_question_starts_generic_without_running_a_followup_classifier(state, "另一个服务为什么磁盘抖动？")


def test_postgres_active_generic_diagnosis_blocks_new_question_but_replays_receipt(state):
    generic_tests.test_active_diagnosis_blocks_new_questions_but_replays_original_receipt(state)


def test_postgres_newer_report_requires_latest_entry_and_keeps_original_receipt(state):
    generic_tests.test_newer_report_requires_latest_entry_but_keeps_history_and_receipt(state)


@pytest.mark.parametrize("retain_second", [False, True])
def test_postgres_retry_preserves_saved_material_selection(state, retain_second):
    generic_tests.test_retry_uses_saved_material_selection_without_reviving_excluded_logs(state, retain_second)
