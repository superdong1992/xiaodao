from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest

from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    Attachment,
    AttachmentStatus,
    ERROR_SPECS,
    ErrorCode,
    Job,
    JobOutcome,
    JobStatus,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    ResourceType,
    StateFile,
    canonical_json_bytes,
)
from problem_locator.contracts.limits import (
    ORPHAN_RESOURCE_RETENTION_SECONDS,
    PROPOSAL_STAGING_RETENTION_SECONDS,
    UPLOAD_TEMP_RETENTION_SECONDS,
    WORKSPACE_RETENTION_SECONDS,
)
from problem_locator.storage.coordination import (
    AttachmentUploadRegistry,
    StorageCoordinationLock,
)
from problem_locator.storage.layout import StorageLayout
from problem_locator.storage.paths import proposal_stage_path
from problem_locator.storage.quarantine import QuarantineMover
from problem_locator.storage.resource_store import StagePathRegistry
from problem_locator.storage.retention import RetentionScanner
from problem_locator.storage.retention_cleaner import StorageRetentionCleaner
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
)
from tests.unit.storage.fakes import (
    BarrierStorageCoordinationLock,
    FakeFileSync,
    FaultInjectingReplace,
    FixedClock,
)


FIXTURES = Path("tests/fixtures/contracts/positive")
NOW = "2026-08-10T12:00:00.000Z"
NOW_SECONDS = datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp()
CASE_ID = "00000000-0000-0000-0000-000000000001"
OUTCOME_JOB_ID = "00000000-0000-0000-0000-000000000011"
UNRELATED_UPLOAD_ID = "00000000-0000-0000-0000-000000000071"
OTHER_UPLOAD_ID = "00000000-0000-0000-0000-000000000072"
OLD_RESOURCE_ID = "00000000-0000-0000-0000-000000000073"
EXACT_RESOURCE_ID = "00000000-0000-0000-0000-000000000074"
TERMINAL_JOB_ID = "00000000-0000-0000-0000-000000000075"
CLEANUP_ID = "00000000-0000-0000-0000-000000000076"
PROCESSED_AT = "2026-08-01T00:00:00.000Z"


def _load_model(name: str, model: type[StateFile] | type[Job] | type[JobOutcome]):
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return model.model_validate(payload)


def _state() -> StateFile:
    return _load_model("state.json", StateFile)


def _outcome() -> JobOutcome:
    return _load_model("job-outcome-diagnosis.json", JobOutcome)


def _diagnose_job() -> Job:
    return _load_model("job-diagnose.json", Job)


def _set_age(path: Path, age_seconds: int) -> None:
    timestamp_ns = int((NOW_SECONDS - age_seconds) * 1_000_000_000)
    os.utime(path, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)


class SnapshotRepository:
    def __init__(
        self,
        state: StateFile,
        *,
        on_read: Callable[[int], None] | None = None,
    ) -> None:
        self.state = state
        self.read_calls = 0
        self.on_read = on_read

    def read_snapshot(self) -> StateFile:
        self.read_calls += 1
        if self.on_read is not None:
            self.on_read(self.read_calls)
        return self.state.model_copy(deep=True)


@dataclass(slots=True)
class Harness:
    layout: StorageLayout
    lock: StorageCoordinationLock | BarrierStorageCoordinationLock
    repository: SnapshotRepository
    resources: InMemoryResourceStore
    records: InMemoryExecutionRecordStore
    ids: DeterministicIdGenerator
    scanner: RetentionScanner
    mover: QuarantineMover
    stages: StagePathRegistry
    cleaner: StorageRetentionCleaner
    attachments: AttachmentUploadRegistry
    delete_failures: list[tuple[Path, BaseException]]


def _harness(
    tmp_path: Path,
    *,
    state: StateFile | None = None,
    lock: StorageCoordinationLock | BarrierStorageCoordinationLock | None = None,
    repository: SnapshotRepository | None = None,
) -> Harness:
    layout = StorageLayout.at(tmp_path / "data")
    layout.ensure_directories()
    shared_lock = lock or StorageCoordinationLock()
    selected_repository = repository or SnapshotRepository(state or _state())
    resources = InMemoryResourceStore()
    records = InMemoryExecutionRecordStore()
    ids = DeterministicIdGenerator(seed="retention-cleaner-tests")
    scanner = RetentionScanner(layout, FixedClock(NOW))
    mover = QuarantineMover(
        layout,
        shared_lock,
        FakeFileSync(),
        FaultInjectingReplace(),
    )
    stages = StagePathRegistry()
    attachments = AttachmentUploadRegistry()
    delete_failures: list[tuple[Path, BaseException]] = []
    cleaner = StorageRetentionCleaner(
        layout,
        shared_lock,
        selected_repository,  # type: ignore[arg-type]
        resources,
        records,
        ids,
        scanner,
        mover,
        stages,
        attachments,
        on_delete_failure=lambda path, error: delete_failures.append((path, error)),
    )
    return Harness(
        layout,
        shared_lock,
        selected_repository,
        resources,
        records,
        ids,
        scanner,
        mover,
        stages,
        cleaner,
        attachments,
        delete_failures,
    )


def _staged_directory(
    path: Path,
    *,
    age_seconds: int,
    marker_bytes: bytes | None = b"{}\n",
) -> Path:
    path.mkdir(parents=True)
    (path / "payload").write_bytes(b"staged")
    if marker_bytes is None:
        _set_age(path, age_seconds)
    else:
        marker = path / "staged.json"
        marker.write_bytes(marker_bytes)
        _set_age(marker, age_seconds)
    return path


def _formal_file(
    layout: StorageLayout,
    storage_key: str,
    *,
    age_seconds: int,
    payload: bytes = b"formal",
) -> Path:
    path = layout.data_root / storage_key
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    _set_age(path.parent, age_seconds)
    return path


def _published_outcome(harness: Harness) -> tuple[JobOutcome, object]:
    outcome = _outcome()
    payload = canonical_json_bytes(outcome)
    harness.records.publish_outcome_bytes(outcome.job_id, payload)
    job_directory = harness.layout.jobs / outcome.job_id
    job_directory.mkdir()
    (job_directory / "job_outcome.json").write_bytes(payload)
    _set_age(job_directory, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)
    receipt = harness.records.read_published_outcome(outcome.job_id)
    assert receipt is not None
    return outcome, receipt


def _outbox_objects(
    harness: Harness,
    outcome: JobOutcome,
) -> tuple[Path, Path, Path]:
    proposal = outcome.proposed_artifacts[0]
    stage = proposal_stage_path(
        harness.layout.data_root,
        proposal.staged_resource_ref.owner_job_id,
        proposal.proposal_key,
    )
    _staged_directory(
        stage,
        age_seconds=PROPOSAL_STAGING_RETENTION_SECONDS + 1,
        marker_bytes=canonical_json_bytes(proposal.staged_resource_ref),
    )
    resource_id = harness.ids.derive(
        "artifact",
        [
            harness.repository.state.installation_id,
            outcome.case_id,
            outcome.outcome_id,
            proposal.proposal_key,
        ],
    )
    target = harness.resources.plan_target(
        outcome.case_id,
        ResourceType.ARTIFACT,
        resource_id,
        proposal.staged_resource_ref.resource_kind,
        proposal.staged_resource_ref.size,
        proposal.staged_resource_ref.sha256,
    )
    formal = _formal_file(
        harness.layout,
        target.final_storage_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
    )
    next_job_id = harness.ids.derive(
        "job",
        [
            harness.repository.state.installation_id,
            outcome.case_id,
            outcome.outcome_id,
            "next_job",
        ],
    )
    next_job = harness.layout.jobs / next_job_id
    next_job.mkdir()
    (next_job / "job.json").write_bytes(b"prepublished")
    _set_age(next_job, ORPHAN_RESOURCE_RETENTION_SECONDS + 1)
    return stage, formal, next_job


def _with_processing_record(
    state: StateFile,
    outcome: JobOutcome,
    receipt: object,
    disposition: OutcomeDisposition,
    *,
    trusted_outcome: bool,
) -> StateFile:
    error_code = (
        ErrorCode.OUTCOME_INVALID
        if disposition is OutcomeDisposition.REJECTED
        else None
    )
    record = OutcomeProcessingRecord(
        outcome_id=outcome.outcome_id,
        job_id=outcome.job_id,
        outcome_hash=receipt.outcome_file_ref.sha256,
        outcome_file_ref=receipt.outcome_file_ref,
        disposition=disposition,
        processed_at=PROCESSED_AT,
        error_code=error_code,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="Outcome processing is complete.",
    )
    aggregate = state.cases[CASE_ID]
    jobs = dict(aggregate.jobs)
    jobs[outcome.job_id] = _diagnose_job().model_copy(
        update={
            "status": JobStatus.SUCCEEDED,
            "started_at": PROCESSED_AT,
            "finished_at": PROCESSED_AT,
            "runtime_epoch": "00000000-0000-0000-0000-000000000078",
        }
    )
    updated = aggregate.model_copy(
        update={
            "jobs": jobs,
            "outcomes": ({outcome.outcome_id: outcome} if trusted_outcome else {}),
            "outcome_processing_records": {outcome.outcome_id: record},
        },
        deep=True,
    )
    return state.model_copy(update={"cases": {CASE_ID: updated}}, deep=True)


def _port_error(code: ErrorCode) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message="Injected execution-record failure.",
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def test_strict_24_hour_and_7_day_candidates_are_deleted_only_after_threshold(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    old_upload = _staged_directory(
        harness.layout.uploads / UNRELATED_UPLOAD_ID,
        age_seconds=UPLOAD_TEMP_RETENTION_SECONDS + 1,
    )
    exact_upload = _staged_directory(
        harness.layout.uploads / OTHER_UPLOAD_ID,
        age_seconds=UPLOAD_TEMP_RETENTION_SECONDS,
    )
    old_key = (
        f"resources/cases/{CASE_ID}/evidence/{OLD_RESOURCE_ID}/payload"
    )
    exact_key = (
        f"resources/cases/{CASE_ID}/evidence/{EXACT_RESOURCE_ID}/payload"
    )
    old_resource = _formal_file(
        harness.layout,
        old_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
    )
    exact_resource = _formal_file(
        harness.layout,
        exact_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS,
    )

    result = harness.cleaner.run_once()

    assert not old_upload.exists()
    assert not old_resource.exists()
    assert exact_upload.exists()
    assert exact_resource.exists()
    assert len(result.deleted) == 2
    assert result.failed_deletions == ()


def test_unconfirmed_durable_outbox_protects_stage_target_and_next_job(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    outcome, _ = _published_outcome(harness)
    stage, formal, next_job = _outbox_objects(harness, outcome)
    unrelated = _staged_directory(
        harness.layout.uploads / UNRELATED_UPLOAD_ID,
        age_seconds=UPLOAD_TEMP_RETENTION_SECONDS + 1,
    )

    result = harness.cleaner.run_once()

    assert stage.exists()
    assert formal.exists()
    assert next_job.exists()
    assert not unrelated.exists()
    assert {stage, formal, next_job} <= set(result.skipped)
    assert any(
        call[1] is ResourceType.ARTIFACT
        for call in harness.resources.plan_target_calls
    )


@pytest.mark.parametrize(
    ("disposition", "trusted_outcome"),
    [
        (OutcomeDisposition.APPLIED, True),
        (OutcomeDisposition.STALE, True),
        (OutcomeDisposition.REJECTED, True),
        (OutcomeDisposition.REJECTED, False),
    ],
)
def test_every_persisted_terminal_disposition_stops_outbox_protection_without_reread(
    tmp_path: Path,
    disposition: OutcomeDisposition,
    trusted_outcome: bool,
) -> None:
    harness = _harness(tmp_path)
    outcome, receipt = _published_outcome(harness)
    stage, formal, next_job = _outbox_objects(harness, outcome)
    harness.repository.state = _with_processing_record(
        harness.repository.state,
        outcome,
        receipt,
        disposition,
        trusted_outcome=trusted_outcome,
    )
    # A technical REJECTED record may have no trusted Outcome.  More broadly,
    # cleanup never needs to parse any finalized file after a terminal audit.
    harness.records.inject_failure(
        "read_published_outcome",
        _port_error(ErrorCode.EXECUTION_RECORD_FAILED),
    )
    (harness.layout.jobs / outcome.job_id / "job_outcome.json").write_bytes(
        b"corrupt after disposition"
    )

    harness.cleaner.run_once()

    assert not stage.exists()
    assert not formal.exists()
    assert not next_job.exists()
    assert (harness.layout.jobs / outcome.job_id).exists()


@pytest.mark.parametrize("failure_mode", ["typed_error", "unexpected_none"])
def test_corrupt_unconfirmed_outcome_pauses_all_moves_and_existing_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    harness = _harness(tmp_path)
    outcome, _ = _published_outcome(harness)
    old_upload = _staged_directory(
        harness.layout.uploads / UNRELATED_UPLOAD_ID,
        age_seconds=UPLOAD_TEMP_RETENTION_SECONDS + 1,
    )
    old_state_temp = harness.layout.state_temporary / "old-state.tmp"
    old_state_temp.write_bytes(b"old")
    isolated = harness.mover.move_if(CLEANUP_ID, old_state_temp, lambda: True)
    assert isolated is not None

    if failure_mode == "typed_error":
        harness.records.inject_failure(
            "read_published_outcome",
            _port_error(ErrorCode.EXECUTION_RECORD_FAILED),
        )
    else:
        monkeypatch.setattr(
            harness.records,
            "read_published_outcome",
            lambda _job_id: None,
        )

    with pytest.raises(ApplicationPortError) as raised:
        harness.cleaner.run_once()

    assert raised.value.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert old_upload.exists()
    assert (harness.layout.jobs / outcome.job_id).exists()
    assert isolated.exists()


def test_markerless_active_stage_and_attachment_upload_lease_are_not_deleted(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    proposal = proposal_stage_path(
        harness.layout.data_root,
        OUTCOME_JOB_ID,
        "markerless-active",
    )
    _staged_directory(
        proposal,
        age_seconds=PROPOSAL_STAGING_RETENTION_SECONDS + 1,
        marker_bytes=None,
    )
    upload = _staged_directory(
        harness.layout.uploads / UNRELATED_UPLOAD_ID,
        age_seconds=UPLOAD_TEMP_RETENTION_SECONDS + 1,
    )
    attachment_lease = harness.attachments.acquire(UNRELATED_UPLOAD_ID)
    try:
        with harness.stages.acquire_stage(proposal):
            result = harness.cleaner.run_once()
    finally:
        attachment_lease.release()

    assert proposal.exists()
    assert upload.exists()
    assert proposal in result.skipped
    assert upload in result.skipped

    harness.cleaner.run_once()
    assert not proposal.exists()
    assert not upload.exists()


def test_completed_proposal_for_nonterminal_state_job_remains_pending_publish(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    pending_job_id = next(
        iter(harness.repository.state.cases[CASE_ID].jobs)
    )
    proposal = proposal_stage_path(
        harness.layout.data_root,
        pending_job_id,
        "completed-but-not-finalized",
    )
    _staged_directory(
        proposal,
        age_seconds=PROPOSAL_STAGING_RETENTION_SECONDS + 1,
    )

    result = harness.cleaner.run_once()

    assert proposal.exists()
    assert proposal in result.skipped


def test_state_references_and_nonterminal_workspaces_are_retained(
    tmp_path: Path,
) -> None:
    payload = b"referenced attachment"
    sha256 = hashlib.sha256(payload).hexdigest()
    storage_key = (
        f"resources/cases/{CASE_ID}/attachments/{OLD_RESOURCE_ID}/payload"
    )
    state = _state()
    aggregate = state.cases[CASE_ID]
    attachment = Attachment(
        attachment_id=OLD_RESOURCE_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name="input.log",
        content_type="text/plain",
        declared_size=len(payload),
        declared_sha256=sha256,
        size=len(payload),
        sha256=sha256,
        storage_key=storage_key,
        created_at=PROCESSED_AT,
        updated_at=PROCESSED_AT,
    )
    terminal_job = aggregate.jobs[next(iter(aggregate.jobs))].model_copy(
        update={
            "job_id": TERMINAL_JOB_ID,
            "status": JobStatus.SUCCEEDED,
            "started_at": PROCESSED_AT,
            "finished_at": PROCESSED_AT,
            "runtime_epoch": "00000000-0000-0000-0000-000000000077",
        }
    )
    jobs = dict(aggregate.jobs)
    jobs[TERMINAL_JOB_ID] = terminal_job
    updated = aggregate.model_copy(
        update={"attachments": {OLD_RESOURCE_ID: attachment}, "jobs": jobs},
        deep=True,
    )
    state = state.model_copy(update={"cases": {CASE_ID: updated}}, deep=True)
    harness = _harness(tmp_path, state=state)
    formal = _formal_file(
        harness.layout,
        storage_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
        payload=payload,
    )
    pending_workspace = harness.layout.workspaces / next(iter(aggregate.jobs))
    terminal_workspace = harness.layout.workspaces / TERMINAL_JOB_ID
    pending_workspace.mkdir()
    terminal_workspace.mkdir()
    _set_age(pending_workspace, WORKSPACE_RETENTION_SECONDS + 1)
    _set_age(terminal_workspace, WORKSPACE_RETENTION_SECONDS + 1)

    harness.cleaner.run_once()

    assert formal.exists()
    assert pending_workspace.exists()
    assert not terminal_workspace.exists()


def test_commit_that_wins_shared_lock_prevents_orphan_quarantine(
    tmp_path: Path,
) -> None:
    barrier = BarrierStorageCoordinationLock()
    state = _state()
    repository = SnapshotRepository(state)
    harness = _harness(tmp_path, lock=barrier, repository=repository)
    payload = b"published before commit"
    sha256 = hashlib.sha256(payload).hexdigest()
    storage_key = (
        f"resources/cases/{CASE_ID}/attachments/{OLD_RESOURCE_ID}/payload"
    )
    formal = _formal_file(
        harness.layout,
        storage_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
        payload=payload,
    )
    attachment = Attachment(
        attachment_id=OLD_RESOURCE_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name="input.log",
        content_type="text/plain",
        declared_size=len(payload),
        declared_sha256=sha256,
        size=len(payload),
        sha256=sha256,
        storage_key=storage_key,
        created_at=PROCESSED_AT,
        updated_at=PROCESSED_AT,
    )

    barrier.acquire()
    barrier.arm_checkpoint("before_acquire")
    failure: list[BaseException] = []

    def clean() -> None:
        try:
            harness.cleaner.run_once()
        except BaseException as error:  # pragma: no cover - asserted below
            failure.append(error)

    worker = threading.Thread(target=clean, daemon=True)
    worker.start()
    assert barrier.wait_for_checkpoint("before_acquire", timeout_seconds=2)
    aggregate = repository.state.cases[CASE_ID]
    updated = aggregate.model_copy(
        update={"attachments": {OLD_RESOURCE_ID: attachment}},
        deep=True,
    )
    repository.state = repository.state.model_copy(
        update={"cases": {CASE_ID: updated}},
        deep=True,
    )
    barrier.release()
    barrier.release_checkpoint("before_acquire")
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert failure == []
    assert formal.exists()


def test_cleanup_that_wins_shared_lock_cannot_be_followed_by_dangling_commit(
    tmp_path: Path,
) -> None:
    second_read_entered = threading.Event()
    allow_second_read = threading.Event()

    def on_read(call_number: int) -> None:
        if call_number == 2:
            second_read_entered.set()
            assert allow_second_read.wait(timeout=3)

    repository = SnapshotRepository(_state(), on_read=on_read)
    harness = _harness(tmp_path, repository=repository)
    storage_key = (
        f"resources/cases/{CASE_ID}/evidence/{OLD_RESOURCE_ID}/payload"
    )
    formal = _formal_file(
        harness.layout,
        storage_key,
        age_seconds=ORPHAN_RESOURCE_RETENTION_SECONDS + 1,
    )
    cleanup_failure: list[BaseException] = []
    commit_observed_formal: list[bool] = []

    def clean() -> None:
        try:
            harness.cleaner.run_once()
        except BaseException as error:  # pragma: no cover - asserted below
            cleanup_failure.append(error)

    def commit_if_present() -> None:
        with harness.lock:
            commit_observed_formal.append(formal.exists())

    cleaner_thread = threading.Thread(target=clean, daemon=True)
    cleaner_thread.start()
    assert second_read_entered.wait(timeout=2)
    commit_thread = threading.Thread(target=commit_if_present, daemon=True)
    commit_thread.start()
    allow_second_read.set()
    cleaner_thread.join(timeout=3)
    commit_thread.join(timeout=3)

    assert cleanup_failure == []
    assert not cleaner_thread.is_alive() and not commit_thread.is_alive()
    assert commit_observed_formal == [False]
    assert not formal.exists()


def test_failed_recursive_delete_is_reported_and_retried_next_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    old_state_temp = harness.layout.state_temporary / "old-state.tmp"
    old_state_temp.write_bytes(b"old")
    _set_age(old_state_temp, UPLOAD_TEMP_RETENTION_SECONDS + 1)
    real_delete = harness.mover.delete
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected recursive delete failure")
        real_delete(path)

    monkeypatch.setattr(harness.mover, "delete", fail_once)
    first = harness.cleaner.run_once()

    assert len(first.failed_deletions) == 1
    assert len(harness.delete_failures) == 1
    assert harness.mover.discover() == first.failed_deletions

    monkeypatch.setattr(harness.mover, "delete", real_delete)
    second = harness.cleaner.run_once()
    assert second.failed_deletions == ()
    assert harness.mover.discover() == ()
