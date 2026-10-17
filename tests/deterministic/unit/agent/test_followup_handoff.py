"""One report question either answers or atomically starts an independent run."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import DiagnosisMode, JobStatus
from problem_locator.followup.models import FollowupSource
from problem_locator.followup.service import ReportFollowupService
from tests.deterministic.unit.agent.test_report_continuation import (
    ARTIFACT, CASE, OWNER, confirm, create, system, upload,
)
from tests.deterministic.unit.agent.test_report_followup import model_reply

JOB = "00000000-0000-0000-0000-000000000011"
DECISION = dict(action="CONTINUE_DIAGNOSIS", effective_problem_text="故障时间是 10:05，客户端和服务端均超时。",
    diagnosis_goal="核查服务端排队是否导致超时。", reason="现有报告没有核对服务端排队，需要结合当前材料继续定位。", log_mode="APPEND")


@pytest.fixture
def state(system):
    s = system
    backend = SimpleNamespace(answer="原报告只是待验证的假设。", decision=None, calls=[])
    def execute(**kwargs):
        backend.calls.append(kwargs)
        return SimpleNamespace(final_result=model_reply(kwargs, answer=backend.answer, decision=backend.decision))
    backend.execute = execute
    followups = ReportFollowupService(s.agent, "claude", enabled=True, backend=backend)
    source = FollowupSource(CASE, JOB, "故障时间是 10:00，仅客户端超时。", s.report.markdown,
        s.report.artifact.sha256, "GENERIC", False, s.store._now())
    followups._source = lambda cid, rid: source
    s.agent.followups = followups
    s.store.followup_store = followups.store
    s.followups, s.backend = followups, backend
    return s


def ask(s, *, request_id="question", attachments=()):
    return s.followups.submit(s.cid, s.rid, request_id, "时间是 10:05，服务端排队会不会导致超时？",
        owner_key=OWNER, attachment_ids=list(attachments))


def item(s):
    return s.followups.get(s.cid, s.rid, owner_key=OWNER).items[-1]


def counts(s):
    with s.repository.database_read() as db:
        return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("agent_conversation_runs", "agent_continuation_drafts", "agent_messages"))


def test_ordinary_answer_keeps_one_model_call_and_never_creates_a_draft_or_run(state):
    s = state
    before = counts(s)
    ask(s)
    assert s.followups.worker.run_once()
    assert item(s).status == "COMPLETED" and item(s).diagnosis_run_id is None
    assert counts(s) == before
    assert len(s.backend.calls) == 1 and s.engine.calls == []


@pytest.mark.parametrize("mode", ["APPEND", "REPLACE"])
def test_same_followup_call_hands_off_exact_inputs_and_link_without_intent_model(state, mode):
    s = state
    old = upload(s, "client.zip", b"client logs")
    new = upload(s, "server.zip", b"server logs")
    duplicate = upload(s, "server-copy.zip", b"server logs")
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body["report_inputs"]["attachment_ids"] = [old]
        s.store._save(db, body)
    receipt = ask(s, attachments=[new, duplicate])
    expected = [old, new] if mode == "APPEND" else [new]
    s.backend.decision = {**{key: value for key, value in DECISION.items() if key != "log_mode"},
        "attachment_ids": expected}
    assert s.followups.worker.run_once()
    answer = item(s)
    run = s.store.get_run(s.cid)
    assert answer.diagnosis_run_id == run["run_id"] != s.rid
    assert answer.status == "COMPLETED" and "已开始继续诊断" in answer.answer_markdown
    assert answer.attachment_ids == [new]
    assert counts(s) == (2, 0, 1)
    assert len(s.backend.calls) == 1 and s.engine.calls == []
    assert run["parent_run_id"] == run["root_run_id"] == s.rid
    assert run["report_version"] == 2
    assert run["continuation_context"]["parent_report_markdown"] == s.report.markdown
    message = s.store.get_conversation(s.cid).messages[0]
    assert message.attachment_ids == expected and message.text == DECISION["effective_problem_text"]
    assert message.request_id == "followup-diagnosis-" + receipt.followup_id
    commands = []
    s.agent._execute_command = lambda cid, label, command, **kwargs: commands.append(command)
    assert s.agent.run_once(s.cid)
    assert commands[0].raw_problem_text == DECISION["effective_problem_text"]
    assert commands[0].continuation_context.goal == DECISION["diagnosis_goal"]
    assert s.engine.calls == []
    assert s.store.get_run(s.cid, s.rid)["report_available"]


def test_handoff_retry_is_one_run_and_one_message_even_when_called_concurrently(state):
    s = state
    ask(s)
    task = s.followups.store.claim_task()
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: s.followups.continue_diagnosis(task, DECISION), range(2))) == [True, True]
    assert counts(s) == (2, 0, 1)
    assert s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (2, 0, 1)


@pytest.mark.parametrize("mode", ["APPEND", "REPLACE"])
def test_material_uploaded_before_clarification_remains_available_to_the_next_question(state, mode):
    s = state
    first = upload(s, "first.zip", b"first")
    latest = upload(s, "latest.zip", b"latest")
    ask(s, request_id="first-upload", attachments=[first])
    first_task = s.followups.store.claim_task()
    assert s.followups.store.finish_task(first_task["followup_id"], answer="请补充故障时间。")
    ask(s, request_id="latest-upload", attachments=[latest])
    latest_task = s.followups.store.claim_task()
    assert s.followups.store.finish_task(latest_task["followup_id"], answer="还需要服务端首次超时的时间。")
    receipt = ask(s, request_id="clarification")
    s.backend.decision = {**{key: value for key, value in DECISION.items() if key != "log_mode"},
        "attachment_ids": [first, latest] if mode == "APPEND" else [latest]}
    assert s.followups.worker.run_once()
    assert first in s.backend.calls[0]["prompt"] and latest in s.backend.calls[0]["prompt"]
    message = s.store.get_conversation(s.cid).messages[0]
    assert message.request_id == "followup-diagnosis-" + receipt.followup_id
    assert message.attachment_ids == ([first, latest] if mode == "APPEND" else [latest])
    assert message.text == DECISION["effective_problem_text"]
    assert s.engine.calls == [] and counts(s) == (2, 0, 1)


def test_replace_without_uploaded_materials_keeps_old_run_and_returns_clear_next_step(state):
    s = state
    ask(s)
    task = s.followups.store.claim_task()
    assert s.followups.continue_diagnosis(task, {**DECISION, "log_mode": "REPLACE"})
    assert counts(s) == (1, 0, 0)
    answer = item(s)
    assert answer.diagnosis_run_id is None and "先上传用于替换的日志" in answer.answer_markdown


@pytest.mark.parametrize("when", ["before_preparation", "during_preparation"])
def test_stop_before_handoff_commit_cannot_leave_an_orphan_run(state, monkeypatch, when):
    s = state
    receipt = ask(s)
    task = s.followups.store.claim_task()
    stop = lambda: s.followups.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=OWNER)
    if when == "before_preparation":
        stop()
    else:
        original = s.service._baseline
        def baseline(*args):
            result = original(*args)
            stop()
            return result
        monkeypatch.setattr(s.service, "_baseline", baseline)
    assert not s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (1, 0, 0)
    assert item(s).status == "CANCELLED" and item(s).diagnosis_run_id is None


def test_delete_during_handoff_preparation_cannot_create_a_run(state, monkeypatch):
    s = state
    ask(s)
    task = s.followups.store.claim_task()
    original = s.service._baseline
    def baseline(*args):
        result = original(*args)
        s.store.request_delete(s.cid, owner_key=OWNER)
        return result
    monkeypatch.setattr(s.service, "_baseline", baseline)
    assert not s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (1, 0, 0)


def test_handoff_answer_commit_failure_rolls_back_run_message_and_events(state, monkeypatch):
    s = state
    ask(s)
    task = s.followups.store.claim_task()
    original = s.followups.store._transition
    def fail(db, row, status, **kwargs):
        if kwargs.get("diagnosis_run_id"):
            raise RuntimeError("answer write failed")
        return original(db, row, status, **kwargs)
    with s.repository.database_read() as db:
        events_before = db.execute("SELECT count(*) FROM agent_events").fetchone()[0]
    monkeypatch.setattr(s.followups.store, "_transition", fail)
    with pytest.raises(RuntimeError, match="answer write failed"):
        s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (1, 0, 0)
    assert item(s).status == "RUNNING"
    with s.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM agent_events").fetchone()[0] == events_before
    monkeypatch.setattr(s.followups.store, "_transition", original)
    assert s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (2, 0, 1)


def test_old_report_handoff_returns_actionable_answer_without_opening_another_run(state):
    s = state
    ask(s)
    task = s.followups.store.claim_task()
    draft = create(s, effective_problem_text="后来确认的新问题")
    newer = confirm(s, draft)
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body.update(status="COMPLETED", report_available=True)
        s.store._save(db, body)
    before = counts(s)
    assert s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == before and s.store.get_run(s.cid)["run_id"] == newer.run_id
    answer = item(s)
    assert answer.status == "COMPLETED" and answer.diagnosis_run_id is None
    assert "尚未开始" in answer.answer_markdown and "最新报告" in answer.answer_markdown


def test_attachment_identity_scope_and_expired_replay_are_preserved(state):
    s = state
    first, other = upload(s, "first.zip", b"first"), upload(s, "other.zip", b"other")
    receipt = ask(s, attachments=[first])
    assert ask(s, attachments=[first]) == receipt
    with pytest.raises(AgentStoreError) as conflict:
        ask(s, attachments=[other])
    assert conflict.value.code == "AGENT_IDEMPOTENCY_CONFLICT"
    task = s.followups.store.claim_task()
    assert s.followups.task_attachments(task) == [{"attachment_id": first, "name": "first.zip",
        "sha256": hashlib.sha256(b"first").hexdigest(), "size": 5}]
    with s.repository.database_transaction() as db:
        raw = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=?", (first,)).fetchone()[0]
        data = json.loads(raw)
        data["status"] = "EXPIRED"
        db.execute("UPDATE agent_attachments SET body=? WHERE attachment_id=?", (json.dumps(data), first))
    assert ask(s, attachments=[first]) == receipt
    assert s.followups.continue_diagnosis(task, DECISION)
    assert counts(s) == (1, 0, 0)
    assert item(s).diagnosis_run_id is None and "尚未开始" in item(s).answer_markdown
    another = s.store.create_conversation("other-owner", owner_key="b" * 64)
    with s.repository.database_transaction() as db:
        raw = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=?", (other,)).fetchone()[0]
        data = json.loads(raw)
        data["conversation_id"] = another.conversation_id
        db.execute("UPDATE agent_attachments SET body=? WHERE attachment_id=?", (json.dumps(data), other))
    with pytest.raises(AgentStoreError, match="不属于"):
        ask(s, request_id="other-scope", attachments=[other])


def test_followup_source_uses_confirmed_effective_problem_without_an_extra_run_read(state, monkeypatch):
    s = state
    result = SimpleNamespace(source_job_id=JOB, skill_name="generic", report_markdown=s.report.markdown,
        report_sha256=s.report.artifact.sha256, report_artifact_id=ARTIFACT, occurred_at=s.store._now())
    job = SimpleNamespace(status=JobStatus.SUCCEEDED, diagnosis_mode=DiagnosisMode.GENERIC,
        generic_skill_name="generic", generic_problem_text="原始问题", job_id=JOB, attachment_refs=[])
    aggregate = SimpleNamespace(case=SimpleNamespace(generic_result_v2=result, case_id=CASE, raw_problem_text="原始问题"),
        jobs={JOB: job})
    captured = SimpleNamespace(view=SimpleNamespace(case_id=CASE), snapshot=SimpleNamespace(cases={CASE: aggregate}))
    monkeypatch.setattr(s.store, "read_conversation", lambda *args, **kwargs: captured)
    s.agent.application.read_conversation_delivery = lambda *args, **kwargs: (None, SimpleNamespace(
        format="markdown", source_job_id=JOB, artifact=s.report.artifact, markdown=s.report.markdown), None)
    original, reads = s.store.get_run, []
    def get_run(*args, **kwargs):
        reads.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(s.store, "get_run", get_run)
    source = ReportFollowupService._source(s.followups, s.cid, s.rid)
    assert source.problem_text == "故障时间是 10:00，仅客户端超时。"
    assert len(reads) == 1


def test_existing_snapshot_does_not_hide_confirmed_facts_from_the_followup_model(state):
    s = state
    ask(s)
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body["report_inputs"]["effective_problem_text"] += "\n已确认：服务端也超时，timeout_seconds=10。"
        s.store._save(db, body)
    assert s.followups.worker.run_once()
    assert "已确认：服务端也超时，timeout_seconds=10。" in s.backend.calls[0]["prompt"]
