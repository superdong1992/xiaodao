"""Background learning yields to observed foreground work without model calls."""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from problem_locator.agent.service import AgentConversationService
from problem_locator.continuation.service import ContinuationService
from problem_locator.contracts import JobType
from problem_locator.dispatch.activity import QueueIdleObservation
from problem_locator.dispatch.dispatcher import InProcessDispatcher
from problem_locator.dispatch.service import SchedulerService
from problem_locator.followup.service import ReportFollowupService
from problem_locator.followup.worker import FollowupWorker

from test_experience_memory import Backend, Store, task, worker
from tests.deterministic.unit.agent.test_report_followup import system as followup_system, submit


def confirm_empty(observation):
    observation.observed_empty(observation.begin_scan())


def test_empty_observation_never_overwrites_newer_notification():
    observation = QueueIdleObservation()
    assert not observation.idle
    scan = observation.begin_scan()
    observation.invalidate()
    observation.observed_empty(scan)
    assert not observation.idle
    confirm_empty(observation)
    assert observation.idle
    observation.begin_scan()
    assert not observation.idle  # Failed or interrupted scans remain unknown.


@pytest.mark.parametrize("answer", [False, None, 1, RuntimeError("unavailable")])
def test_busy_unknown_or_failed_gate_does_not_claim_task(tmp_path, answer):
    store, backend = Store([task()]), Backend()
    state = {"answer": answer}

    def idle():
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    consumer = worker(tmp_path, store, backend, can_start=idle)
    assert not consumer.run_once()
    assert store.claims == 0 and store.tasks[0]["status"] == "PENDING"
    assert not backend.calls and not store.failed
    state["answer"] = True
    assert consumer.run_once()
    assert len(backend.calls) == 1 and store.tasks[0]["status"] == "READY"
    assert not consumer.run_once()
    assert len(backend.calls) == 1


def test_foreground_arrival_never_cancels_or_repeats_started_learning(tmp_path):
    state = {"idle": True}

    def foreground_arrives(invocation):
        state["idle"] = False
        assert not invocation["cancellation"].is_cancelled()

    store, backend = Store([task(), task()]), Backend(action=foreground_arrives)
    consumer = worker(tmp_path, store, backend, can_start=lambda: state["idle"])
    assert consumer.run_once()
    assert not consumer.run_once()
    assert len(backend.calls) == store.claims == 1
    assert [item["status"] for item in store.tasks] == ["READY", "PENDING"]
    assert not store.failed


@pytest.mark.parametrize("job_type", [JobType.ROUTE, JobType.DIAGNOSE])
def test_running_and_queued_foreground_defers_learning_until_idle(tmp_path, job_type):
    started, release = threading.Event(), threading.Event()

    class Foreground:
        def execute_one(self, job_id, cancellation):
            started.set()
            assert release.wait(2)

        def request_shutdown(self):
            release.set()

    dispatcher = InProcessDispatcher(Foreground(), route_workers=1, diagnose_workers=1,
        job_identity=lambda identity: (identity, job_type))
    scheduler = SchedulerService.__new__(SchedulerService)
    scheduler._lock = threading.Lock()
    scheduler._recovery_result = SimpleNamespace(completed=True)
    scheduler._fatal_worker_error_type = None
    scheduler._dispatcher = dispatcher
    scheduler.operational_state = dispatcher._operational
    store, backend = Store([task()]), Backend()
    consumer = worker(tmp_path, store, backend, can_start=scheduler.foreground_idle)
    assert not consumer.run_once()  # Scheduler has not enabled claiming yet.
    try:
        dispatcher.start()
        dispatcher.enable_claiming()
        dispatcher.submit(str(uuid.uuid4()))
        assert started.wait(2)
        dispatcher.submit(str(uuid.uuid4()))
        assert len(dispatcher.running_job_ids) == len(dispatcher.queued_job_ids) == 1
        assert not consumer.run_once()
        assert store.claims == 0 and not backend.calls
        release.set()
        assert dispatcher.wait_until_idle(2)
        assert consumer.run_once()
        assert len(backend.calls) == 1
        dispatcher.pause_claiming()
        assert not scheduler.foreground_idle()
    finally:
        release.set()
        assert dispatcher.shutdown(2)
    assert not scheduler.foreground_idle()


def bare_agent():
    agent = AgentConversationService.__new__(AgentConversationService)
    agent.application = SimpleNamespace()
    agent._stop = threading.Event()
    agent._wake, agent._control_wake, agent._continuation_wake = (threading.Event() for _ in range(3))
    agent._failure = None
    agent._thread = agent._continuation_thread = SimpleNamespace(is_alive=lambda: True)
    agent._intake_activity = QueueIdleObservation()
    agent._processing, agent._run_lock = threading.Lock(), threading.Lock()
    agent._active_runs = {}
    agent.followups = None
    agent.continuations = SimpleNamespace(foreground_activity=QueueIdleObservation(), _processing=threading.Lock())
    agent.store = SimpleNamespace(pending_conversations=lambda: [])
    return agent


def test_intake_and_continuation_observations_cover_startup_notifications_and_active_work():
    agent = bare_agent()
    assert not agent.foreground_idle()
    assert not agent.run_once()
    assert not agent.foreground_idle()  # Continuation queue is still unknown.
    confirm_empty(agent.continuations.foreground_activity)
    assert agent.foreground_idle()
    agent._changed("conversation")
    assert not agent.foreground_idle()
    assert not agent.run_once()
    confirm_empty(agent.continuations.foreground_activity)
    agent._active_runs["attachment-or-intake"] = object()
    assert not agent.foreground_idle()
    agent._active_runs.clear()
    assert agent.foreground_idle()
    agent._failure = "database unavailable"
    assert not agent.foreground_idle()
    agent._failure = None
    agent._stop.set()
    assert not agent.foreground_idle()


def test_intake_only_marks_global_empty_scan_idle():
    agent = bare_agent()
    confirm_empty(agent.continuations.foreground_activity)
    agent.store.pending_conversations = lambda: ["queued"]
    agent._advance_guarded = lambda selected: False
    agent._attachment_workers = SimpleNamespace(discard_unsubmitted=lambda task: None)
    assert not agent.run_once()
    assert not agent.foreground_idle()
    agent.store.pending_conversations = lambda: []
    assert not agent.run_once("one-conversation")
    assert not agent.foreground_idle()
    assert not agent.run_once()
    assert agent.foreground_idle()


def test_notification_during_empty_intake_scan_invalidates_observation():
    agent = bare_agent()
    confirm_empty(agent.continuations.foreground_activity)

    def scan():
        agent._changed("new-request")
        return []

    agent.store.pending_conversations = scan
    assert not agent.run_once()
    assert not agent.foreground_idle()


def test_continuation_reuses_existing_scan_without_changing_work_result():
    queries = []

    class Database:
        def execute(self, query, params):
            queries.append(query)
            return SimpleNamespace(fetchall=lambda: [])

    @contextmanager
    def read():
        yield Database()

    continuation = ContinuationService.__new__(ContinuationService)
    continuation.agent = bare_agent()
    continuation.repository = SimpleNamespace(database_read=read)
    continuation._processing = threading.Lock()
    continuation.foreground_activity = QueueIdleObservation()
    continuation._last_scan_empty = False
    assert not continuation.run_once("one-conversation")
    assert not continuation.foreground_activity.idle
    assert not continuation.run_once()
    assert continuation.foreground_activity.idle
    assert len(queries) == 2


def bare_followups(*, empty=True, claim=None):
    service = ReportFollowupService.__new__(ReportFollowupService)
    service.enabled = True
    service._stop, service._wake = threading.Event(), threading.Event()
    service._threads = [SimpleNamespace(is_alive=lambda: True)]
    service.foreground_activity = QueueIdleObservation()
    service.store = SimpleNamespace(last_claim_empty=empty, claim_task=claim or (lambda: None))
    service.worker = FollowupWorker(service, "unused", backend=Backend())
    return service


def test_followup_queue_unknown_or_contended_is_not_idle():
    service = bare_followups(empty=False)
    assert not service.foreground_idle()
    assert not service.worker.run_once()
    assert not service.foreground_idle()
    service.store.last_claim_empty = True
    assert not service.worker.run_once()
    assert service.foreground_idle()
    service._changed()
    assert not service.foreground_idle()
    assert not service.worker.run_once()
    assert service.foreground_idle()
    service.worker._processing.acquire()
    try:
        assert not service.foreground_idle()
    finally:
        service.worker._processing.release()
    service._stop.set()
    assert not service.foreground_idle()


def test_followup_notification_during_scan_and_storage_failure_remain_unknown():
    service = bare_followups()
    service.store.claim_task = lambda: service._changed()
    assert not service.worker.run_once()
    assert not service.foreground_idle()

    def failed():
        raise RuntimeError("storage unavailable")

    service.store.claim_task = failed
    assert not service.worker.run_once()
    assert not service.foreground_idle()
    service.enabled = False
    assert service.foreground_idle()  # Disabled followups cannot call a model.


def test_followup_real_queue_notification_and_empty_scan(followup_system):
    service = followup_system.service
    assert not service.foreground_activity.idle
    assert not service.worker.run_once()
    assert service.foreground_activity.idle
    submit(followup_system)
    assert not service.foreground_activity.idle
    claimed = service.store.claim_task()
    assert claimed is not None and not service.store.last_claim_empty
    assert not followup_system.backend.calls


def test_followup_contended_claim_does_not_look_like_empty_queue(followup_system, monkeypatch):
    import problem_locator.followup.store as followup_store

    service = followup_system.service
    assert not service.worker.run_once()
    submit(followup_system)
    monkeypatch.setattr(followup_store, "try_lock_conversation", lambda *args: False)
    assert not service.worker.run_once()
    assert not service.store.last_claim_empty and not service.foreground_activity.idle
    assert not followup_system.backend.calls
