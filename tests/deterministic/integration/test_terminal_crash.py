"""Kill a real RPC journey at the terminal SQLite boundary, without graceful close."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from problem_locator.contracts import ApplicationPortError, CaseStatus, ErrorCode
from problem_locator.storage.state_repository import CaseStateRepository
from problem_locator.storage.coordination import StorageCoordinationLock
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FixedClock


def crash_child(root: Path, boundary: str) -> None:
    from tests.deterministic.integration.test_async_archive import create_pending_archive
    original = CaseStateRepository._persist

    def persist(store, case_id, state):
        aggregate = state.cases[case_id]
        if aggregate.case.status is not CaseStatus.RESOLVED:
            return original(store, case_id, state)
        report = next(item for item in aggregate.artifacts.values() if item.kind.value == 'USER_RESULT')
        metadata = {'case_id': case_id, 'artifact_id': report.artifact_id,
                    'storage_key': report.storage_key, 'size': report.size, 'sha256': report.sha256,
                    'data_root': str(store.layout.data_root)}
        if boundary == 'before_commit':
            def trace(sql):
                if sql == 'COMMIT':
                    (root / 'boundary.json').write_text(json.dumps(metadata), encoding='utf-8')
                    os._exit(71)
            store._db.set_trace_callback(trace)
        original(store, case_id, state)
        # Represents the acknowledgement boundary: report bytes + FULL transaction
        # already durable, no graceful shutdown/checkpoint/recovery can help us.
        with (root / 'boundary.json').open('w', encoding='utf-8') as stream:
            json.dump(metadata, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(72)

    CaseStateRepository._persist = persist
    create_pending_archive(root / 'data')
    raise AssertionError('terminal boundary was never reached')


@pytest.mark.parametrize('boundary,exit_code', [('before_commit', 71), ('after_commit', 72)])
def test_terminal_report_survives_only_after_durable_commit(tmp_path, boundary, exit_code):
    import hashlib
    completed = subprocess.run([sys.executable, '-m', 'tests.deterministic.integration.test_terminal_crash', str(tmp_path), boundary],
        capture_output=True, text=True, timeout=45)
    assert completed.returncode == exit_code, completed.stdout + completed.stderr
    metadata = json.loads((tmp_path / 'boundary.json').read_text(encoding='utf-8'))
    data_root = Path(metadata['data_root'])
    # Both boundaries happen after resource publication. SQLite decides availability.
    report_bytes = (data_root / metadata['storage_key']).read_bytes()
    assert len(report_bytes) == metadata['size']
    assert hashlib.sha256(report_bytes).hexdigest() == metadata['sha256']
    repository = CaseStateRepository(data_root, StorageCoordinationLock(),
        FixedClock('2026-09-05T00:00:00.000Z'), DeterministicIdGenerator(seed='crash-reader'))
    try:
        if boundary == 'before_commit':
            with pytest.raises(ApplicationPortError) as caught:
                repository.read_case(metadata['case_id'])
            assert caught.value.error.code is ErrorCode.CASE_NOT_FOUND
            assert repository.claim_archive_task() is None
        else:
            case = repository.read_case(metadata['case_id'])
            assert case.case.status is CaseStatus.RESOLVED
            assert case.case.archive_status == 'PENDING'
            assert repository.read_artifact(metadata['artifact_id']).sha256 == metadata['sha256']
            assert repository.claim_archive_task()[0] == metadata['case_id']
        assert repository._live == {}
    finally:
        repository.close()


if __name__ == '__main__':
    crash_child(Path(sys.argv[1]), sys.argv[2])
