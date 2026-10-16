from contextlib import nullcontext
import sqlite3
from types import SimpleNamespace

import pytest

from problem_locator.storage.completed_workspaces import CompletedWorkspacePolicy

JOB = "10000000-0000-0000-0000-000000000001"
CASE = "20000000-0000-0000-0000-000000000001"


@pytest.fixture
def setup_policy():
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE object_index(object_id TEXT, case_id TEXT);"
        "CREATE TABLE archive_tasks(case_id TEXT,status TEXT);"
        "CREATE TABLE agent_followup_tasks(workspace_id TEXT,status TEXT);"
        "CREATE TABLE agent_followup_snapshots(source_job_id TEXT,status TEXT);")
    db.execute("INSERT INTO object_index VALUES (?,?)", (JOB, CASE))
    case = SimpleNamespace(status="RESOLVED", active_job_id=None, archive_status="READY")
    job = SimpleNamespace(status="SUCCEEDED")
    state = SimpleNamespace(cases={CASE: SimpleNamespace(case=case, jobs={JOB: job})})
    repository = SimpleNamespace(database_read=lambda: nullcontext(db), read_snapshot=lambda _: state)
    followups = SimpleNamespace(enabled=False, store=SimpleNamespace(workspace_in_use=lambda _: False))
    policy = CompletedWorkspacePolicy(repository, followups)
    yield SimpleNamespace(policy=policy, db=db, case=case, job=job, followups=followups)
    db.close()


def test_success_requires_snapshot_and_zip_consumers_to_finish(setup_policy):
    s = setup_policy
    assert s.policy(JOB)
    s.followups.enabled = True
    assert not s.policy(JOB)
    s.db.execute("INSERT INTO agent_followup_snapshots VALUES (?, 'BUILDING')", (JOB,))
    assert not s.policy(JOB)
    s.db.execute("UPDATE agent_followup_snapshots SET status='READY'")
    assert s.policy(JOB) and s.policy(JOB + ".logparse-preprocess")
    s.db.execute("UPDATE agent_followup_snapshots SET status='UNAVAILABLE'")
    assert not s.policy(JOB)
    s.db.execute("UPDATE agent_followup_snapshots SET status='READY'")
    s.case.archive_status = "PENDING"
    assert not s.policy(JOB)
    s.case.archive_status = "READY"
    s.followups.store.workspace_in_use = lambda _: True
    assert not s.policy(JOB)


@pytest.mark.parametrize("target,attribute,value", [
    ("case", "status", "WAITING_INPUT"), ("case", "status", "FAILED"),
    ("case", "active_job_id", JOB), ("job", "status", "RUNNING"),
    ("job", "status", "FAILED"), ("case", "archive_status", "FAILED"),
])
def test_unsuccessful_or_unfinished_workspaces_wait_for_normal_ttl(setup_policy, target, attribute, value):
    setattr(getattr(setup_policy, target), attribute, value)
    assert not setup_policy.policy(JOB)


def test_successful_followup_workspace_is_reclaimable_but_failed_one_is_not(setup_policy):
    s = setup_policy
    s.db.execute("INSERT INTO agent_followup_tasks VALUES (?, 'COMPLETED')", (JOB,))
    assert s.policy(JOB)
    s.db.execute("UPDATE agent_followup_tasks SET status='FAILED'")
    assert not s.policy(JOB)


def test_live_archive_worker_protects_successful_workspace(setup_policy):
    setup_policy.db.execute("INSERT INTO archive_tasks VALUES (?, 'RUNNING')", (CASE,))
    assert not setup_policy.policy(JOB)
