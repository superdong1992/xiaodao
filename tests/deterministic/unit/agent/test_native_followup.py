"""Persisted native sessions remain readable without the retired worker."""
from __future__ import annotations
import uuid
import json
import pytest
from problem_locator.agent.models import AgentStoreError
from problem_locator.followup.service import ReportFollowupService
from tests.deterministic.unit.agent.test_cleanup import stack, _file, _finish
from tests.deterministic.unit.agent.test_report_archive import setup, configure, publish, expire
from tests.deterministic.unit.application.test_reports import generic_report
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, Backend, make_logs, original_rows, submit, system, view,
)


def begin_historical_turn(s, request="first", text="解释旧报告。"):
    receipt = submit(s, request, text)
    task = s.service.store.claim_task()
    assert task["followup_id"] == receipt.followup_id
    previous = s.service.store.sessions.begin(task)
    session_id = previous["session_id"] or str(uuid.uuid4())
    s.service.store.sessions.capture_session(task, session_id)
    return task, previous, session_id


def complete_historical_turn(s, request="first", text="解释旧报告。", answer="旧版模型保存的回答。"):
    task, previous, session_id = begin_historical_turn(s, request, text)
    assert s.service.store.finish_task(task["followup_id"], answer=answer)
    return task, previous, session_id


def test_historical_checkpoint_and_incremental_history_preserve_originals(system):
    s = system
    before = original_rows(s)
    first, _, session_id = complete_historical_turn(s, text="trace-709 是此次标记。")
    assert submit(s, "first", "trace-709 是此次标记。").followup_id == first["followup_id"]
    saved = s.service.store.sessions.get(s.cid)
    second, previous, resumed_id = begin_historical_turn(s, "second", "结合刚才的标记解释。")
    assert resumed_id == session_id and previous["last_followup_id"] == first["followup_id"]
    assert s.service.store.sessions.history(second, saved["last_order"]) == []
    assert s.service.store.sessions.history(second)[0]["text"] == "trace-709 是此次标记。"
    assert s.service.store.finish_task(second["followup_id"], answer="保留完整历史回答。")
    assert original_rows(s) == before and s.backend.calls == []
    assert all(item.status == "COMPLETED" for item in view(s).items)
    assert [e.sequence for e in s.service.list_events(s.cid, s.rid, owner_key=OWNER).events] == list(range(1, 7))


def test_restart_and_feature_switch_preserve_historical_session(system):
    s = system
    complete_historical_turn(s)
    saved = s.service.store.sessions.get(s.cid)
    disabled = ReportFollowupService(s.agent, "codeagent", enabled=False, backend=s.backend)
    disabled._source = lambda cid, rid: s.source
    with pytest.raises(AgentStoreError, match="报告追问尚未开启"):
        disabled.submit(s.cid, s.rid, "disabled", "关闭期间的补充。", owner_key=OWNER)
    assert not disabled.worker.run_once()
    assert len(disabled.get(s.cid, s.rid, owner_key=OWNER).items) == 1
    resumed = ReportFollowupService(s.agent, "codeagent", enabled=True, backend=s.backend)
    resumed._source = lambda cid, rid: s.source
    assert resumed.store.sessions.get(s.cid) == saved
    assert resumed.get(s.cid, s.rid, owner_key=OWNER).items[0].answer_markdown == "旧版模型保存的回答。"
    assert s.backend.calls == []


def test_server_written_historical_answer_remains_in_next_turn_history(system):
    s = system
    complete_historical_turn(s, answer="这次尚未开始：原日志已清理，请补充日志。")
    task, _, _ = begin_historical_turn(s, "next")
    assert s.service.store.sessions.history(task)[0]["answer_markdown"] == "这次尚未开始：原日志已清理，请补充日志。"


def test_historical_snapshot_is_not_recreated_by_observation(system):
    s = system
    original, _ = make_logs(s)
    assert s.service.observe_report(s.cid, s.rid)
    assert not s.service.snapshot_worker.run_once()
    assert s.service.store.enqueue_snapshot(s.cid, s.rid, s.source)
    assert s.service.snapshot_worker.run_once()
    before = original_rows(s)
    complete_historical_turn(s)
    assert view(s).snapshot_status == "READY"
    assert original_rows(s) == before
    assert (original / "inputs/logs/source.log").read_bytes().endswith(b"trace_key=one\n")


def test_new_report_checkpoint_retains_same_historical_session(system):
    from tests.deterministic.unit.agent.test_native_followup_reports import new_report
    s = system
    _, _, session_id = complete_historical_turn(s)
    old_rid = s.rid
    new_report(s, "新报告")
    task, previous, resumed_id = complete_historical_turn(s, "new-report")
    assert resumed_id == session_id and previous["report_run_id"] == old_rid
    saved = s.service.store.sessions.get(s.cid)
    assert saved["report_run_id"] == s.rid and saved["report_sha256"] == s.source.report_sha256
    assert saved["last_followup_id"] == task["followup_id"]


def test_cancelled_historical_turn_rejects_late_result_but_retains_session(system):
    s = system
    task, _, session_id = begin_historical_turn(s)
    s.service.stop(s.cid, s.rid, task["followup_id"], "stop", owner_key=OWNER)
    assert s.service.store.finish_task(task["followup_id"], answer="迟到的回答")
    with s.repository.database_read() as db:
        raw = db.execute("SELECT body FROM agent_followup_tasks WHERE followup_id=?", (task["followup_id"],)).fetchone()[0]
    assert json.loads(raw)["status"] == "CANCELLED" and json.loads(raw)["answer_markdown"] is None
    with pytest.raises(AgentStoreError):
        s.service.store.sessions.capture_session(task, session_id)
    _, previous, resumed_id = complete_historical_turn(s, "after-stop")
    assert resumed_id == session_id and previous["previous_status"] == "CANCELLED"
    assert view(s).items[0].answer_markdown is None


def test_historical_store_preserves_more_than_one_hundred_questions(system):
    for index in range(101):
        complete_historical_turn(system, str(index), f"第 {index} 条补充")
    task, _, _ = begin_historical_turn(system, "latest")
    history = system.service.store.sessions.history(task)
    assert len(history) == 101 and history[-1]["text"] == "第 100 条补充"
    assert system.backend.calls == []


def test_retention_keeps_native_checkpoint_and_historical_answer(system):
    s = system
    complete_historical_turn(s)
    saved = s.service.store.sessions.get(s.cid)
    s.clock.value = "2026-12-01T00:00:00.000Z"
    assert view(s).can_ask and s.service.observe_report(s.cid, s.rid)
    assert not s.service.snapshot_worker.run_once()
    assert s.service.store.sessions.get(s.cid) == saved
    assert view(s).items[0].answer_markdown == "旧版模型保存的回答。"


def test_deleted_conversation_cannot_accept_late_session_or_answer(system):
    s = system
    task, _, session_id = begin_historical_turn(s)
    s.agent.store.request_delete(s.cid, owner_key=OWNER)
    with pytest.raises(AgentStoreError):
        s.service.store.sessions.capture_session(task, session_id)
    assert s.service.store.finish_task(task["followup_id"], answer="迟到的回答")
    with s.repository.database_read() as db:
        raw = db.execute("SELECT body FROM agent_followup_tasks WHERE followup_id=?", (task["followup_id"],)).fetchone()[0]
    assert json.loads(raw)["status"] == "CANCELLED" and json.loads(raw)["answer_markdown"] is None
    with pytest.raises(AgentStoreError):
        view(s)


def test_delete_removes_only_this_conversations_native_files(stack):
    repository, store, cid, _, _, _, cleanup = stack
    own = _file(repository.layout.data_root, f"resources/followup-sessions/{cid}/config/transcript.jsonl")
    other_id = str(uuid.uuid4())
    other = _file(repository.layout.data_root, f"resources/followup-sessions/{other_id}/config/transcript.jsonl")
    (own.parent / "latest").symlink_to(other)
    _finish(repository)
    store.request_delete(cid)
    assert cleanup.run_once()
    assert not own.exists()
    assert other.exists()


def test_expired_report_is_a_verified_generic_handoff_source(setup):
    configure(setup)
    aggregate, resources, _ = generic_report(2)
    setup.resources.content = resources.content
    setup.store.submit_message(setup.conversation_id, "initial", "请检查超时原因。")
    publish(setup, aggregate)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    # The minimal archive fixture omits Jobs; supply their persisted projection.
    with setup.repository.database_transaction() as db:
        body = setup.store._load(db, setup.conversation_id)
        body["report_inputs"] = {"effective_problem_text": "请检查超时原因。", "attachment_ids": [],
            "runtime": {"generic_skill": "generic-problem-locator-smoke"}}
        setup.store._save(db, body)
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    expire(setup)
    followups = ReportFollowupService(setup.service, "codeagent", enabled=True,
        backend=Backend())
    with setup.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversations SET owner_key=? WHERE conversation_id=?", (OWNER, setup.conversation_id))
    setup.service.followups, setup.store.followup_store = followups, followups.store
    owner = OWNER
    assert followups.get(setup.conversation_id, rid, owner_key=owner).can_ask
    followups.submit(setup.conversation_id, rid, "later", "报告发布很久了，请解释一下。", owner_key=owner)
    assert followups.worker.run_once()
    item = followups.get(setup.conversation_id, rid, owner_key=owner).items[-1]
    assert item.status == "COMPLETED" and item.diagnosis_run_id != rid
    assert setup.store.get_run(setup.conversation_id)["force_generic"] is True
    assert followups.worker.backend.calls == []
    while followups.snapshot_worker.run_once():
        pass
    assert followups.store.snapshot(rid)["status"] == "UNAVAILABLE"
    assert followups.store.claim_snapshot() is None
    assert setup.service.report_archive.read(setup.conversation_id, rid).content == resources.content


def test_corrupted_archive_cannot_initialize_session(setup):
    configure(setup)
    aggregate, resources, _ = generic_report(2)
    setup.resources.content = resources.content
    publish(setup, aggregate)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    expire(setup)
    with setup.repository.database_transaction() as db:
        db.execute("UPDATE agent_report_archives SET content=? WHERE run_id=?", (b"changed", rid))
    backend = Backend()
    followups = ReportFollowupService(setup.service, "codeagent", enabled=True,
        backend=backend)
    with pytest.raises(AgentStoreError, match="历史报告校验失败"):
        followups._source(setup.conversation_id, rid)
    assert backend.calls == []
