from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CONTRACT_REVISION,
    SCHEMA_VERSION,
    ApplicationPortError,
    Attachment,
    AttachmentStatus,
    ErrorCode,
    ExecutionFileRef,
    Job,
    JobOutcome,
    OutcomeDisposition,
    OutcomeProcessingRecord,
    RecoveryProcessingRecord,
    RuntimeEpochRecord,
    StateFile,
    StateMutation,
    StateRepository,
    canonical_json_bytes,
)
from problem_locator.storage.atomic import read_stable_file_bytes
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES, StorageLayout
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.deterministic.contracts.fakes import InMemoryExecutionRecordStore
from tests.deterministic.unit.storage.fakes import (
    DeterministicIdGenerator,
    FakeFileSync,
    FaultInjectingReplace,
    FixedClock,
)


CONTRACT_FIXTURES = Path("tests/fixtures/contracts/positive")
CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000010"
OUTCOME_ID = "00000000-0000-0000-0000-000000000020"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000040"
RECOVERY_ID = "00000000-0000-0000-0000-000000000080"
RUNTIME_EPOCH = "00000000-0000-0000-0000-000000000081"
PENDING_JOB_ID = "00000000-0000-0000-0000-000000000011"
INITIAL_TIME = "2026-07-31T00:00:00.000Z"
COMMIT_TIME = "2026-07-31T00:01:00.000Z"
COMPLETED_TIME = "2026-07-31T00:02:00.000Z"


class _MutatingStateReader:
    """Corrupt the just-replaced state before the commit verification read."""

    def __init__(self) -> None:
        self._replacement: bytes | None = None
        self._armed_reads = 0

    def arm(self, replacement: bytes) -> None:
        self._replacement = replacement
        self._armed_reads = 0

    def __call__(self, path: Path) -> bytes:
        replacement = self._replacement
        if replacement is not None and path.name == "state.json":
            self._armed_reads += 1
            if self._armed_reads == 2:
                path.write_bytes(replacement)
                self._replacement = None
                raise OSError("injected post-replace state damage")
        return read_stable_file_bytes(path)


def _load_json(name: str) -> dict:
    return json.loads((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))


def _positive_state() -> StateFile:
    return StateFile.model_validate(_load_json("state.json"))


def _empty_mutation(**updates: object) -> StateMutation:
    payload: dict[str, object] = {
        "upsert_case": None,
        "upsert_runtime_epoch_records": [],
        "upsert_recovery_processing_records": [],
        "insert_jobs": [],
        "job_lifecycle_updates": [],
        "insert_outcomes": [],
        "insert_outcome_processing_records": [],
        "insert_execution_failure_records": [],
        "upsert_attachments": [],
        "insert_evidence": [],
        "insert_artifacts": [],
        "insert_idempotency_records": [],
    }
    payload.update(updates)
    return StateMutation.model_validate(payload)


def _recovery_mutation(
    *,
    completed_at: str | None = None,
    pending_job_ids: list[str] | None = None,
) -> StateMutation:
    record = RecoveryProcessingRecord(
        recovery_id=RECOVERY_ID,
        current_runtime_epoch=RUNTIME_EPOCH,
        interrupted_job_ids=[],
        pending_job_ids=(
            [PENDING_JOB_ID] if pending_job_ids is None else pending_job_ids
        ),
        completed_at=completed_at,
    )
    runtime = RuntimeEpochRecord(
        runtime_epoch=RUNTIME_EPOCH,
        started_at=INITIAL_TIME,
        recovery_id=RECOVERY_ID,
        recovery_completed_at=completed_at,
    )
    return _empty_mutation(
        upsert_runtime_epoch_records=[runtime],
        upsert_recovery_processing_records=[record],
    )


def _repository(
    tmp_path: Path,
    *,
    clock: FixedClock | None = None,
    file_sync: FakeFileSync | None = None,
    replacer: FaultInjectingReplace | None = None,
    execution_store: InMemoryExecutionRecordStore | None = None,
) -> tuple[
    JsonFileStateRepository,
    FixedClock,
    FakeFileSync,
    FaultInjectingReplace,
    InMemoryExecutionRecordStore,
]:
    selected_clock = clock or FixedClock(INITIAL_TIME)
    selected_sync = file_sync or FakeFileSync()
    selected_replacer = replacer or FaultInjectingReplace()
    selected_records = execution_store or InMemoryExecutionRecordStore()
    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        selected_clock,
        DeterministicIdGenerator(seed="state-repository-tests"),
        file_sync=selected_sync,
        replacer=selected_replacer,
        execution_record_store=selected_records,
    )
    return (
        repository,
        selected_clock,
        selected_sync,
        selected_replacer,
        selected_records,
    )


def _write_state(layout: StorageLayout, state: StateFile | dict) -> None:
    if not layout.data_format_marker.exists():
        layout.data_format_marker.write_bytes(DATA_FORMAT_MARKER_BYTES)
    layout.state.write_bytes(canonical_json_bytes(state))


def _assert_port_error(
    expected_code: ErrorCode,
    callback: object,
) -> ApplicationPortError:
    with pytest.raises(ApplicationPortError) as raised:
        callback()  # type: ignore[operator]
    assert raised.value.error.code is expected_code
    return raised.value


def test_empty_directory_initializes_generation_one_canonical_state(
    tmp_path: Path,
) -> None:
    repository, clock, sync, replacer, _ = _repository(tmp_path)

    snapshot = repository.read_snapshot()
    assert isinstance(repository, StateRepository)
    assert snapshot.schema_version == SCHEMA_VERSION == 5
    assert snapshot.contract_revision == CONTRACT_REVISION == "v5-contract-r1"
    assert snapshot.generation == 1
    assert snapshot.created_at == INITIAL_TIME
    assert snapshot.updated_at == INITIAL_TIME
    assert snapshot.cases == {}
    assert snapshot.recovery_processing_records == {}
    assert repository.export_snapshot() == repository.layout.state.read_bytes()
    assert repository.layout.previous_state.exists() is False
    assert repository.layout.data_format_marker.read_bytes() == DATA_FORMAT_MARKER_BYTES
    assert clock.calls == 1
    assert sync.count("sync_file") == 2
    assert [event.destination for event in replacer.events] == [
        repository.layout.state
    ]


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b'{"truncated":\n', ErrorCode.STATE_CORRUPT),
        (
            canonical_json_bytes(
                {
                    "schema_version": 5,
                    "contract_revision": "v2-contract-r1",
                }
            ),
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        ),
        (
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "contract_revision": "v1-contract-r4",
                }
            ),
            ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        ),
    ],
)
def test_startup_rejects_corrupt_or_unsupported_state_without_prev_fallback(
    tmp_path: Path,
    payload: bytes,
    expected_code: ErrorCode,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    layout.data_format_marker.write_bytes(DATA_FORMAT_MARKER_BYTES)
    layout.state.write_bytes(payload)
    layout.previous_state.write_bytes(canonical_json_bytes(_positive_state()))

    _assert_port_error(
        expected_code,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="rejected-state"),
            execution_record_store=InMemoryExecutionRecordStore(),
        ),
    )
    assert layout.state.read_bytes() == payload


def test_missing_state_with_previous_or_business_content_is_corrupt(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    layout.data_format_marker.write_bytes(DATA_FORMAT_MARKER_BYTES)
    layout.previous_state.write_bytes(b"administrator recovery candidate\n")

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="missing-current"),
            execution_record_store=InMemoryExecutionRecordStore(),
        ),
    )
    assert not layout.state.exists()


def test_unmarked_pre_v2_state_is_rejected_without_adoption_or_rewrite(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "legacy-data"
    data_root.mkdir()
    layout = StorageLayout.at(data_root)
    legacy = canonical_json_bytes(
        {"contract_revision": "v2-contract-r1", "schema_version": 2}
    )
    layout.state.write_bytes(legacy)
    original_entries = tuple(data_root.iterdir())

    error = _assert_port_error(
        ErrorCode.STATE_SCHEMA_UNSUPPORTED,
        lambda: JsonFileStateRepository(
            data_root,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="unmarked-pre-v2"),
            execution_record_store=InMemoryExecutionRecordStore(),
        ),
    )

    assert "fresh DATA_ROOT" in error.error.message
    assert layout.state.read_bytes() == legacy
    assert tuple(data_root.iterdir()) == original_entries == (layout.state,)
    assert not layout.data_format_marker.exists()


def test_mismatched_data_format_marker_is_corrupt_and_never_rewritten(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    legacy_marker = b'{"format_id":"problem-locator-data-v2","schema_version":2}\n'
    layout.data_format_marker.write_bytes(legacy_marker)

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="mismatched-marker"),
            execution_record_store=InMemoryExecutionRecordStore(),
        ),
    )

    assert layout.data_format_marker.read_bytes() == legacy_marker
    assert not layout.state.exists()


def test_startup_requires_each_state_job_to_match_published_pending_manifest(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    state = _positive_state()
    _write_state(layout, state)
    records = InMemoryExecutionRecordStore()

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="missing-job-record"),
            execution_record_store=records,
        ),
    )

    job = state.cases[CASE_ID].jobs[JOB_ID]
    records.publish_job(job)
    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="present-job-record"),
        execution_record_store=records,
    )
    assert repository.read_job(JOB_ID) == job


def test_lifecycle_state_job_is_checked_against_immutable_pending_job_record(
    tmp_path: Path,
) -> None:
    state_payload = _load_json("state.json")
    aggregate = state_payload["cases"][CASE_ID]
    pending_job = Job.model_validate(aggregate["jobs"][JOB_ID])
    aggregate["jobs"][JOB_ID].update(
        status="RUNNING",
        started_at=COMMIT_TIME,
        runtime_epoch=RUNTIME_EPOCH,
    )
    state = StateFile.model_validate(state_payload)
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    _write_state(layout, state)
    records = InMemoryExecutionRecordStore()
    records.publish_job(pending_job)

    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="running-job-record"),
        execution_record_store=records,
    )
    assert repository.read_job(JOB_ID).status.value == "RUNNING"


def test_untrusted_technical_rejection_does_not_require_parseable_outcome_file(
    tmp_path: Path,
) -> None:
    state_payload = _load_json("state.json")
    aggregate = state_payload["cases"][CASE_ID]
    outcome_hash = "a" * 64
    aggregate["outcome_processing_records"] = {
        OUTCOME_ID: {
            "outcome_id": OUTCOME_ID,
            "job_id": JOB_ID,
            "outcome_hash": outcome_hash,
            "outcome_file_ref": {
                "relative_key": f"jobs/{JOB_ID}/job_outcome.json",
                "size": 17,
                "sha256": outcome_hash,
            },
            "disposition": "REJECTED",
            "processed_at": COMMIT_TIME,
            "error_code": "OUTCOME_MISSING",
            "accepted_evidence_ids": [],
            "accepted_artifact_ids": [],
            "created_job_id": None,
            "reason": "The finalized Outcome could not be trusted.",
        }
    }
    state = StateFile.model_validate(state_payload)
    records = InMemoryExecutionRecordStore()
    records.publish_job(state.cases[CASE_ID].jobs[JOB_ID])
    records.inject_failure(
        "read_published_outcome",
        AssertionError("untrusted rejection must not read Outcome bytes"),
    )
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    _write_state(layout, state)

    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="untrusted-outcome-audit"),
        execution_record_store=records,
    )
    record = repository.read_snapshot().cases[CASE_ID].outcome_processing_records[
        OUTCOME_ID
    ]
    assert record.disposition is OutcomeDisposition.REJECTED
    assert record.error_code is ErrorCode.OUTCOME_MISSING


def test_trusted_saved_outcome_must_match_its_published_receipt(
    tmp_path: Path,
) -> None:
    state_payload = _load_json("state.json")
    aggregate = state_payload["cases"][CASE_ID]
    outcome = JobOutcome.model_validate(_load_json("job-outcome-route.json"))
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_ref = ExecutionFileRef(
        relative_key=f"jobs/{JOB_ID}/job_outcome.json",
        size=len(outcome_bytes),
        sha256=hashlib.sha256(outcome_bytes).hexdigest(),
    )
    record = OutcomeProcessingRecord(
        outcome_id=OUTCOME_ID,
        job_id=JOB_ID,
        outcome_hash=outcome_ref.sha256,
        outcome_file_ref=outcome_ref,
        disposition=OutcomeDisposition.APPLIED,
        processed_at=COMMIT_TIME,
        error_code=None,
        accepted_evidence_ids=[],
        accepted_artifact_ids=[],
        created_job_id=None,
        reason="The route Outcome was committed.",
    )
    aggregate["outcomes"] = {OUTCOME_ID: outcome.model_dump(mode="json")}
    aggregate["outcome_processing_records"] = {
        OUTCOME_ID: record.model_dump(mode="json")
    }
    state = StateFile.model_validate(state_payload)
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    _write_state(layout, state)
    records = InMemoryExecutionRecordStore()
    records.publish_job(state.cases[CASE_ID].jobs[JOB_ID])

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="missing-trusted-outcome"),
            execution_record_store=records,
        ),
    )

    records.publish_outcome_bytes(JOB_ID, outcome_bytes)
    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="present-trusted-outcome"),
        execution_record_store=records,
    )
    assert repository.read_snapshot().cases[CASE_ID].outcomes[OUTCOME_ID] == outcome


def _state_with_ready_attachment(payload: bytes) -> StateFile:
    state_payload = _load_json("state.json")
    sha256 = hashlib.sha256(payload).hexdigest()
    attachment = Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.READY,
        name="diagnostic.log",
        content_type="text/plain",
        declared_size=len(payload),
        declared_sha256=sha256,
        size=len(payload),
        sha256=sha256,
        storage_key=(
            f"resources/cases/{CASE_ID}/attachments/{ATTACHMENT_ID}/payload"
        ),
        created_at=INITIAL_TIME,
        updated_at=COMMIT_TIME,
    )
    state_payload["cases"][CASE_ID]["attachments"] = {
        ATTACHMENT_ID: attachment.model_dump(mode="json")
    }
    return StateFile.model_validate(state_payload)


def test_startup_validates_state_referenced_resource_identity_bytes_and_mode(
    tmp_path: Path,
) -> None:
    payload = b"immutable attachment bytes"
    state = _state_with_ready_attachment(payload)
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    _write_state(layout, state)
    records = InMemoryExecutionRecordStore()
    records.publish_job(state.cases[CASE_ID].jobs[JOB_ID])

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="missing-resource"),
            execution_record_store=records,
        ),
    )

    target = (
        layout.cases_resources
        / CASE_ID
        / "attachments"
        / ATTACHMENT_ID
        / "payload"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.chmod(0o444)
    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="valid-resource"),
        execution_record_store=records,
    )
    assert repository.validate_all().valid

    target.chmod(0o644)
    assert repository.validate_all().valid is False
    _assert_port_error(ErrorCode.STATE_CORRUPT, repository.export_snapshot)


def test_commit_applies_fake_equivalent_structure_and_returns_deep_copies(
    tmp_path: Path,
) -> None:
    repository, clock, _, _, records = _repository(tmp_path)
    fixture = _positive_state()
    aggregate = fixture.cases[CASE_ID]
    records.publish_job(aggregate.jobs[JOB_ID])
    clock.set(COMMIT_TIME)
    mutation = _empty_mutation(
        upsert_case=aggregate.case,
        insert_jobs=list(aggregate.jobs.values()),
    )

    receipt = repository.commit(1, None, mutation)

    assert receipt.generation == 2
    assert receipt.case_revision == 1
    assert repository.read_case(CASE_ID) == aggregate
    first = repository.read_snapshot()
    first.cases.clear()
    second = repository.read_snapshot()
    assert CASE_ID in second.cases
    assert second.updated_at == COMMIT_TIME
    assert StateFile.model_validate_json(repository.layout.state.read_bytes()) == second
    previous = StateFile.model_validate_json(
        repository.layout.previous_state.read_bytes()
    )
    assert previous.generation == 1

    restarted = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        clock,
        DeterministicIdGenerator(seed="state-restart"),
        execution_record_store=records,
    )
    assert restarted.read_snapshot() == second


def test_missing_reads_and_generation_or_case_conflicts_use_exact_port_codes(
    tmp_path: Path,
) -> None:
    repository, _, _, _, _ = _repository(tmp_path)

    _assert_port_error(
        ErrorCode.CASE_NOT_FOUND,
        lambda: repository.read_case(CASE_ID),
    )
    _assert_port_error(
        ErrorCode.JOB_NOT_FOUND,
        lambda: repository.read_job(JOB_ID),
    )
    _assert_port_error(
        ErrorCode.ARTIFACT_NOT_FOUND,
        lambda: repository.read_artifact(ATTACHMENT_ID),
    )
    _assert_port_error(
        ErrorCode.REVISION_CONFLICT,
        lambda: repository.commit(0, None, _empty_mutation()),
    )

    fixture = _positive_state()
    aggregate = fixture.cases[CASE_ID]
    repository._execution_record_store.publish_job(  # type: ignore[attr-defined]
        aggregate.jobs[JOB_ID]
    )
    _assert_port_error(
        ErrorCode.REVISION_CONFLICT,
        lambda: repository.commit(
            1,
            99,
            _empty_mutation(
                upsert_case=aggregate.case,
                insert_jobs=list(aggregate.jobs.values()),
            ),
        ),
    )
    assert repository.read_snapshot().generation == 1


def test_recovery_record_exact_replay_and_one_way_completion_are_persisted(
    tmp_path: Path,
) -> None:
    repository, clock, _, _, _ = _repository(tmp_path)
    clock.set(COMMIT_TIME)
    initial = _recovery_mutation()

    assert repository.commit(1, None, initial).generation == 2
    assert repository.commit(2, None, initial).generation == 3
    completed = _recovery_mutation(completed_at=COMPLETED_TIME)
    assert repository.commit(3, None, completed).generation == 4
    saved = repository.read_snapshot()
    assert saved.recovery_processing_records[RECOVERY_ID].completed_at == COMPLETED_TIME
    assert saved.runtime_epochs[0].recovery_completed_at == COMPLETED_TIME

    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(4, None, initial),
    )
    assert repository.read_snapshot() == saved

    changed_first_receipt = _recovery_mutation(
        completed_at=COMPLETED_TIME,
        pending_job_ids=[],
    )
    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(4, None, changed_first_receipt),
    )
    assert repository.read_snapshot() == saved


def test_runtime_epoch_identity_cannot_be_rewritten_after_first_receipt(
    tmp_path: Path,
) -> None:
    repository, _, _, _, _ = _repository(tmp_path)
    assert repository.commit(1, None, _recovery_mutation()).generation == 2
    completed = _recovery_mutation(completed_at=COMPLETED_TIME)
    assert repository.commit(2, None, completed).generation == 3
    saved = repository.read_snapshot()
    drifted_runtime = RuntimeEpochRecord(
        runtime_epoch=RUNTIME_EPOCH,
        started_at=COMMIT_TIME,
        recovery_id=RECOVERY_ID,
        recovery_completed_at=COMPLETED_TIME,
    )

    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(
            3,
            None,
            _empty_mutation(upsert_runtime_epoch_records=[drifted_runtime]),
        ),
    )

    assert repository.read_snapshot() == saved


def test_pre_replace_failure_reloads_unchanged_disk_truth(tmp_path: Path) -> None:
    repository, _, sync, _, _ = _repository(tmp_path)
    sync.fail_next("sync_file", OSError("new state temp sync failed"))

    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(1, None, _recovery_mutation()),
    )

    assert repository.read_snapshot().generation == 1
    assert StateFile.model_validate_json(repository.layout.state.read_bytes()).generation == 1
    assert repository.layout.previous_state.exists() is False


def test_post_replace_directory_sync_failure_reloads_new_disk_truth(
    tmp_path: Path,
) -> None:
    repository, _, sync, _, _ = _repository(tmp_path)
    sync.fail_next("sync_directory", OSError("DATA_ROOT sync failed"))

    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(1, None, _recovery_mutation()),
    )

    in_memory = repository.read_snapshot()
    on_disk = StateFile.model_validate_json(repository.layout.state.read_bytes())
    assert in_memory == on_disk
    assert on_disk.generation == 2
    assert RECOVERY_ID in on_disk.recovery_processing_records
    assert StateFile.model_validate_json(
        repository.layout.previous_state.read_bytes()
    ).generation == 1


@pytest.mark.parametrize(
    "expected_code",
    [ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED],
)
def test_failed_commit_reload_fail_stops_every_read_with_exact_state_error(
    tmp_path: Path,
    expected_code: ErrorCode,
) -> None:
    reader = _MutatingStateReader()
    repository = JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(INITIAL_TIME),
        DeterministicIdGenerator(seed="fail-stopped-state"),
        file_sync=FakeFileSync(),
        replacer=FaultInjectingReplace(),
        execution_record_store=InMemoryExecutionRecordStore(),
        read_file=reader,
    )
    healthy_bytes = repository.layout.state.read_bytes()
    if expected_code is ErrorCode.STATE_CORRUPT:
        damaged_bytes = b'{"truncated":\n'
    else:
        old_revision = json.loads(healthy_bytes.decode("utf-8"))
        old_revision["contract_revision"] = "v1-contract-r2"
        damaged_bytes = canonical_json_bytes(old_revision)
    reader.arm(damaged_bytes)

    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(1, None, _recovery_mutation()),
    )
    assert repository.layout.state.read_bytes() == damaged_bytes

    for callback in (
        repository.read_snapshot,
        lambda: repository.read_case(CASE_ID),
        lambda: repository.read_job(JOB_ID),
        lambda: repository.read_artifact(ATTACHMENT_ID),
    ):
        error = _assert_port_error(expected_code, callback)
        assert error.error.retryable is False

    report = repository.validate_all()
    assert report.valid is False
    assert report.errors[0].code == expected_code.value
    _assert_port_error(expected_code, repository.export_snapshot)
    _assert_port_error(
        ErrorCode.STATE_WRITE_FAILED,
        lambda: repository.commit(1, None, _empty_mutation()),
    )

    # An online repair can be validated, but this repository instance remains
    # fail-stopped until a restart reconstructs its in-memory authority.
    repository.layout.state.write_bytes(healthy_bytes)
    assert repository.validate_all().valid
    _assert_port_error(expected_code, repository.read_snapshot)


def test_validate_and_export_recheck_authoritative_disk_without_mutating_snapshot(
    tmp_path: Path,
) -> None:
    repository, _, _, _, _ = _repository(tmp_path)
    valid = repository.validate_all()
    assert valid.valid
    assert valid.generation == 1
    assert valid.object_counts.recovery_processing_records == 0

    repository.layout.state.write_bytes(b'{"truncated":\n')
    invalid = repository.validate_all()
    assert invalid.valid is False
    assert invalid.errors[0].code == ErrorCode.STATE_CORRUPT.value
    _assert_port_error(ErrorCode.STATE_CORRUPT, repository.export_snapshot)
    assert repository.read_snapshot().generation == 1


def test_storage_key_owner_drift_is_rejected_even_when_state_dto_parses(
    tmp_path: Path,
) -> None:
    payload = b"resource owner drift"
    state = _state_with_ready_attachment(payload)
    state_payload = state.model_dump(mode="python")
    wrong_id = "00000000-0000-0000-0000-000000000041"
    state_payload["cases"][CASE_ID]["attachments"][ATTACHMENT_ID][
        "storage_key"
    ] = f"resources/cases/{CASE_ID}/attachments/{wrong_id}/payload"
    drifted = StateFile.model_validate(state_payload)
    layout = StorageLayout.at(tmp_path)
    layout.ensure_directories()
    _write_state(layout, drifted)
    records = InMemoryExecutionRecordStore()
    records.publish_job(drifted.cases[CASE_ID].jobs[JOB_ID])

    _assert_port_error(
        ErrorCode.STATE_CORRUPT,
        lambda: JsonFileStateRepository(
            tmp_path,
            StorageCoordinationLock(),
            FixedClock(INITIAL_TIME),
            DeterministicIdGenerator(seed="owner-drift"),
            execution_record_store=records,
        ),
    )
