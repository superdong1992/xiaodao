from __future__ import annotations

import hashlib
import json
from pathlib import Path

from problem_locator.application.outcome_processing import (
    OutcomeActivity,
    OutcomeReplayDisposition,
    classify_outcome_activity,
    decide_outcome_replay,
    make_outcome_processing_record,
    validate_published_job_recovery,
    validate_published_outcome,
)
from problem_locator.contracts import (
    ErrorCode,
    ExecutionFileRef,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    PublishedJobReceipt,
    RuntimeExecutionReceipt,
    StateFile,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-07-31T00:00:20.000Z"
EPOCH = "00000000-0000-0000-0000-000000000777"


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "tests/fixtures/contracts/positive" / name).read_text(
            encoding="utf-8"
        )
    )


def _outcome() -> JobOutcome:
    return JobOutcome.model_validate(_load("job-outcome-route.json"))


def _job() -> Job:
    return Job.model_validate(_load("job-route.json"))


def _file_ref(value: object, filename: str, job_id: str) -> ExecutionFileRef:
    payload = canonical_json_bytes(value)
    return ExecutionFileRef(
        relative_key=f"jobs/{job_id}/{filename}",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _active_aggregate():
    state = StateFile.model_validate(_load("state.json"))
    aggregate = next(iter(state.cases.values()))
    pending = next(iter(aggregate.jobs.values()))
    running = pending.model_copy(
        update={
            "status": JobStatus.RUNNING,
            "started_at": NOW,
            "runtime_epoch": EPOCH,
        }
    )
    return aggregate.model_copy(update={"jobs": {running.job_id: running}})


def test_finalized_record_is_the_only_trusted_outcome() -> None:
    outcome = _outcome()
    file_ref = _file_ref(outcome, "job_outcome.json", outcome.job_id)
    published = RuntimeExecutionReceipt(
        job_outcome=outcome,
        outcome_file_ref=file_ref,
    )

    valid = validate_published_outcome(outcome, file_ref, published)
    missing = validate_published_outcome(outcome, file_ref, None)
    changed = outcome.model_copy(update={"produced_at": "2026-07-31T00:00:31.000Z"})
    mismatch = validate_published_outcome(changed, file_ref, published)

    assert valid.outcome == outcome
    assert valid.error_code is None
    assert missing.error_code is ErrorCode.OUTCOME_MISSING
    assert mismatch.error_code is ErrorCode.OUTCOME_INVALID


def test_outcome_replay_distinguishes_duplicate_and_conflict() -> None:
    aggregate = _active_aggregate()
    outcome = _outcome()
    file_ref = _file_ref(outcome, "job_outcome.json", outcome.job_id)
    record = make_outcome_processing_record(
        outcome,
        file_ref,
        disposition=OutcomeDisposition.STALE,
        processed_at=NOW,
        error_code=None,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="late finalized record",
    )
    saved = aggregate.model_copy(
        update={"outcome_processing_records": {outcome.outcome_id: record}}
    )

    assert (
        decide_outcome_replay(saved, outcome.outcome_id, file_ref.sha256)
        is OutcomeReplayDisposition.DUPLICATE
    )
    assert (
        decide_outcome_replay(saved, outcome.outcome_id, "0" * 64)
        is OutcomeReplayDisposition.CONFLICT
    )
    assert (
        decide_outcome_replay(saved, "00000000-0000-0000-0000-000000000099", "0" * 64)
        is OutcomeReplayDisposition.NEW
    )


def test_activity_classification_separates_active_stale_invalid_and_missing() -> None:
    aggregate = _active_aggregate()
    outcome = _outcome()

    assert classify_outcome_activity(aggregate, outcome).activity is OutcomeActivity.ACTIVE

    advanced_diagnosis = aggregate.case.diagnosis_state.model_copy(
        update={"revision": 2}
    )
    advanced_case = aggregate.case.model_copy(
        update={"diagnosis_state": advanced_diagnosis}
    )
    advanced = aggregate.model_copy(update={"case": advanced_case})
    assert classify_outcome_activity(advanced, outcome).activity is OutcomeActivity.STALE

    wrong_case = outcome.model_copy(
        update={"case_id": "00000000-0000-0000-0000-000000000999"}
    )
    invalid = classify_outcome_activity(aggregate, wrong_case)
    assert invalid.activity is OutcomeActivity.INVALID
    assert invalid.error_code is ErrorCode.OUTCOME_INVALID

    missing_job = outcome.model_copy(
        update={"job_id": "00000000-0000-0000-0000-000000000999"}
    )
    missing = classify_outcome_activity(aggregate, missing_job)
    assert missing.activity is OutcomeActivity.JOB_NOT_FOUND
    assert missing.error_code is ErrorCode.JOB_NOT_FOUND


def test_published_job_recovery_checks_all_stable_lifecycle_fields() -> None:
    job = _job()
    receipt = PublishedJobReceipt(
        job=job,
        job_file_ref=_file_ref(job, "job.json", job.job_id),
    )

    assert validate_published_job_recovery(
        receipt,
        job_id=job.job_id,
        case_id=job.case_id,
        created_at=job.created_at,
    )
    assert not validate_published_job_recovery(
        receipt,
        job_id=job.job_id,
        case_id=job.case_id,
        created_at="2026-07-31T00:00:01.000Z",
    )


def test_processing_record_uses_finalized_hash_and_stable_business_time_split() -> None:
    outcome = _outcome()
    file_ref = _file_ref(outcome, "job_outcome.json", outcome.job_id)

    record = make_outcome_processing_record(
        outcome,
        file_ref,
        disposition=OutcomeDisposition.REJECTED,
        processed_at=NOW,
        error_code=ErrorCode.OUTCOME_INVALID,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="invalid binding",
    )

    assert record.outcome_hash == file_ref.sha256
    assert record.processed_at == NOW
    assert record.processed_at != outcome.produced_at
