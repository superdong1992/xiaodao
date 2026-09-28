"""Followups share the report expiry; cleanup protects and removes exact inputs."""
import hashlib
from dataclasses import replace

from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.store import FollowupStore
from tests.deterministic.unit.storage.test_history_retention import (
    stack, _close_run, _file, OLD, NOW, BOUNDARY, CASE_ID, JOB_ID,
)

OWNER = "a" * 64


def _source():
    return FollowupSource(case_id=CASE_ID, source_job_id=JOB_ID, problem_text="原问题",
        report_markdown="# 原报告", report_sha256=hashlib.sha256(b"report").hexdigest(),
        source_kind="GENERIC", has_logs=True, occurred_at=OLD)


def _followups(stack):
    followups = FollowupStore(stack.repository, stack.clock)
    stack.store.followup_store = followups
    created = stack.store.create_conversation("create", owner_key=OWNER)
    _close_run(stack.store, created.conversation_id, created.run_id)
    return followups, created


def test_queued_followup_protects_old_run_then_expires_without_renewal(stack):
    followups, created = _followups(stack)
    cid, rid = created.conversation_id, created.run_id
    receipt = followups.submit(cid, rid, FollowupRequest(request_id="ask", text="补充解释"), _source(), OWNER)
    stack.clock.value = NOW
    newer = stack.store.submit_message(cid, "new", "新的诊断问题")
    workspace = _file(stack.root, f"tmp/workspaces/{receipt.followup_id}/inputs/history.md")
    assert not stack.history._expire_run(cid, rid, None, BOUNDARY)
    assert workspace.exists()
    # Expiry uses the original report time, not the recent cancellation time.
    followups.stop(cid, rid, receipt.followup_id, "stop", OWNER)
    assert stack.history._expire_run(cid, rid, None, BOUNDARY)
    assert not workspace.exists()
    assert stack.store.get_run(cid, newer.run_id)["run_id"] == newer.run_id
    with stack.repository.database_read() as db:
        assert not db.execute("SELECT 1 FROM agent_followup_tasks").fetchall()
        assert not db.execute("SELECT 1 FROM agent_followup_events").fetchall()


def test_whole_conversation_expiry_waits_for_snapshot_and_off_recovers_it(stack):
    followups, created = _followups(stack)
    assert followups.enqueue_snapshot(created.conversation_id, created.run_id, _source())
    stack.clock.value = NOW
    assert not stack.history._expire_conversation(created.conversation_id, BOUNDARY)
    followups.recover(enabled=False)
    assert stack.history._expire_conversation(created.conversation_id, BOUNDARY)
    assert stack.cleanup.run_once()
    with stack.repository.database_read() as db:
        assert not db.execute("SELECT 1 FROM agent_followup_snapshots").fetchall()


def test_delete_revokes_late_answer_and_captures_followup_workspace(stack):
    followups, created = _followups(stack)
    cid, rid = created.conversation_id, created.run_id
    receipt = followups.submit(cid, rid, FollowupRequest(request_id="ask", text="解释"), _source(), OWNER)
    task = followups.claim_task()
    workspace = _file(stack.root, f"tmp/workspaces/{task['workspace_id']}/inputs/history.md")
    stack.store.request_delete(cid, owner_key=OWNER)
    assert task["workspace_id"] in stack.store.cleanup_context(cid)["workspace_ids"]
    followups.finish_task(receipt.followup_id, answer="迟到回答")
    with stack.repository.database_read() as db:
        assert db.execute("SELECT status FROM agent_followup_tasks").fetchone()[0] == "CANCELLED"
    assert stack.cleanup.run_once()
    assert not workspace.exists()
    with stack.repository.database_read() as db:
        assert not db.execute("SELECT 1 FROM agent_followup_tasks").fetchall()


def test_pending_snapshot_and_running_answer_protect_exact_workspace(stack):
    followups, created = _followups(stack)
    cid, rid = created.conversation_id, created.run_id
    assert followups.enqueue_snapshot(cid, rid, _source())
    assert followups.workspace_in_use(JOB_ID)
    assert not followups.workspace_in_use(JOB_ID + ".logparse-preprocess")
    receipt = followups.submit(cid, rid, FollowupRequest(request_id="ask", text="解释"), _source(), OWNER)
    assert followups.workspace_in_use(receipt.followup_id)
    followups.recover(enabled=False)
    assert not followups.workspace_in_use(JOB_ID)
    assert not followups.workspace_in_use(receipt.followup_id)
