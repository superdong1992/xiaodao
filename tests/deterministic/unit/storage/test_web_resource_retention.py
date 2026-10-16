"""Public text outlives the Web resource deadline and pressure reclamation."""
import hashlib
import json
from datetime import timedelta

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.store import AgentStore
from problem_locator.followup.models import FollowupRequest, FollowupSource
from problem_locator.followup.store import FollowupStore
from problem_locator.timestamps import format_timestamp, parse_timestamp
from tests.deterministic.unit.agent.test_store import _project_waiting_questions
from tests.deterministic.unit.storage.test_history_retention import (
    stack as legacy_stack, _close_run, _file, OLD, CASE_ID, JOB_ID,
)


@pytest.fixture
def stack(legacy_stack):
    legacy_stack.history.web_retention_days = 15
    return legacy_stack


def advance(stack, days):
    stack.clock.value = format_timestamp(parse_timestamp(OLD) + timedelta(days=days))


def upload(stack, cid, key="upload"):
    record = stack.store.reserve_attachment(cid, key, "日志.txt", "text/plain", 3,
        hashlib.sha256(b"log").hexdigest())
    path = _file(stack.root, f"resources/conversations/{record.attachment_id}/payload", b"log")
    stack.store.complete_attachment(record.attachment_id, str(path))
    return record.attachment_id, path


def test_text_history_and_metadata_survive_web_deadline_and_reopen(stack):
    created = stack.store.create_conversation("create", owner_key="owner")
    cid, rid = created.conversation_id, created.run_id
    aid, path = upload(stack, cid)
    stack.store.submit_message(cid, "question", "请分析这些日志", [aid])
    stack.store.append_event(cid, "assistant.question", {"questions": ["何时开始发生？"]}, dedupe_key="question")
    _close_run(stack.store, cid, rid)
    before = stack.store.read_conversation(cid, history=True).history
    advance(stack, 14)
    stack.history.run_once()
    assert path.exists()
    advance(stack, 15)
    assert stack.history.run_once()
    assert not path.exists()
    assert stack.store.get_attachment(aid).status == "EXPIRED"
    assert stack.store.get_run(cid, rid)["resources_expired"] is True
    assert stack.store.get_run(cid, rid)["resource_cleanup_reason"] == "RETENTION_EXPIRED"
    advance(stack, 365)
    stack.history.run_once()
    reopened = AgentStore(stack.repository, clock=stack.clock)
    assert reopened.read_conversation(cid, history=True).history == before
    assert reopened.list_conversations("owner").items[0].current_run.resources_expired is True


def test_shared_attachment_protects_new_waiting_run_during_pressure(stack):
    created = stack.store.create_conversation("create")
    cid, old_rid = created.conversation_id, created.run_id
    aid, path = upload(stack, cid)
    stack.store.submit_message(cid, "first", "第一轮", [aid])
    _close_run(stack.store, cid, old_rid)
    advance(stack, 1)
    newer = stack.store.submit_message(cid, "second", "第二轮", [aid])
    assert stack.history.reclaim_web_resources()
    assert stack.store.get_run(cid, old_rid)["resources_expired"]
    assert not stack.store.get_run(cid, newer.run_id).get("resources_expired", False)
    assert path.exists() and stack.store.get_attachment(aid).status == "READY"
    assert not stack.history.reclaim_web_resources()
    _close_run(stack.store, cid, newer.run_id)
    assert stack.history.reclaim_web_resources()
    assert not path.exists()
    assert stack.store.get_attachment(aid).status == "EXPIRED"
    assert stack.store.get_run(cid, newer.run_id)["resource_cleanup_reason"] == "DISK_PRESSURE"
    assert len(stack.store.read_conversation(cid, history=True).history) == 4


def test_expired_uploads_release_count_and_bytes_but_never_revive(stack):
    cid = stack.store.create_conversation("create").conversation_id
    ids = [upload(stack, cid, str(index))[0] for index in range(20)]
    advance(stack, 15)
    stack.history.run_once()
    assert all(stack.store.get_attachment(aid).status == "EXPIRED" for aid in ids)
    upload(stack, cid, "new")
    with pytest.raises(AgentStoreError) as expired:
        upload(stack, cid, "0")
    assert expired.value.code == "AGENT_ATTACHMENT_EXPIRED"
    with pytest.raises(AgentStoreError) as expired:
        stack.store.set_attachment_status(ids[0], "UPLOADING")
    assert expired.value.status_code == 410


def test_failed_physical_cleanup_keeps_quota_and_retries_exact_paths(stack, monkeypatch):
    created = stack.store.create_conversation("create")
    cid = created.conversation_id
    ids = [upload(stack, cid, str(index))[0] for index in range(20)]
    stack.store.submit_message(cid, "question", "回收日志", ids)
    _close_run(stack.store, cid, created.run_id)
    original = stack.quarantine.delete
    monkeypatch.setattr(stack.quarantine, "delete", lambda *_: (_ for _ in ()).throw(OSError("blocked")))
    assert not stack.history.reclaim_web_resources()
    assert stack.store.get_run(cid)["resources_expired"]
    with pytest.raises(AgentStoreError) as quota:
        upload(stack, cid, "new")
    assert quota.value.code == "AGENT_ATTACHMENT_LIMIT"
    monkeypatch.setattr(stack.quarantine, "delete", original)
    assert stack.history.run_once()
    upload(stack, cid, "new")
    with stack.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM history_cleanup_jobs").fetchone()[0] == 0


def test_archive_failure_prevents_resource_revocation(stack):
    created = stack.store.create_conversation("create")
    aid, path = upload(stack, created.conversation_id)
    stack.store.submit_message(created.conversation_id, "question", "问题", [aid])
    _close_run(stack.store, created.conversation_id, created.run_id)
    stack.history.archive_run = lambda *_: False
    assert not stack.history.reclaim_web_resources()
    assert path.exists()
    assert not stack.store.get_run(created.conversation_id).get("resources_expired", False)


def test_business_activity_does_not_follow_rename_and_idle_stop_opens_new_run(stack):
    created = stack.store.create_conversation("create")
    cid, rid = created.conversation_id, created.run_id
    stack.store.update_intake(cid, {}, ["请提供日志"])
    advance(stack, 14)
    stack.store.rename_conversation(cid, "修改名称")
    assert stack.store.get_run(cid)["last_business_activity_at"] == OLD
    assert not stack.store.request_idle_stop(cid, rid, format_timestamp(parse_timestamp(OLD) - timedelta(seconds=1)))
    assert stack.store.request_idle_stop(cid, rid, OLD)
    assert stack.store.get_run(cid)["stop_reason"] == "IDLE_TIMEOUT"
    stack.store.finish_stop(cid, rid)
    assert "长时间未收到补充信息" in stack.store.get_run(cid)["failure"]["message"]
    new = stack.store.submit_message(cid, "new", "现在补充问题")
    assert new.run_id != rid


def test_completed_followup_answers_survive_resource_reclamation(stack):
    created = stack.store.create_conversation("create", owner_key="owner")
    cid, rid = created.conversation_id, created.run_id
    _close_run(stack.store, cid, rid)
    followups = FollowupStore(stack.repository, stack.clock, retention_days=15)
    stack.store.followup_store = followups
    source = FollowupSource(CASE_ID, JOB_ID, "原问题", "# 报告", "a" * 64, "GENERIC", False, OLD)
    receipt = followups.submit(cid, rid, FollowupRequest(request_id="ask", text="解释结论"), source, "owner")
    assert not stack.history.reclaim_web_resources()
    followups.claim_task()
    assert followups.finish_task(receipt.followup_id, answer="这是已经回复的正文")
    advance(stack, 16)
    assert stack.history.run_once()
    page = followups.page(cid, rid, "owner")
    assert page["items"][0].text == "解释结论"
    assert page["items"][0].answer_markdown == "这是已经回复的正文"
    assert page["snapshot"]["reserved_bytes"] == 0
    assert followups.request(cid, rid, "ask", "解释结论", "owner") == receipt
    with pytest.raises(AgentStoreError) as expired:
        followups.submit(cid, rid, FollowupRequest(request_id="new", text="再次解释"), source, "owner")
    assert expired.value.code == "AGENT_FOLLOWUP_EXPIRED"


def test_permanent_attachment_metadata_is_loaded_only_for_current_history_page(stack):
    cid = stack.store.create_conversation("create").conversation_id
    expired_ids = set()
    for index in range(21):
        aid, _ = upload(stack, cid, str(index))
        receipt = stack.store.submit_message(cid, str(index), f"问题 {index}", [aid])
        _close_run(stack.store, cid, receipt.run_id)
        assert stack.history.reclaim_web_resources()
        expired_ids.add(aid)
    available_id, _ = upload(stack, cid, "available")
    page = stack.store.read_conversation(cid, history=True, history_limit=2)
    seen = set()
    while True:
        referenced = {aid for entry in page.history if entry.message for aid in entry.message.attachment_ids}
        assert {item.attachment_id for item in page.attachments} == referenced | {available_id}
        assert len(page.attachments) <= 2
        assert all(item.status == ("READY" if item.attachment_id == available_id else "EXPIRED") for item in page.attachments)
        seen.update(referenced)
        if page.history_next_cursor is None:
            break
        page = stack.store.read_conversation(cid, history=True, history_limit=2, history_before=page.history_next_cursor)
    assert seen == expired_ids


@pytest.mark.parametrize("reliable_activity", [True, False])
def test_missing_business_activity_is_backfilled_once_from_evidence_or_upgrade_time(stack, reliable_activity):
    cid = stack.store.create_conversation("create").conversation_id
    if reliable_activity:
        advance(stack, 1)
        stack.store.submit_message(cid, "message", "用户问题")
        advance(stack, 2)
        stack.store.append_event(cid, "assistant.question", {"questions": ["请提供日志"]}, dedupe_key="question")
        advance(stack, 3)
        upload(stack, cid)
    advance(stack, 20)
    stack.store.rename_conversation(cid, "非业务改名")
    with stack.repository.database_transaction() as db:
        for table in ("agent_conversation_runs", "agent_conversations"):
            body = json.loads(db.execute(f"SELECT body FROM {table} WHERE conversation_id=?", (cid,)).fetchone()[0])
            body.pop("last_business_activity_at", None)
            db.execute(f"UPDATE {table} SET body=? WHERE conversation_id=?", (json.dumps(body), cid))
    advance(stack, 30)
    reopened = AgentStore(stack.repository, clock=stack.clock)
    expected = format_timestamp(parse_timestamp(OLD) + timedelta(days=3 if reliable_activity else 30))
    assert reopened.get_run(cid)["last_business_activity_at"] == expected
    advance(stack, 31)
    assert AgentStore(stack.repository, clock=stack.clock).get_run(cid)["last_business_activity_at"] == expected


def test_retry_releases_pending_paths_before_expiring_another_run(stack, monkeypatch):
    created = stack.store.create_conversation("create")
    cid = created.conversation_id
    aid, path = upload(stack, cid)
    stack.store.submit_message(cid, "first", "第一轮", [aid])
    _close_run(stack.store, cid, created.run_id)
    original = stack.quarantine.delete
    monkeypatch.setattr(stack.quarantine, "delete", lambda *_: (_ for _ in ()).throw(OSError("blocked")))
    assert not stack.history.reclaim_web_resources()
    advance(stack, 1)
    newer = stack.store.submit_message(cid, "second", "第二轮")
    _close_run(stack.store, cid, newer.run_id)
    monkeypatch.setattr(stack.quarantine, "delete", original)
    assert stack.history.reclaim_web_resources()
    assert not path.exists()
    assert not stack.store.get_run(cid, newer.run_id).get("resources_expired", False)


@pytest.mark.parametrize("restart_status", ["PENDING", "COMMITTED"])
def test_own_restart_does_not_keep_payload_after_resource_reclamation(stack, restart_status):
    created = stack.store.create_conversation("create")
    cid, rid = created.conversation_id, created.run_id
    aid, path = upload(stack, cid)
    if restart_status == "COMMITTED":
        stack.store.submit_message(cid, "first", "问题", [aid])
    _close_run(stack.store, cid, rid)
    with stack.repository.database_transaction() as db:
        db.execute("INSERT INTO agent_generic_restarts(conversation_id,request_id,run_id,fingerprint,command_key,status,payload) "
            "VALUES (?,?,?,?,?,?,?)", (cid, "restart", rid, "fingerprint", "command", restart_status,
                json.dumps({"message": {"attachment_ids": [aid]}})))
    assert stack.history.reclaim_web_resources()
    assert not path.exists()


def test_idle_waiting_timeout_defers_while_conversation_is_in_use(stack):
    created = stack.store.create_conversation("create")
    stack.store.update_intake(created.conversation_id, {}, ["请提供日志"])
    calls = []
    stack.history.expire_waiting_run = lambda *args: calls.append(args) or True
    advance(stack, 15)
    with stack.usage.acquire(created.conversation_id):
        stack.history.run_once()
    assert not calls
    stack.history.run_once()
    assert calls == [(created.conversation_id, created.run_id, format_timestamp(parse_timestamp(OLD)))]


def test_followup_deadline_uses_run_completion_after_report_publication(stack):
    created = stack.store.create_conversation("create")
    stack.store.bind_case(created.conversation_id, CASE_ID)
    source = FollowupSource(CASE_ID, JOB_ID, "问题", "# 报告", "a" * 64, "GENERIC", False, OLD)
    followups = FollowupStore(stack.repository, stack.clock, retention_days=15)
    advance(stack, 20)
    assert not followups.expired(source)
    _close_run(stack.store, created.conversation_id, created.run_id)
    advance(stack, 34)
    assert not followups.expired(source)
    advance(stack, 35)
    assert followups.expired(source)


@pytest.mark.parametrize("waiting_entry", ["update_intake", "finish_intake", "case_projection"])
def test_entering_waiting_with_unchanged_questions_refreshes_activity_only_once(stack, waiting_entry):
    cid = stack.store.create_conversation("create").conversation_id
    questions = ["请提供日志"]
    stack.store.update_intake(cid, {}, questions)
    advance(stack, 1)
    stack.store.update_intake(cid, {}, questions, status="RUNNING")
    assert stack.store.get_run(cid)["last_business_activity_at"] == OLD
    def enter_waiting():
        if waiting_entry == "update_intake":
            stack.store.update_intake(cid, {}, questions)
        elif waiting_entry == "finish_intake":
            stack.store.finish_intake(cid, [], questions)
        else:
            _project_waiting_questions(stack.store, cid, questions)
    advance(stack, 5)
    enter_waiting()
    expected = stack.clock.now()
    assert stack.store.get_run(cid)["last_business_activity_at"] == expected
    advance(stack, 6)
    enter_waiting()
    stack.store.rename_conversation(cid, "改名不续期")
    stack.store.read_conversation(cid, history=True)
    assert stack.store.get_run(cid)["last_business_activity_at"] == expected
    assert len([event for event in stack.store.list_events(cid) if event.type == "assistant.question"]) == 1
