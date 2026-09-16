"""In-flight commits keep their receipts and expose a missed dispatch signal."""
from __future__ import annotations

import pytest

from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import ApplicationPortError, CaseStatus, ErrorCode, Job, JobStatus, OutcomeDisposition, canonical_json_bytes
from problem_locator.dispatch import InProcessDispatcher, SchedulerService
from problem_locator.operational import OperationalState
from tests.deterministic.contracts.fakes import InMemoryExecutionRecordStore, InMemoryStateRepository, ScriptedCoordinator
from tests.deterministic.unit.application import test_external_commands as external
from tests.deterministic.unit.application import test_outcome_submission as outcomes
from .test_dispatcher import _RecordingWorker


OTHER_CASE = "00000000-0000-0000-0000-000000000991"
OTHER_JOB = "00000000-0000-0000-0000-000000000992"


def _pause(state):
    state.record(case_id=OTHER_CASE, job_id=OTHER_JOB, phase="RESULT_DELIVERY", error_code=ErrorCode.STATE_WRITE_FAILED)


def _dispatcher(repository, operational):
    worker = _RecordingWorker()
    dispatcher = InProcessDispatcher(worker, operational_state=operational,
        job_identity=lambda key: SchedulerService._identity(repository, key))
    return dispatcher, worker


def _pause_after_commit(monkeypatch, repository, operational):
    commit = repository.commit

    def commit_then_pause(*args):
        receipt = commit(*args)
        _pause(operational)
        return receipt

    monkeypatch.setattr(repository, "commit", commit_then_pause)


def _assert_pending_query_fault(repository, resources, notifier, operational, case_id, job_id):
    before = repository.read_snapshot().model_dump_json()
    query = ApplicationQueryService(repository, resources, notifier, operational_state=operational)
    with pytest.raises(ApplicationPortError) as caught:
        query.get_case(case_id)
    details = {item.field: item.actual for item in caught.value.error.details}
    assert caught.value.error.code is ErrorCode.DISPATCH_REJECTED
    assert details["phase"] == "DISPATCH_PAUSED" and details["persistence"] == "UNKNOWN"
    assert details["case_id"] == case_id and details["job_id"] == job_id
    assert OTHER_CASE not in details.values() and OTHER_JOB not in details.values()
    assert repository.read_snapshot().model_dump_json() == before


def test_create_case_committed_after_admission_pause_keeps_receipt_and_exposes_pending_job(monkeypatch):
    route = external._job("job-route.json")
    coordinator = ScriptedCoordinator([lambda *_: external._plan(target_status=CaseStatus.RUNNING,
        next_job_spec=external._job_spec(route, target_revision=1))])
    handler, repository, _, resources, _, notifier, _, _ = external._handler(
        external._state().model_copy(update={"generation": 0, "cases": {}}), coordinator)
    operational = OperationalState()
    dispatcher, worker = _dispatcher(repository, operational)
    handler._dispatcher, handler._operational = dispatcher, operational
    _pause_after_commit(monkeypatch, repository, operational)

    response = handler.execute(external._create_command())

    receipt = response.business_receipt
    assert response.dispatch_pending and receipt.case_revision == 1
    assert repository.read_job(receipt.job_id).status is JobStatus.PENDING
    assert len(repository.commit_calls) == len(coordinator.calls) == 1
    assert worker.calls == [] and dispatcher.queued_job_ids == ()
    _assert_pending_query_fault(repository, resources, notifier, operational, receipt.case_id, receipt.job_id)
    readiness = {item.field: item.actual for item in operational.latest_error.details}
    assert readiness["phase"] == "RESULT_DELIVERY" and readiness["cause_code"] == "STATE_WRITE_FAILED"
    with pytest.raises(ApplicationPortError) as caught:
        handler.execute(external._create_command(statement="new request after pause"))
    assert caught.value.error.code is ErrorCode.DISPATCH_REJECTED
    assert len(repository.commit_calls) == 1


def test_successful_outcome_after_pause_marks_new_job_even_when_source_job_is_confirmed(monkeypatch):
    outcome = outcomes._outcome("job-outcome-route.json")
    records = InMemoryExecutionRecordStore()
    file_ref = records.publish_outcome_bytes(outcome.job_id, canonical_json_bytes(outcome))
    bindings = outcomes.runtime_bindings_from_job(Job.model_validate(outcomes._load("job-diagnose.json")))
    repository = InMemoryStateRepository(outcomes._running_state())
    operational = OperationalState()
    dispatcher, worker = _dispatcher(repository, operational)
    service, _, resources, _, _, notifier, _ = outcomes._service(repository,
        ScriptedCoordinator([outcomes._route_to_diagnose_plan]), records,
        catalog=outcomes._diagnose_catalog(bindings), dispatcher=dispatcher)
    _pause_after_commit(monkeypatch, repository, operational)

    receipt = service.submit_outcome(outcome, file_ref)

    assert receipt.disposition is OutcomeDisposition.APPLIED
    aggregate = repository.read_case(outcome.case_id)
    next_job_id = aggregate.case.active_job_id
    assert next_job_id != outcome.job_id
    assert aggregate.jobs[outcome.job_id].status is JobStatus.SUCCEEDED
    assert aggregate.jobs[next_job_id].status is JobStatus.PENDING
    assert worker.calls == [] and dispatcher.queued_job_ids == ()
    operational.confirm_finished_job(outcome.job_id)
    _assert_pending_query_fault(repository, resources, notifier, operational, outcome.case_id, next_job_id)
    assert service.submit_outcome(outcome, file_ref).disposition is OutcomeDisposition.DUPLICATE
    assert len(repository.commit_calls) == 1


def test_paused_dispatch_does_not_mark_queued_duplicates_finished_jobs_or_unknown_ids():
    repository = InMemoryStateRepository(outcomes._running_state())
    operational = OperationalState()
    dispatcher, _ = _dispatcher(repository, operational)
    job_id = outcomes.JOB_ID
    assert dispatcher.submit(job_id).accepted
    _pause(operational)
    before = operational.faults
    assert dispatcher.submit(job_id).duplicate
    assert operational.faults == before
    dispatcher.cancel(job_id)
    # Running and completed Jobs need no new queue signal.
    assert not dispatcher.submit(job_id).accepted
    state = repository.read_snapshot()
    aggregate = state.cases[outcomes.CASE_ID]
    terminal = aggregate.model_copy(update={
        "case": aggregate.case.model_copy(update={"status": CaseStatus.CANCELLED, "active_job_id": None}),
        "jobs": {job_id: aggregate.jobs[job_id].model_copy(update={"status": JobStatus.CANCELLED,
            "finished_at": "2026-09-15T00:00:00.000Z"})}})
    repository.seed(state.model_copy(update={"cases": {outcomes.CASE_ID: terminal}}))
    assert not dispatcher.submit(job_id).accepted
    # An unreadable ID proves no Case identity and must not create a fault.
    assert not dispatcher.submit(OTHER_JOB).accepted
    assert operational.faults == before
