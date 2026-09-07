"""Actual RPC report publication, delayed archives and SQLite restart behavior."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from problem_locator.contracts import ArtifactKind, CaseStatus
from problem_locator.dispatch.archive import ArchiveService
from problem_locator.storage.state_repository import CaseStateRepository
from tests.deterministic.contracts.fakes import InMemoryStateChangeNotifier
from tests.deterministic.journey.test_rpc_timeout import (
    _Stack, _mcp, _query, _windows_extended_path, ARCHIVE, PARAMETER_GROUP_A,
)


def create_pending_archive(root: Path):
    # Every consumer uses the same controlled directory. Normalizing here also
    # covers standalone pytest and crash-child callers whose basetemp is not an
    # extended Windows path; deep immutable staging names can exceed MAX_PATH.
    if os.name == 'nt':
        root = _windows_extended_path(root)
    stack = _Stack(root, logparse_record=root.parent/'logparse.json',
        agent_record=root.parent/'agent.jsonl', review_entered=root.parent/'entered',
        review_release=root.parent/'released', seed='archive-v10')
    (root.parent/'released').write_text('pass\n', encoding='utf-8')
    created = _mcp(stack.mcp, 'problem_locator_create_case', {
        'request_id': 'archive-create', 'raw_problem_text': 'RPC timeout',
        'statement': 'RPC timeout', 'expected_behavior': 'RPC completes',
        'actual_behavior': 'RPC timeout', 'scope': 'payment-to-inventory RPC',
        'goals': ['Identify timeout'], 'non_goals': [], 'constraints': [],
        'completion_criteria': ['Find evidence'], 'initial_user_fact_names': ['order_id'],
        'initial_user_fact_values': ['synthetic-order-0001'], 'wait_seconds': 0})
    case_id = created['business_receipt']['case_id']
    stack.start()
    assert stack.scheduler.wait_until_idle(10)
    view = _query(stack.mcp, case_id)
    assert view['status'] == CaseStatus.WAITING_INPUT.value, view
    _mcp(stack.mcp, 'problem_locator_submit_supplement', {
        'request_id': 'archive-inputs', 'case_id': case_id, 'expected_case_revision': view['case_revision'],
        'input_names': list(PARAMETER_GROUP_A), 'input_values': list(PARAMETER_GROUP_A.values()),
        'attachment_ids': [], 'wait_seconds': 0})
    view = _query(stack.mcp, case_id)
    data = ARCHIVE.read_bytes()
    prepared = _mcp(stack.mcp, 'problem_locator_prepare_attachment', {
        'request_id': 'archive-prepare', 'case_id': case_id, 'expected_case_revision': view['case_revision'],
        'name': 'rpc.zip', 'content_type': 'application/zip', 'declared_size': len(data),
        'declared_sha256': hashlib.sha256(data).hexdigest()})
    upload = prepared['upload']
    with TestClient(stack.http_app) as client:
        response = client.put(f"/api/v1/attachments/{upload['attachment_id']}/content", content=data,
            headers={key: value for key, value in upload['required_headers'].items() if value is not None})
    assert response.status_code == 200, response.text
    _mcp(stack.mcp, 'problem_locator_submit_supplement', {
        'request_id': 'archive-diagnose', 'case_id': case_id,
        'expected_case_revision': response.json()['data']['case_revision'],
        'input_names': [], 'input_values': [], 'attachment_ids': [upload['attachment_id']], 'wait_seconds': 0})
    assert stack.scheduler.wait_until_idle(15)
    assert stack.scheduler.fatal_worker_error_type is None
    return stack, case_id


@pytest.fixture
def pending(tmp_path):
    stack, case_id = create_pending_archive(tmp_path/'data')
    try:
        view = _query(stack.mcp, case_id)
        assert view['status'] == 'RESOLVED', view
        assert view['archive_status'] == 'PENDING'
        yield stack, case_id
    finally:
        stack.shutdown()


def _report(stack, case_id):
    aggregate = stack.repository.read_case(case_id)
    report = next(item for item in aggregate.artifacts.values() if item.kind is ArtifactKind.USER_RESULT)
    data = (stack.data_root/report.storage_key).read_bytes()
    assert hashlib.sha256(data).hexdigest() == report.sha256
    assert json.loads(data)['status'] == 'COMPLETED'
    return report, data


def test_archive_helper_supports_deep_staging_from_unprefixed_root(tmp_path_factory):
    root = tmp_path_factory.mktemp('archive-long') / ('a' * 100) / 'data'
    # Formal Test Flow may already provide an extended basetemp. Remove that
    # prefix only from this test input so the helper owns its normalization.
    plain = str(root)
    if plain.startswith('\\\\?\\UNC\\'):
        root = Path('\\\\' + plain[8:])
    elif plain.startswith('\\\\?\\'):
        root = Path(plain[4:])
    # The Logparse test checkout itself stays short enough for Git; the deep
    # generated resource names, which caused the regression, exceed MAX_PATH.
    assert not str(root).startswith('\\\\?\\')
    parent = _windows_extended_path(root.parent) if os.name == 'nt' else root.parent
    parent.mkdir(parents=True)
    stack, case_id = create_pending_archive(root)
    try:
        if os.name == 'nt':
            assert str(stack.data_root).startswith('\\\\?\\')
        assert _query(stack.mcp, case_id)['archive_status'] == 'PENDING'
        assert any(len(str(path).removeprefix('\\\\?\\')) > 260 for path in stack.data_root.rglob('*'))
        report, report_bytes = _report(stack, case_id)
        assert stack.archive.run_once()
        assert _query(stack.mcp, case_id)['archive_status'] == 'READY'
        assert _report(stack, case_id)[1] == report_bytes
        archive = next(item for item in stack.repository.read_case(case_id).artifacts.values()
            if item.kind is ArtifactKind.USER_RESULT_ARCHIVE)
        with zipfile.ZipFile(stack.data_root / archive.storage_key) as result:
            assert result.testzip() is None
            for source in report.metadata.archive_plan.logs:
                content = result.read(source.archive_name)
                assert len(content) == source.size
                assert hashlib.sha256(content).hexdigest() == source.sha256
    finally:
        stack.shutdown()


def test_json_is_delivered_while_archive_worker_is_delayed(pending, monkeypatch):
    stack, case_id = pending
    report, before = _report(stack, case_id)
    entered, release = threading.Event(), threading.Event()
    original = stack.resources.stage_archive

    def delay(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(stack.resources, 'stage_archive', delay)
    worker = threading.Thread(target=stack.archive.run_once)
    worker.start()
    try:
        assert entered.wait(2)
        compact = _mcp(stack.mcp, 'problem_locator_get_case', {'case_id': case_id})
        assert compact['case_view']['archive_status'] == 'PENDING'
        assert compact['artifact_views']
        assert _report(stack, case_id)[1] == before
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert _query(stack.mcp, case_id)['archive_status'] == 'READY'
    assert _report(stack, case_id)[1] == before


def test_archive_crash_after_file_publication_reuses_the_same_bytes_on_restart(pending, monkeypatch):
    stack, case_id = pending
    original = stack.archive._set_status

    def crash_before_ready(selected_case, status, artifact=None):
        if status == 'READY':
            raise SystemExit('simulated crash after ZIP fsync')
        original(selected_case, status, artifact)

    monkeypatch.setattr(stack.archive, '_set_status', crash_before_ready)
    with pytest.raises(SystemExit):
        stack.archive.run_once()
    report, before = _report(stack, case_id)
    stack.repository.close()
    stack.repository = CaseStateRepository(stack.data_root, stack.coordination_lock, stack.clock, stack.ids)
    resumed = ArchiveService(stack.repository, stack.resources, stack.publication_guard,
        InMemoryStateChangeNotifier(), stack.clock)
    assert resumed.run_once()
    assert stack.repository.read_case(case_id).case.archive_status == 'READY'
    assert _report(stack, case_id)[1] == before
    archive = next(item for item in stack.repository.read_case(case_id).artifacts.values()
        if item.kind is ArtifactKind.USER_RESULT_ARCHIVE)
    with zipfile.ZipFile(stack.data_root/archive.storage_key) as result:
        for source in report.metadata.archive_plan.logs:
            content = result.read(source.archive_name)
            assert len(content) == source.size
            assert hashlib.sha256(content).hexdigest() == source.sha256
        assert result.testzip() is None


def test_zip_failure_does_not_revoke_delivered_report(pending, monkeypatch):
    stack, case_id = pending
    report, before = _report(stack, case_id)

    def fail(*args, **kwargs):
        raise OSError('injected ZIP disk failure')

    monkeypatch.setattr(stack.resources, 'stage_archive', fail)
    assert stack.archive.run_once()
    view = _query(stack.mcp, case_id)
    assert (view['status'], view['archive_status']) == ('RESOLVED', 'FAILED')
    assert _report(stack, case_id)[1] == before
    assert not stack.archive.run_once()


@pytest.mark.parametrize('claimed', [False, True])
def test_pending_or_interrupted_archive_resumes_after_database_reopen(pending, claimed):
    stack, case_id = pending
    _, before = _report(stack, case_id)
    if claimed:
        assert stack.repository.claim_archive_task()[0] == case_id
    stack.repository.close()
    stack.repository = CaseStateRepository(stack.data_root, stack.coordination_lock, stack.clock, stack.ids)
    archive = ArchiveService(stack.repository, stack.resources, stack.publication_guard,
        InMemoryStateChangeNotifier(), stack.clock)
    assert not stack.repository._live
    assert stack.repository.read_case(case_id).case.archive_status == 'PENDING'
    assert _report(stack, case_id)[1] == before
    assert archive.run_once()
    assert stack.repository.read_case(case_id).case.archive_status == 'READY'
    assert _report(stack, case_id)[1] == before
