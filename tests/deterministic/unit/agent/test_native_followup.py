"""Native sessions add context continuity without changing report or Web contracts."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.followup.service import ReportFollowupService
from tests.deterministic.unit.agent.test_cleanup import stack, _file, _finish
from tests.deterministic.unit.agent.test_report_archive import setup, configure, publish, expire
from tests.deterministic.unit.application.test_reports import generic_report
from tests.deterministic.unit.agent.test_report_followup import (
    OWNER, Backend, make_logs, model_reply, original_rows, submit, system, view,
)


class SessionBackend(Backend):
    def execute(self, **kwargs):
        self.calls.append(kwargs)
        session = kwargs.get("native_session")
        if session is not None:
            transcript = session.config_dir / "test-session.jsonl"
            saved = json.loads(transcript.read_text()) if transcript.exists() else {
                "session_id": str(uuid.uuid4()), "messages": []}
            assert session.resume_id == (saved["session_id"] if saved["messages"] else None)
            session.on_session_id(saved["session_id"])
            saved["messages"].append(kwargs["prompt"])
            transcript.write_text(json.dumps(saved), encoding="utf-8")
        if self.action is not None:
            self.action(kwargs)
        return SimpleNamespace(final_result=model_reply(kwargs, answer=self.answer, decision=self.decision))


@pytest.fixture
def native(system):
    system.backend = SessionBackend()
    system.service.worker.backend = system.backend
    return system


def context(call):
    return json.loads((call["workspace_root"] / "inputs/context.json").read_text("utf-8"))


def test_two_questions_resume_exact_session_with_incremental_input_and_unchanged_originals(native):
    before = original_rows(native)
    first = submit(native, text="日志中的唯一标记是 trace-709，请记住。")
    assert submit(native, text="日志中的唯一标记是 trace-709，请记住。") == first
    assert native.service.worker.run_once()
    saved = native.service.store.sessions.get(native.cid)
    assert context(native.backend.calls[0])["original_report"] == native.source.report_markdown
    second = submit(native, "second", "结合刚才的标记，解释为什么排队。")
    assert native.service.worker.run_once()
    call = native.backend.calls[1]
    assert call["native_session"].resume_id == saved["session_id"]
    assert call["native_session"].config_dir == native.backend.calls[0]["native_session"].config_dir
    assert call["workspace_root"] != native.backend.calls[0]["workspace_root"]
    assert "trace-709" not in call["prompt"]
    assert "original_report" not in context(call)
    assert context(call)["prior_turns"] == []
    assert context(call)["response_contract"]["request_key"] == second.followup_id
    assert original_rows(native) == before
    assert native.agent.store.list_events(native.cid) == []
    assert [e.sequence for e in native.service.list_events(native.cid, native.rid, owner_key=OWNER).events] == list(range(1, 7))
    assert all(item.status == "COMPLETED" for item in view(native).items)


def test_restart_and_switching_feature_off_then_on_preserve_context(native):
    submit(native)
    assert native.service.worker.run_once()
    saved = native.service.store.sessions.get(native.cid)
    disabled = ReportFollowupService(native.agent, "codeagent", enabled=False, backend=native.backend)
    disabled._source = lambda cid, rid: native.source
    with pytest.raises(AgentStoreError, match="报告追问尚未开启"):
        disabled.submit(native.cid, native.rid, "disabled", "总开关关闭期间的补充", owner_key=OWNER)
    assert not disabled.worker.run_once()
    assert len(disabled.get(native.cid, native.rid, owner_key=OWNER).items) == 1
    assert len(native.backend.calls) == 1
    resumed = ReportFollowupService(native.agent, "codeagent", enabled=True,
        backend=native.backend)
    resumed._source = lambda cid, rid: native.source
    resumed.submit(native.cid, native.rid, "resumed", "继续刚才的问题", owner_key=OWNER)
    assert resumed.worker.run_once()
    last = native.backend.calls[-1]
    assert last["native_session"].resume_id == saved["session_id"]
    assert context(last)["prior_turns"] == []
    assert all(item.status == "COMPLETED" for item in resumed.get(native.cid, native.rid, owner_key=OWNER).items)


def test_rejected_handoff_is_supplied_to_the_next_native_message(native, monkeypatch):
    def reject(*args):
        raise AgentStoreError("AGENT_RESOURCE_EXPIRED", "原日志已清理，请补充日志。", 410)

    def unexpected_failure(*args, **kwargs):
        pytest.fail(str(kwargs))

    monkeypatch.setattr("problem_locator.followup.worker.log_event", unexpected_failure)
    monkeypatch.setattr(native.agent.continuations, "handoff_followup", reject)
    native.backend.decision = {"effective_problem_text": "需要核查请求超时的原因。",
        "diagnosis_goal": "核查排队是否导致超时。", "reason": "需要继续定位。", "attachment_ids": []}
    first = submit(native)
    assert native.service.worker.run_once()
    answer = view(native).items[-1].answer_markdown
    assert "这次尚未开始" in answer and "原日志已清理" in answer
    native.backend.decision = None
    submit(native, "after-rejection", "那先解释报告。")
    assert native.service.worker.run_once()
    history = context(native.backend.calls[-1])["prior_turns"]
    assert history[0]["followup_id"] == first.followup_id
    assert history[0]["answer"] == answer


def test_native_session_keeps_current_log_inputs_after_resume(native):
    original, _ = make_logs(native)
    assert native.service.observe_report(native.cid, native.rid)
    assert native.service.snapshot_worker.run_once()
    before = original_rows(native)
    for request in ("logs-first", "logs-next"):
        submit(native, request, "核对 trace_key 附近的证据。")
        assert native.service.worker.run_once()
        call = native.backend.calls[-1]
        assert call["file_access"] == "read-search"
        assert context(call)["context_mode"] == "REPORT_AND_LOGS"
        manifest = json.loads((call["workspace_root"] / "inputs/logs.json").read_bytes())
        assert b"QUEUE_OVERFLOW" in (call["workspace_root"] / manifest["logs"][0]["log_path"]).read_bytes()
    assert original_rows(native) == before
    assert (original / "inputs/logs/source.log").read_bytes().endswith(b"trace_key=one\n")


def test_new_report_uses_same_session_and_sends_report_once(native):
    submit(native)
    assert native.service.worker.run_once()
    previous = native.service.store.sessions.get(native.cid)
    with native.repository.database_transaction() as db:
        row = json.loads(db.execute("SELECT body FROM agent_conversation_runs WHERE run_id=?", (native.rid,)).fetchone()[0])
        rid = str(uuid.uuid4())
        row.update(run_id=rid, ordinal=2)
        db.execute("INSERT INTO agent_conversation_runs(run_id,conversation_id,ordinal,status,epoch,body) VALUES (?,?,?,?,?,?)",
            (rid, native.cid, 2, row["status"], "test", json.dumps(row)))
    native.service.submit(native.cid, rid, "new-report", "结合上一版，解释新报告。", owner_key=OWNER)
    assert native.service.worker.run_once()
    call = native.backend.calls[-1]
    assert call["native_session"].resume_id == previous["session_id"]
    assert context(call)["original_report"] == native.source.report_markdown
    assert context(call)["conversation_context"]["report_run_id"] == rid
    native.service.submit(native.cid, rid, "new-report-next", "再解释一下。", owner_key=OWNER)
    assert native.service.worker.run_once()
    assert "original_report" not in context(native.backend.calls[-1])


def test_cancelled_turn_cannot_publish_and_next_message_keeps_session(native):
    first = submit(native)
    native.backend.action = lambda _: native.service.stop(native.cid, native.rid, first.followup_id, "stop", owner_key=OWNER)
    assert not native.service.worker.run_once()
    assert view(native).items[0].status == "CANCELLED"
    saved = native.service.store.sessions.get(native.cid)
    native.backend.action = None
    submit(native, "after-stop", "请换一种解释。")
    assert native.service.worker.run_once()
    assert native.backend.calls[-1]["native_session"].resume_id == saved["session_id"]
    assert context(native.backend.calls[-1])["conversation_context"]["previous_status"] == "CANCELLED"
    assert view(native).items[0].answer_markdown is None


def test_native_does_not_end_conversation_at_one_hundred_questions(native):
    for i in range(101):
        submit(native, str(i), f"第 {i} 条补充")
        assert native.service.worker.run_once()
    assert view(native).can_ask
    assert len(native.backend.calls) == 101
    assert context(native.backend.calls[-1])["prior_turns"] == []


def test_native_can_continue_after_retention_without_snapshot_activity(native):
    submit(native)
    assert native.service.worker.run_once()
    native.clock.value = "2026-12-01T00:00:00.000Z"
    assert view(native).can_ask
    assert not native.service.observe_report(native.cid, native.rid)
    submit(native, "later", "隔了一段时间，再解释一下。")
    assert native.service.worker.run_once()
    assert view(native).items[-1].context_mode == "REPORT_ONLY"
    assert "未重新核对原始日志" in view(native).items[-1].answer_markdown


def test_deleted_conversation_cannot_accept_late_session_or_answer(native):
    submit(native)
    native.backend.action = lambda _: native.agent.store.request_delete(native.cid, owner_key=OWNER)
    assert not native.service.worker.run_once()
    with pytest.raises(AgentStoreError):
        view(native)
    with native.repository.database_read() as db:
        assert db.execute("SELECT status FROM agent_followup_tasks WHERE conversation_id=?", (native.cid,)).fetchone()[0] != "COMPLETED"


def test_delete_removes_only_this_conversations_native_files(stack):
    repository, store, cid, _, _, _, cleanup = stack
    own = _file(repository.layout.data_root, f"resources/followup-sessions/{cid}/config/transcript.jsonl")
    other_id = str(uuid.uuid4())
    other = _file(repository.layout.data_root, f"resources/followup-sessions/{other_id}/config/transcript.jsonl")
    try:
        (own.parent / "latest").symlink_to(other)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    _finish(repository)
    store.request_delete(cid)
    assert cleanup.run_once()
    assert not own.exists()
    assert other.exists()


def test_expired_report_initializes_native_session_from_verified_archive(setup):
    configure(setup)
    aggregate, resources, _ = generic_report(2)
    setup.resources.content = resources.content
    setup.store.submit_message(setup.conversation_id, "initial", "请检查超时原因。")
    publish(setup, aggregate)
    rid = setup.store.get_run(setup.conversation_id)["run_id"]
    # The minimal archive fixture omits Jobs; supply their persisted projection.
    with setup.repository.database_transaction() as db:
        body = setup.store._load(db, setup.conversation_id)
        body["report_inputs"] = {"effective_problem_text": "请检查超时原因。",
            "runtime": {"generic_skill": "generic-problem-locator-smoke"}}
        setup.store._save(db, body)
    assert setup.service.report_archive.archive_run(setup.conversation_id, rid)
    expire(setup)
    followups = ReportFollowupService(setup.service, "codeagent", enabled=True,
        backend=SessionBackend())
    with setup.repository.database_transaction() as db:
        db.execute("UPDATE agent_conversations SET owner_key=? WHERE conversation_id=?", (OWNER, setup.conversation_id))
    owner = OWNER
    assert followups.get(setup.conversation_id, rid, owner_key=owner).can_ask
    followups.submit(setup.conversation_id, rid, "later", "报告发布很久了，请解释一下。", owner_key=owner)
    assert followups.worker.run_once()
    item = followups.get(setup.conversation_id, rid, owner_key=owner).items[-1]
    assert item.status == "COMPLETED" and item.context_mode == "REPORT_ONLY"
    assert not followups.snapshot_worker.run_once()
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
    backend = SessionBackend()
    followups = ReportFollowupService(setup.service, "codeagent", enabled=True,
        backend=backend)
    with pytest.raises(AgentStoreError, match="历史报告校验失败"):
        followups._source(setup.conversation_id, rid)
    assert backend.calls == []
