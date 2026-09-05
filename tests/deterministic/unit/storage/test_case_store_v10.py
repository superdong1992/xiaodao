from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from problem_locator.contracts import ApplicationPortError, Case, CaseStatus, ErrorCode, Job, StateFile
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import CaseStateRepository
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FixedClock
from tests.deterministic.unit.storage.test_state_repository import _empty_mutation

_TIME = '2026-07-31T00:01:00.000Z'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


@pytest.fixture
def repository(tmp_path):
    store = CaseStateRepository(tmp_path, StorageCoordinationLock(), FixedClock(_TIME),
        DeterministicIdGenerator(seed='case-store-v10'), file_sync=FakeFileSync())
    yield store
    store.close()


def _case(index: int):
    raw = Path('tests/fixtures/contracts/positive/state.json').read_text(encoding='utf-8')
    raw = _UUID.sub(lambda match: str(uuid5(NAMESPACE_URL, f'{index}/{match[0]}')), raw)
    state = StateFile.model_validate_json(raw)
    return next(iter(state.cases.values()))


def _create(repository, index):
    aggregate = _case(index)
    before = repository.read_snapshot(request_key=f'new:{index}')
    repository.commit(before.generation, None, _empty_mutation(upsert_case=aggregate.case,
        insert_jobs=list(aggregate.jobs.values())))
    return aggregate.case.case_id


def _cancel(repository, case_id):
    before = repository.read_snapshot(case_id)
    case = before.cases[case_id].case
    replacement = Case.model_validate({**case.model_dump(), 'status': CaseStatus.CANCELLED,
        'active_job_id': None, 'case_revision': case.case_revision + 1, 'updated_at': _TIME})
    repository.commit(before.generation, case.case_revision, _empty_mutation(upsert_case=replacement))


def test_active_updates_do_not_write_sqlite_or_state_json(repository):
    statements = []
    repository._db.set_trace_callback(statements.append)
    case_id = _create(repository, 1)
    snapshot = repository.read_snapshot(case_id)
    case = snapshot.cases[case_id].case
    replacement = case.model_copy(update={'case_revision': case.case_revision + 1})
    repository.commit(snapshot.generation, case.case_revision, _empty_mutation(upsert_case=replacement))
    assert not any(sql.startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN', 'COMMIT')) for sql in statements)
    assert not repository.layout.state.exists()
    assert not repository.layout.previous_state.exists()


def test_unrelated_cases_have_independent_revision_domains(repository):
    left, right = _create(repository, 1), _create(repository, 2)
    snapshots = [repository.read_snapshot(case_id) for case_id in (left, right)]
    for case_id, snapshot in zip((left, right), snapshots):
        case = snapshot.cases[case_id].case
        repository.commit(snapshot.generation, case.case_revision,
            _empty_mutation(upsert_case=case.model_copy(update={'case_revision': case.case_revision + 1})))
    assert set(snapshots[0].cases) == {left}
    assert set(snapshots[1].cases) == {right}
    with pytest.raises(ApplicationPortError) as failure:
        case = snapshots[0].cases[left].case
        repository.commit(snapshots[0].generation, case.case_revision, _empty_mutation(upsert_case=case))
    assert failure.value.error.code is ErrorCode.REVISION_CONFLICT


def test_only_completed_work_survives_reopen(repository):
    active, completed = _create(repository, 1), _create(repository, 2)
    _cancel(repository, completed)
    assert completed not in repository._live
    reopened = CaseStateRepository(repository.layout.data_root, StorageCoordinationLock(),
        FixedClock(_TIME), DeterministicIdGenerator(seed='reopened'), file_sync=FakeFileSync())
    try:
        assert reopened.read_case(completed).case.status is CaseStatus.CANCELLED
        with pytest.raises(ApplicationPortError) as failure:
            reopened.read_case(active)
        assert failure.value.error.code is ErrorCode.CASE_NOT_FOUND
        assert not reopened._live
    finally:
        reopened.close()


@pytest.mark.parametrize('history_count', [1000, 10000])
def test_history_is_not_loaded_or_serialized_by_an_active_update(repository, history_count, monkeypatch):
    seed = _create(repository, 0)
    _cancel(repository, seed)
    raw = repository._db.execute('SELECT snapshot FROM completed_cases WHERE case_id=?', (seed,)).fetchone()[0].decode()
    rows = []
    for index in range(1, history_count + 1):
        mapped = {value: str(uuid5(NAMESPACE_URL, f'history/{index}/{value}')) for value in set(_UUID.findall(raw))}
        serialized = _UUID.sub(lambda match: mapped[match[0]], raw)
        rows.append((mapped[seed], serialized.encode()))
    with repository._database_lock:
        repository._db.execute('BEGIN')
        repository._db.executemany('INSERT INTO completed_cases VALUES (?, ?)', rows)
        repository._db.execute('COMMIT')
    observed = []
    repository._db.set_trace_callback(observed.append)
    active = _create(repository, history_count + 1)
    original = repository._apply_mutation

    def only_one_case(state, revision, mutation):
        assert set(state.cases) == {active}
        return original(state, revision, mutation)

    monkeypatch.setattr(repository, '_apply_mutation', only_one_case)
    snapshot = repository.read_snapshot(active)
    case = snapshot.cases[active].case
    repository.commit(snapshot.generation, case.case_revision,
        _empty_mutation(upsert_case=case.model_copy(update={'case_revision': case.case_revision + 1})))
    assert not any('SELECT snapshot FROM completed_cases' in sql and active not in sql for sql in observed)
    assert len(repository._live) == 1


def test_v9_directory_is_rejected_without_modifying_it(tmp_path):
    marker = tmp_path/'data-format.json'
    old = b'{"contract_revision":"v9-contract-r1","format_id":"problem-locator-data-v2","schema_version":2,"state_schema_version":9}\n'
    marker.write_bytes(old)
    with pytest.raises(ApplicationPortError) as failure:
        CaseStateRepository(tmp_path, StorageCoordinationLock(), FixedClock(_TIME),
            DeterministicIdGenerator(seed='old-root'), file_sync=FakeFileSync())
    assert failure.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert marker.read_bytes() == old
    assert set(tmp_path.iterdir()) == {marker}


def test_retention_discovery_does_not_hash_payloads_or_walk_resource_trees(repository, monkeypatch):
    import os
    from problem_locator.storage import resource_files
    from problem_locator.storage.retention import RetentionScanner
    case_id = _create(repository, 1)
    resource_id = str(uuid5(NAMESPACE_URL, 'retention-resource'))
    payload = repository.layout.cases_resources / case_id / 'artifacts' / resource_id / 'tree'
    payload.mkdir(parents=True)
    (payload / 'large.log').write_bytes(b'x' * 1048576)
    os.utime(payload.parent, (1, 1))
    monkeypatch.setattr(resource_files, '_inspect_physical_resource',
        lambda *args, **kwargs: pytest.fail('retention read historical payload bytes'))
    candidates = RetentionScanner(repository.layout, FixedClock(_TIME)).discover()
    assert any(candidate.path == payload for candidate in candidates)
