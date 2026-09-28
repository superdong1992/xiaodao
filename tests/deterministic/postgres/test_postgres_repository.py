"""Real PostgreSQL storage, concurrent commits and owner-fencing regressions."""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.bootstrap import ServiceStateAdmin
from problem_locator.contracts import ApplicationPortError, CaseStatus, ErrorCode
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import CaseStateRepository
from problem_locator.storage.postgres_layout import validate_postgres_root
from tests.postgres_helpers import postgres_database_url, postgres_repository
from tests.deterministic.unit.storage.test_case_store_v10 import _create, _cancel


def _open(root, database_url):
    return CaseStateRepository(root, StorageCoordinationLock(),
        SimpleNamespace(now=lambda: "2026-09-28T00:00:00.000Z"),
        SimpleNamespace(new=lambda kind: str(uuid.uuid4())), database_url=database_url)


def test_empty_postgres_database_and_root_share_one_installation(postgres_repository):
    repository = postgres_repository
    marker = validate_postgres_root(repository.layout)
    with repository.database_read() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        version = int(db.execute("SHOW server_version_num").fetchone()[0])
        assert db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0
    assert version >= 170000
    assert metadata["installation_id"] == marker["installation_id"] == repository.read_snapshot().installation_id
    assert metadata["storage_backend"] == "postgresql-v1"
    assert not (repository.layout.data_root / "completed.sqlite3").exists()
    assert repository.health().valid


def test_postgres_terminal_commit_survives_restart_but_active_case_does_not(postgres_repository, postgres_database_url):
    repository = postgres_repository
    active, finished = _create(repository, 1), _create(repository, 2)
    _cancel(repository, finished)
    original = repository.export_snapshot()
    root = repository.layout.data_root
    repository.close()
    reopened = _open(root, postgres_database_url)
    try:
        assert reopened.read_case(finished).case.status is CaseStatus.CANCELLED
        assert active not in reopened.read_snapshot().cases
        # Export before restart included the active Case only in process memory.
        assert active in json.loads(original)["cases"]
        assert reopened.validate_all().object_counts.cases == 1
    finally:
        reopened.close()


def test_postgres_terminal_projection_failure_rolls_back_snapshot_and_indexes(postgres_repository):
    import psycopg
    repository = postgres_repository
    case_id = _create(repository, 3)

    def failure(db, state):
        assert db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM object_index").fetchone()[0] > 0
        raise psycopg.OperationalError("injected projection failure")

    repository.on_case_projection = failure
    with pytest.raises(ApplicationPortError) as caught:
        _cancel(repository, case_id)
    assert caught.value.error.code is ErrorCode.STATE_WRITE_FAILED
    with repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM object_index").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM completed_case_retention").fetchone()[0] == 0
    assert repository.read_case(case_id).case.status is CaseStatus.RUNNING
    assert not repository.health().valid


def test_postgres_other_case_commits_while_first_persistence_transaction_is_open(postgres_repository):
    repository = postgres_repository
    first, second = _create(repository, 4), _create(repository, 5)
    entered, release = threading.Event(), threading.Event()

    def hold_first(db, state):
        if first in state.cases:
            entered.set()
            assert release.wait(10)

    repository.on_case_projection = hold_first
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(_cancel, repository, first)
        try:
            assert entered.wait(5)
            # The first database transaction is still open. A repository-wide
            # mutex or shared connection cannot complete this second commit.
            workers.submit(_cancel, repository, second).result(timeout=5)
            assert not pending.done()
            with repository.database_read() as db:
                persisted = {row[0] for row in db.execute("SELECT case_id FROM completed_cases")}
            assert persisted == {second}
        finally:
            release.set()
        pending.result(timeout=5)
    assert repository.read_case(first).case.status is CaseStatus.CANCELLED
    assert repository.read_case(second).case.status is CaseStatus.CANCELLED


def test_postgres_concurrent_archive_claims_never_duplicate_a_task(postgres_repository):
    repository = postgres_repository
    cases = [str(uuid.uuid4()) for _ in range(18)]
    with repository.database_transaction() as db:
        db.executemany("INSERT INTO archive_tasks(case_id,status,payload,error) VALUES (?,'PENDING',?,NULL)",
                       ((case_id, b'{}') for case_id in cases))
    barrier = threading.Barrier(6)

    def drain():
        barrier.wait(timeout=10)
        claimed = []
        while (task := repository.claim_archive_task()) is not None:
            claimed.append(task[0])
        return claimed

    with ThreadPoolExecutor(max_workers=6) as workers:
        claimed = [case_id for batch in workers.map(lambda _: drain(), range(6)) for case_id in batch]
    assert len(claimed) == len(set(claimed)) == len(cases)
    assert set(claimed) == set(cases)


def test_postgres_archive_publication_and_agent_delete_share_one_lock(postgres_repository):
    repository = postgres_repository
    agent = AgentStore(repository)
    conversation = agent.create_conversation("archive-publication")
    case_id = str(uuid.uuid4())
    agent.bind_case(conversation.conversation_id, case_id)
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()

    def publish():
        with repository.archive_publication(case_id):
            entered.set()
            assert release.wait(10)
            assert not repository.is_agent_case_deleted(case_id)

    def delete():
        deleting.set()
        return agent.request_delete(conversation.conversation_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        publication = workers.submit(publish)
        try:
            assert entered.wait(5)
            deletion = workers.submit(delete)
            assert deleting.wait(5)
            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.2)
        finally:
            release.set()
        publication.result(timeout=5)
        deletion.result(timeout=5)
    assert repository.is_agent_case_deleted(case_id)
    with pytest.raises(InterruptedError):
        with repository.archive_publication(case_id):
            pytest.fail("deleted conversation was allowed to publish")


def test_postgres_owner_lock_rejects_second_server_without_resetting_work(postgres_repository, postgres_database_url):
    repository = postgres_repository
    case_id = str(uuid.uuid4())
    with repository.database_transaction() as db:
        db.execute("INSERT INTO archive_tasks(case_id,status,payload,error) VALUES (?,'RUNNING',?,NULL)", (case_id, b'{}'))
    marker = repository.layout.data_format_marker.read_bytes()
    with pytest.raises(ApplicationPortError) as caught:
        _open(repository.layout.data_root, postgres_database_url)
    assert caught.value.error.code is ErrorCode.INSTANCE_LOCKED
    assert repository.layout.data_format_marker.read_bytes() == marker
    with repository.database_read() as db:
        assert db.execute("SELECT status FROM archive_tasks WHERE case_id=?", (case_id,)).fetchone()[0] == "RUNNING"


def test_lost_postgres_owner_session_fails_readiness(postgres_repository):
    repository = postgres_repository
    admin = ServiceStateAdmin(layout=repository.layout,
        instance_lock=SimpleNamespace(is_acquired=lambda: True),
        coordination_lock=StorageCoordinationLock(), repository=repository,
        scheduler=SimpleNamespace(ready=True, operational_state=None))
    assert admin.readiness().ready
    repository._database._owner.close()
    report = admin.readiness()
    assert not report.ready
    assert report.error.code is ErrorCode.STATE_CORRUPT
    assert not next(check for check in report.checks if check.name == "STATE").passed
    assert "postgresql://" not in report.model_dump_json()


def test_new_postgres_owner_waits_for_old_transactions_after_owner_session_loss(postgres_repository, postgres_database_url):
    import psycopg
    repository = postgres_repository
    task_id = str(uuid.uuid4())
    with repository.database_transaction() as db:
        db.execute("INSERT INTO archive_tasks(case_id,status,payload,error) VALUES (?,'PENDING',?,NULL)", (task_id, b'{}'))
    entered, release, starting = threading.Event(), threading.Event(), threading.Event()

    def old_transaction():
        with repository.database_transaction() as db:
            db.execute("UPDATE archive_tasks SET status='RUNNING' WHERE case_id=?", (task_id,))
            entered.set()
            assert release.wait(10)

    def takeover():
        starting.set()
        return _open(repository.layout.data_root, postgres_database_url)

    replacement = None
    with ThreadPoolExecutor(max_workers=2) as workers:
        original = workers.submit(old_transaction)
        try:
            assert entered.wait(5)
            with psycopg.connect(postgres_database_url, autocommit=True) as admin:
                assert admin.execute("SELECT pg_terminate_backend(%s)", (repository._database._owner_pid,)).fetchone()[0]
            future = workers.submit(takeover)
            assert starting.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.3)
        finally:
            release.set()
        original.result(timeout=5)
        replacement = future.result(timeout=10)
    try:
        # Recovery must follow the old commit; it cannot reset the queue before
        # an in-flight old transaction changes it back to RUNNING.
        with replacement.database_read() as db:
            assert db.execute("SELECT status FROM archive_tasks WHERE case_id=?", (task_id,)).fetchone()[0] == 'PENDING'
        assert not repository.health().valid
        with pytest.raises(repository.database_errors):
            with repository.database_transaction():
                pytest.fail("the retired owner acquired a new transaction")
    finally:
        replacement.close()


def test_live_owner_connection_without_its_advisory_lock_is_fenced(postgres_repository):
    repository = postgres_repository
    # Simulate accidental administrative unlock while the dedicated session
    # stays connected. A socket ping or PID check alone must not authorize work.
    repository._database._owner.execute("SELECT pg_advisory_unlock_all()")
    assert not repository.health().valid
    with pytest.raises(repository.database_errors):
        with repository.database_transaction():
            pytest.fail("an unlocked owner was allowed to mutate the database")


def test_postgres_refuses_another_resource_root_without_changing_database(postgres_repository, postgres_database_url, tmp_path):
    import psycopg
    repository = postgres_repository
    root = repository.layout.data_root
    marker = repository.layout.data_format_marker.read_bytes()
    with repository.database_read() as db:
        before = dict(db.execute("SELECT key,value FROM metadata"))
    repository.close()
    wrong_root = tmp_path / "unrelated-resources"
    with pytest.raises(ApplicationPortError) as caught:
        _open(wrong_root, postgres_database_url)
    assert caught.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert not wrong_root.exists()
    assert (root / "data-format.json").read_bytes() == marker
    with psycopg.connect(postgres_database_url) as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == before
    reopened = _open(root, postgres_database_url)
    reopened.close()


def test_postgres_refuses_marker_bound_to_missing_database_identity(postgres_database_url, tmp_path):
    from problem_locator.storage.postgres_layout import initialize_postgres_root
    from problem_locator.storage.layout import StorageLayout
    from tests.deterministic.unit.storage.fakes import FakeFileSync
    import psycopg

    root = tmp_path / "marked"
    layout = StorageLayout.at(root)
    initialize_postgres_root(layout, str(uuid.uuid4()), FakeFileSync())
    marker = layout.data_format_marker.read_bytes()
    with pytest.raises(ApplicationPortError) as caught:
        _open(root, postgres_database_url)
    assert caught.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert layout.data_format_marker.read_bytes() == marker
    with psycopg.connect(postgres_database_url) as db:
        assert db.execute("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'").fetchone()[0] == 0
