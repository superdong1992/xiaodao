"""V10 repository contract: active RAM, terminal SQLite, indexed history.

The former state.json replacement/replay tests are superseded by these tests,
test_case_store_v10.py (1k/10k history and independent revisions), and the real
process-crash/async-archive integration tests. No legacy DATA_ROOT is adopted.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from problem_locator.contracts import (
    CONTRACT_REVISION, SCHEMA_VERSION, ApplicationPortError, Case, CaseStatus,
    ErrorCode, StateFile, StateMutation, StateRepository, canonical_json_bytes,
)
from problem_locator.storage.state_repository import CaseStateRepository
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES, StorageLayout
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FixedClock

CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000010"
INITIAL_TIME = "2026-07-31T00:00:00.000Z"
COMMIT_TIME = "2026-07-31T00:01:00.000Z"


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


def _open(root):
    return CaseStateRepository(root, StorageCoordinationLock(), FixedClock(COMMIT_TIME),
        DeterministicIdGenerator(seed="state-v10"), file_sync=FakeFileSync())


@pytest.fixture
def repository(tmp_path):
    store = _open(tmp_path)
    yield store
    store.close()


def _populate(store):
    state = StateFile.model_validate_json(Path("tests/fixtures/contracts/positive/state.json").read_bytes())
    aggregate = state.cases[CASE_ID]
    receipt = store.commit(1, None, _empty_mutation(upsert_case=aggregate.case,
        insert_jobs=list(aggregate.jobs.values())))
    assert receipt.case_revision == 1
    return aggregate


def _finish(store):
    before = store.read_snapshot(CASE_ID)
    case = before.cases[CASE_ID].case
    return store.commit(before.generation, case.case_revision,
        _empty_mutation(upsert_case=Case.model_validate({**case.model_dump(),
            "status": CaseStatus.CANCELLED, "active_job_id": None,
            "case_revision": case.case_revision + 1})))


def test_empty_directory_initializes_generation_one_canonical_state(repository):
    snapshot = repository.read_snapshot()
    assert isinstance(repository, StateRepository)
    assert snapshot.schema_version == SCHEMA_VERSION == 10
    assert snapshot.contract_revision == CONTRACT_REVISION == "v10-contract-r1"
    assert snapshot.generation == 1 and snapshot.cases == {}
    assert repository.export_snapshot() == canonical_json_bytes(snapshot)
    assert repository.layout.data_format_marker.read_bytes() == DATA_FORMAT_MARKER_BYTES
    assert not repository.layout.state.exists()
    assert not repository.layout.previous_state.exists()
    assert repository._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repository._db.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.parametrize("legacy_version", range(1, 10))
def test_v1_through_v8_state_is_read_only_and_unsupported(tmp_path, legacy_version):
    # Name retained because affected Test Flow selects this guard explicitly.
    layout = StorageLayout.at(tmp_path)
    payload = canonical_json_bytes({"schema_version": legacy_version,
        "contract_revision": f"v{legacy_version}-contract-r1"})
    layout.state.write_bytes(payload)
    before = set(tmp_path.iterdir())
    with pytest.raises(ApplicationPortError) as error:
        _open(tmp_path)
    assert error.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert layout.state.read_bytes() == payload
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("marker", [b"{}", b"broken", b'{"format_id":"problem-locator-data-v2","schema_version":2}\n'])
def test_mismatched_data_format_marker_is_never_rewritten(tmp_path, marker):
    (tmp_path / "data-format.json").write_bytes(marker)
    with pytest.raises(ApplicationPortError) as error:
        _open(tmp_path)
    assert error.value.error.code in {ErrorCode.STATE_CORRUPT, ErrorCode.STATE_SCHEMA_UNSUPPORTED}
    assert (tmp_path / "data-format.json").read_bytes() == marker
    assert not (tmp_path / "completed.sqlite3").exists()


def test_corrupt_database_is_rejected_without_json_fallback(tmp_path):
    store = _open(tmp_path)
    store.close()
    path = tmp_path / "completed.sqlite3"
    path.write_bytes(b"broken sqlite database")
    with pytest.raises(ApplicationPortError) as error:
        _open(tmp_path)
    assert error.value.error.code is ErrorCode.STATE_CORRUPT
    assert path.read_bytes() == b"broken sqlite database"


def test_commit_applies_fake_equivalent_structure_and_returns_deep_copies(repository):
    expected = _populate(repository)
    assert repository.read_case(CASE_ID) == expected
    assert repository.read_job(JOB_ID) == expected.jobs[JOB_ID]
    snapshot = repository.read_snapshot(CASE_ID)
    snapshot.cases.clear()
    assert repository.read_case(CASE_ID) == expected
    assert not repository.layout.state.exists()
    restarted = _open(repository.layout.data_root)
    try:
        assert restarted.read_snapshot().cases == {}
    finally:
        restarted.close()


def test_missing_reads_and_generation_or_case_conflicts_use_exact_port_codes(repository):
    for code, call in [
        (ErrorCode.CASE_NOT_FOUND, lambda: repository.read_case(CASE_ID)),
        (ErrorCode.JOB_NOT_FOUND, lambda: repository.read_job(JOB_ID)),
        (ErrorCode.ARTIFACT_NOT_FOUND, lambda: repository.read_artifact(CASE_ID)),
        (ErrorCode.REVISION_CONFLICT, lambda: repository.commit(0, None, _empty_mutation())),
    ]:
        with pytest.raises(ApplicationPortError) as caught:
            call()
        assert caught.value.error.code is code
    _populate(repository)
    before = repository.read_snapshot(CASE_ID)
    with pytest.raises(ApplicationPortError) as caught:
        repository.commit(before.generation, 99, _empty_mutation(upsert_case=before.cases[CASE_ID].case))
    assert caught.value.error.code is ErrorCode.REVISION_CONFLICT
    assert repository.read_snapshot(CASE_ID) == before


def test_terminal_failure_rolls_back_all_indexes_and_keeps_existing_reports_readable(repository):
    _populate(repository)
    repository._db.execute("CREATE TRIGGER fail_index BEFORE INSERT ON object_index BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(ApplicationPortError) as caught:
        _finish(repository)
    assert caught.value.error.code is ErrorCode.STATE_WRITE_FAILED
    assert repository._db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0
    assert repository._db.execute("SELECT count(*) FROM object_index").fetchone()[0] == 0
    assert repository.read_case(CASE_ID).case.status is CaseStatus.RUNNING
    assert not repository.health().valid
    with pytest.raises(ApplicationPortError):
        repository.commit(1, None, _empty_mutation())


def test_terminal_index_and_metadata_lookup_do_not_parse_history(repository, monkeypatch):
    _populate(repository)
    _finish(repository)
    assert repository._live == {}
    assert repository._objects == {}
    assert repository.retention_in_use("job", JOB_ID)
    assert not repository.retention_in_use("active_job", JOB_ID)
    before = repository._load_case
    monkeypatch.setattr(repository, "_load_case", lambda *args: pytest.fail("history loaded"))
    assert repository.retention_in_use("job", JOB_ID)
    assert not repository.retention_in_use("resource", f"resources/cases/{CASE_ID}/artifacts/{JOB_ID}/payload")
    assert repository.health().valid
    monkeypatch.setattr(repository, "_load_case", before)
    assert repository.read_job(JOB_ID).case_id == CASE_ID


def test_corrupt_case_is_detected_on_demand_and_does_not_require_startup_scan(repository):
    _populate(repository)
    _finish(repository)
    repository._db.execute("UPDATE completed_cases SET snapshot=? WHERE case_id=?", (b"invalid json", CASE_ID))
    restarted = _open(repository.layout.data_root)
    try:
        assert restarted.health().valid
        with pytest.raises(ApplicationPortError) as caught:
            restarted.read_case(CASE_ID)
        assert caught.value.error.code is ErrorCode.STATE_CORRUPT
        assert not restarted.health().valid
    finally:
        restarted.close()


def test_explicit_validate_and_export_include_terminal_history(repository):
    _populate(repository)
    _finish(repository)
    assert repository.validate_all().object_counts.cases == 1
    snapshot = StateFile.model_validate_json(repository.export_snapshot())
    assert snapshot.cases[CASE_ID].case.status is CaseStatus.CANCELLED
    assert repository._live == {}
