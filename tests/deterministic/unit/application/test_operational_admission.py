"""Operational faults reject new writes and never replace confirmed Case state."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from problem_locator.application.external_commands import ExternalCommandHandler
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.bootstrap import ServiceStateAdmin
from problem_locator.contracts import ApplicationPortError, CaseStatus, ErrorCode, JobStatus, StateFile, SubmitSupplement
from problem_locator.operational import OperationalState
from tests.deterministic.contracts.fakes import InMemoryResourceStore, InMemoryStateChangeNotifier, InMemoryStateRepository
from tests.deterministic.unit.application.test_external_commands import _create_command
from tests.deterministic.unit.application.test_queries import CASE_ID, _state


def _faults():
    state = OperationalState(lambda: "2026-09-15T00:00:00Z")
    state.install_epoch("test-epoch")
    state.record(case_id=CASE_ID, job_id="00000000-0000-0000-0000-000000000010",
        phase="RESULT_DELIVERY", error_code=ErrorCode.STATE_WRITE_FAILED)
    return state


@pytest.mark.parametrize("command", [_create_command(), SubmitSupplement(
    idempotency_key="blocked-supplement", case_id=CASE_ID, expected_case_revision=1,
    inputs={"input": "value"}, attachment_ids=[], wait_seconds=0)])
def test_admission_rejects_before_repository_catalog_or_dispatch_access(command):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"admission touched {name}")

    unused = Forbidden()
    handler = ExternalCommandHandler(repository=unused, coordinator=unused, projector=unused,
        publication_guard=unused, resource_store=unused, execution_records=unused,
        asset_catalog=unused, dispatcher=unused, notifier=unused, clock=unused, ids=unused,
        operational_state=_faults())
    with pytest.raises(ApplicationPortError) as caught:
        handler.execute(command)
    assert caught.value.error.code is ErrorCode.DISPATCH_REJECTED
    assert not caught.value.error.details  # No other Case's identity leaks at admission.


def test_get_case_exposes_unknown_without_mutating_running_snapshot():
    repository = InMemoryStateRepository(_state())
    before = repository.read_snapshot().model_dump_json()
    service = ApplicationQueryService(repository, InMemoryResourceStore(), InMemoryStateChangeNotifier(),
        operational_state=_faults())
    with pytest.raises(ApplicationPortError) as caught:
        service.get_case(CASE_ID)
    details = {item.field: item.actual for item in caught.value.error.details}
    assert details["persistence"] == "UNKNOWN"
    assert details["phase"] == "RESULT_DELIVERY"
    assert repository.read_snapshot().model_dump_json() == before


def test_confirmed_terminal_case_wins_over_process_local_fault():
    payload = _state().model_dump(mode="json")
    aggregate = payload["cases"][CASE_ID]
    aggregate["case"].update(status="CANCELLED", active_job_id=None)
    for job in aggregate["jobs"].values():
        job.update(status="CANCELLED", finished_at="2026-09-15T00:00:00.000Z")
    service = ApplicationQueryService(InMemoryStateRepository(StateFile.model_validate(payload)),
        InMemoryResourceStore(), InMemoryStateChangeNotifier(), operational_state=_faults())
    assert service.get_case(CASE_ID).case_view.status.value == "CANCELLED"


def test_confirmed_finished_job_wins_while_case_awaits_input():
    state = _state()
    aggregate = state.cases[CASE_ID]
    finished = aggregate.jobs[aggregate.case.active_job_id].model_copy(update={
        "status": JobStatus.SUCCEEDED, "finished_at": "2026-09-15T00:00:00.000Z"})
    waiting = aggregate.case.model_copy(update={"status": CaseStatus.WAITING_INPUT, "active_job_id": None})
    snapshot = state.model_copy(update={"cases": {CASE_ID: aggregate.model_copy(update={
        "case": waiting, "jobs": {finished.job_id: finished}})}})
    operational = _faults()
    service = ApplicationQueryService(InMemoryStateRepository(snapshot), InMemoryResourceStore(),
        InMemoryStateChangeNotifier(), operational_state=operational)
    assert service.get_case(CASE_ID).case_view.status is CaseStatus.WAITING_INPUT
    assert operational.error_for_case(CASE_ID, "WAITING_INPUT") is None
    assert not operational.accepting and operational.latest_error is not None


def test_readiness_exposes_original_delivery_failure_and_unknown(monkeypatch):
    monkeypatch.setattr("problem_locator.bootstrap._directories_valid", lambda _: True)
    admin = ServiceStateAdmin(layout=object(), coordination_lock=object(),
        instance_lock=SimpleNamespace(is_acquired=lambda: True),
        repository=SimpleNamespace(health=lambda: SimpleNamespace(valid=True)),
        scheduler=SimpleNamespace(ready=False, operational_state=_faults()))
    report = admin.readiness()
    assert not report.ready
    assert report.error.code is ErrorCode.DISPATCH_REJECTED
    details = {item.field: item.actual for item in report.error.details}
    assert details["cause_code"] == "STATE_WRITE_FAILED"
    assert details["persistence"] == "UNKNOWN"
    assert not next(item for item in report.checks if item.name == "RECOVERY").passed
