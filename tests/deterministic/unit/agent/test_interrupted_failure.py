"""Project only the failure that durably interrupted the current Job."""
from __future__ import annotations

from pathlib import Path

import pytest

from problem_locator.agent.failures import interrupted_execution_failure
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import (
    ApplicationErrorDetail,
    CaseAggregate,
    CaseStatus,
    ErrorCode,
    ExecutionFailure,
    ExecutionFailureRecord,
    ExecutionFileRef,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.contracts.serialization import bytes_sha256
from tests.deterministic.unit.storage.test_state_repository import _open


_FIXTURES = Path(__file__).parents[3] / "fixtures/contracts/positive"
_STARTED = "2026-07-31T00:00:10.000Z"
_FINISHED = "2026-07-31T00:00:20.000Z"
_PROCESSED = "2026-07-31T00:00:30.000Z"
_LATE_AUDIT = "2026-07-31T00:00:40.000Z"
_EPOCH = "00000000-0000-0000-0000-000000000080"
_OUTCOME_ID = "00000000-0000-0000-0000-000000000023"
_FAILURE_ID = "00000000-0000-0000-0000-000000000024"
_DIAGNOSTIC_ID = "diag-" + "a" * 64


def _failure():
    return ExecutionFailure(
        stage="EXECUTION_RECORD", code="EXECUTION_RECORD_FAILED", retryable=True,
        message="SECRET raw model output /private/internal/path",
        diagnostic_id=_DIAGNOSTIC_ID, reason_code="AUDIT_ARCHIVE_FAILED",
        details=[ApplicationErrorDetail(
            field="findings[1].evidence_refs[2]", actual="SECRET rejected model text",
            expected="SECRET expected value", resource_type=None, resource_id=None,
            resource_ref=None, limit=None, observed=None,
        )],
    )


def _with_outcome(aggregate, job, *, failure=None, outcome_id=_OUTCOME_ID, disposition=OutcomeDisposition.APPLIED):
    value = JobOutcome.model_validate_json((_FIXTURES / "job-outcome-failure.json").read_bytes())
    outcome = value.model_copy(update={
        "outcome_id": outcome_id, "job_id": job.job_id, "case_id": job.case_id,
        "job_type": job.job_type, "base_state_revision": job.base_state_revision,
        "produced_at": job.finished_at, "error": failure or _failure(),
    })
    raw = canonical_json_bytes(outcome)
    record = OutcomeProcessingRecord(
        outcome_id=outcome_id, job_id=job.job_id, outcome_hash=bytes_sha256(raw),
        outcome_file_ref=ExecutionFileRef(relative_key=f"jobs/{job.job_id}/job_outcome.json",
            size=len(raw), sha256=bytes_sha256(raw)),
        disposition=disposition, processed_at=_PROCESSED,
        error_code=ErrorCode.OUTCOME_INVALID if disposition is OutcomeDisposition.REJECTED else None,
        accepted_evidence_ids=[], accepted_artifact_ids=[], generated_artifact_ids=[],
        created_job_id=None, reason="Persist the observed result.",
    )
    return aggregate.model_copy(update={
        "outcomes": {**aggregate.outcomes, outcome_id: outcome},
        "outcome_processing_records": {**aggregate.outcome_processing_records, outcome_id: record},
    })


def _with_infrastructure_failure(aggregate, job, *, failure_id=_FAILURE_ID):
    record = ExecutionFailureRecord(failure_id=failure_id, job_id=job.job_id,
        runtime_epoch=job.runtime_epoch, failure=_failure(), recorded_at=job.finished_at)
    return aggregate.model_copy(update={
        "execution_failure_records": {**aggregate.execution_failure_records, failure_id: record},
    })


def _aggregate(source="outcome", disposition=OutcomeDisposition.APPLIED):
    state = StateFile.model_validate_json((_FIXTURES / "state.json").read_bytes())
    aggregate = next(iter(state.cases.values()))
    initial_job = next(iter(aggregate.jobs.values()))
    job = initial_job.model_copy(update={"status": JobStatus.INTERRUPTED, "runtime_epoch": _EPOCH,
        "started_at": _STARTED, "finished_at": _FINISHED})
    case = aggregate.case.model_copy(update={"status": CaseStatus.INTERRUPTED, "active_job_id": None,
        "case_revision": 3, "updated_at": _LATE_AUDIT})
    aggregate = aggregate.model_copy(update={"case": case, "jobs": {job.job_id: job}})
    if source == "outcome":
        aggregate = _with_outcome(aggregate, job, disposition=disposition)
    elif source == "infrastructure":
        aggregate = _with_infrastructure_failure(aggregate, job)
    return CaseAggregate.model_validate(aggregate.model_dump(mode="python"))


@pytest.mark.parametrize("disposition", [OutcomeDisposition.APPLIED, OutcomeDisposition.STALE, OutcomeDisposition.REJECTED])
def test_only_applied_outcome_error_explains_current_interruption(disposition):
    aggregate = _aggregate(disposition=disposition)
    job = next(iter(aggregate.jobs.values()))
    processing = aggregate.outcome_processing_records[_OUTCOME_ID]
    # Outcome generation, submission, and a later audit need not share a clock value.
    assert job.finished_at != processing.processed_at != aggregate.case.updated_at
    result = interrupted_execution_failure(aggregate)
    assert result == (_failure() if disposition is OutcomeDisposition.APPLIED else None)


@pytest.mark.parametrize("source", ["outcome", "infrastructure"])
def test_failure_must_match_the_job_completion_instant(source):
    aggregate = _aggregate(source)
    if source == "outcome":
        outcome = aggregate.outcomes[_OUTCOME_ID].model_copy(update={"produced_at": _PROCESSED})
        aggregate = aggregate.model_copy(update={"outcomes": {_OUTCOME_ID: outcome}})
    else:
        record = aggregate.execution_failure_records[_FAILURE_ID].model_copy(update={"recorded_at": _PROCESSED})
        aggregate = aggregate.model_copy(update={"execution_failure_records": {_FAILURE_ID: record}})
    assert interrupted_execution_failure(aggregate) is None


def test_infrastructure_failure_requires_same_runtime_epoch():
    aggregate = _aggregate("infrastructure")
    assert interrupted_execution_failure(aggregate) == _failure()
    record = aggregate.execution_failure_records[_FAILURE_ID].model_copy(update={
        "runtime_epoch": "00000000-0000-0000-0000-000000000081",
    })
    # A mismatched imported/injected record must not become public attribution.
    aggregate = aggregate.model_copy(update={"execution_failure_records": {_FAILURE_ID: record}})
    assert interrupted_execution_failure(aggregate) is None


def test_replaced_interruption_cannot_supply_the_new_jobs_failure():
    aggregate = _aggregate()
    previous = next(iter(aggregate.jobs.values()))
    replacement = previous.model_copy(update={
        "job_id": "00000000-0000-0000-0000-000000000011",
        "replacement_for_job_id": previous.job_id,
    })
    aggregate = aggregate.model_copy(update={"jobs": {previous.job_id: previous, replacement.job_id: replacement}})
    # Equal timestamps deliberately cannot identify which failure belongs now.
    assert interrupted_execution_failure(aggregate) is None
    current_failure = _failure().model_copy(update={"diagnostic_id": "diag-" + "b" * 64})
    aggregate = _with_outcome(aggregate, replacement, failure=current_failure,
        outcome_id="00000000-0000-0000-0000-000000000025")
    assert interrupted_execution_failure(aggregate) == current_failure


def test_multiple_unreplaced_interrupted_jobs_are_not_ordered_by_error_recency():
    aggregate = _aggregate()
    job = next(iter(aggregate.jobs.values()))
    other = job.model_copy(update={"job_id": "00000000-0000-0000-0000-000000000011",
        "finished_at": _PROCESSED})
    aggregate = aggregate.model_copy(update={"jobs": {job.job_id: job, other.job_id: other}})
    assert interrupted_execution_failure(aggregate) is None


@pytest.mark.parametrize("extra_source", ["outcome", "infrastructure"])
def test_multiple_matching_failure_sources_are_not_guessed(extra_source):
    aggregate = _aggregate()
    job = next(iter(aggregate.jobs.values()))
    if extra_source == "outcome":
        aggregate = _with_outcome(aggregate, job, outcome_id="00000000-0000-0000-0000-000000000025")
    else:
        aggregate = _with_infrastructure_failure(aggregate, job)
    assert interrupted_execution_failure(aggregate) is None


def test_interruption_without_record_does_not_invent_a_failure():
    assert interrupted_execution_failure(_aggregate("none")) is None


@pytest.mark.parametrize("source", ["outcome", "infrastructure"])
def test_interrupted_store_snapshot_keeps_typed_failure_across_reload_without_leaking_model_text(tmp_path, source):
    aggregate = _aggregate(source)
    repository = _open(tmp_path)
    try:
        store = AgentStore(repository, runtime_epoch="epoch-one")
        conversation = store.create_conversation("interrupted:projection").conversation_id
        store.bind_case(conversation, aggregate.case.case_id)
        with repository.database_transaction() as db:
            store._project_case(db, store._load(db, conversation), aggregate)
        snapshot = store.get_conversation(conversation)
        assert snapshot.status == "INTERRUPTED"
        assert snapshot.failure.code == "EXECUTION_RECORD_FAILED"
        assert snapshot.failure.retryable is False
        assert snapshot.failure.details == [
            {"field": "phase", "actual": "EXECUTION_RECORD"},
            {"field": "diagnostic_id", "actual": _DIAGNOSTIC_ID},
            {"field": "reason_code", "actual": "AUDIT_ARCHIVE_FAILED"},
            {"field": "location", "actual": "findings[1].evidence_refs[2]"},
        ]
        events = store.list_events(conversation)
        interrupted = next(event for event in events if event.type == "conversation.interrupted")
        assert interrupted.data == {"code": "AGENT_INTERRUPTED", "message": "服务已重启，本次任务已中断，请重新发起。"}
        assert events[-1].type == "conversation.completed"
        assert events[-1].data == {"status": "INTERRUPTED"}
        public_text = snapshot.model_dump_json() + "".join(event.model_dump_json() for event in events)
        assert "SECRET" not in public_text and "/private" not in public_text
    finally:
        repository.close()
    reopened = _open(tmp_path)
    try:
        recovered = AgentStore(reopened, runtime_epoch="epoch-two")
        recovered.recover()
        assert recovered.get_conversation(conversation).failure == snapshot.failure
        assert recovered.list_events(conversation) == events
    finally:
        reopened.close()
