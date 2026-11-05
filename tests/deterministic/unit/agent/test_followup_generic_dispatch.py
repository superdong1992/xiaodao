"""Every accepted report question starts a fresh Generic diagnosis."""
import json

import pytest

from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.agent.test_followup_handoff import state, item, counts
from tests.deterministic.unit.agent.test_report_continuation import OWNER, system, upload


@pytest.mark.parametrize("question", ["为什么另一套服务的 DNS 解析失败？", "原报告中的超时是什么意思？"])
def test_question_starts_generic_without_running_a_followup_classifier(state, question):
    s = state
    receipt = s.followups.submit(s.cid, s.rid, "question", question, owner_key=OWNER)
    s.backend.execute = lambda **kwargs: pytest.fail("不应先执行报告问答或意图判断")
    assert s.followups.worker.run_once()
    result = item(s)
    run = s.store.get_run(s.cid)
    assert result.diagnosis_run_id == run["run_id"] != s.rid
    assert result.status == "COMPLETED" and run["force_generic"] is True
    assert run["effective_problem_text"] == question
    assert counts(s) == (2, 0, 1) and s.engine.calls == []
    assert s.followups.submit(s.cid, s.rid, "question", question, owner_key=OWNER) == receipt
    assert not s.followups.worker.run_once() and counts(s) == (2, 0, 1)
    commands = []
    s.agent._execute_command = lambda cid, label, command, **kw: commands.append(command)
    assert s.agent.run_once(s.cid)
    assert commands[0].force_generic and commands[0].raw_problem_text == question
    context = commands[0].continuation_context
    assert context.parent_report_markdown == s.report.markdown
    reference = json.loads(context.followup_context_json)
    assert reference["original_problem"] == "故障时间是 10:00，仅客户端超时。"


def test_new_logs_are_materials_for_the_generic_skill_not_a_reason_to_classify(state):
    s = state
    old, new = upload(s, "client.zip", b"client"), upload(s, "server.zip", b"server")
    with s.repository.database_transaction() as db:
        value = s.store._load(db, s.cid)
        value["report_inputs"]["attachment_ids"] = [old]
        s.store._save(db, value)
    s.followups.submit(s.cid, s.rid, "question", "结合新增材料分析服务端。", attachment_ids=[new], owner_key=OWNER)
    assert s.followups.worker.run_once()
    message = s.store.get_conversation(s.cid).messages[0]
    assert message.attachment_ids == [old, new]
    assert s.backend.calls == []


def test_cancel_before_handoff_never_opens_a_diagnosis(state):
    s = state
    receipt = s.followups.submit(s.cid, s.rid, "question", "检查另一个方向。", owner_key=OWNER)
    s.followups.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=OWNER)
    assert not s.followups.worker.run_once()
    assert item(s).status == "CANCELLED" and counts(s) == (1, 0, 0)
    assert s.backend.calls == []


def test_active_diagnosis_blocks_new_questions_but_replays_original_receipt(state):
    s = state
    receipt = s.followups.submit(s.cid, s.rid, "first", "检查磁盘延迟。", owner_key=OWNER)
    assert s.followups.worker.run_once()
    current = s.followups.get(s.cid, s.rid, owner_key=OWNER)
    assert not current.can_ask and current.reason == "BUSY" and current.active_followup is None
    with pytest.raises(AgentStoreError, match="已有诊断") as blocked:
        s.followups.submit(s.cid, s.rid, "second", "检查另一方向。", owner_key=OWNER)
    assert blocked.value.code == "AGENT_DIAGNOSIS_BUSY"
    assert s.followups.submit(s.cid, s.rid, "first", "检查磁盘延迟。", owner_key=OWNER) == receipt
    assert counts(s) == (2, 0, 1)


def test_newer_report_requires_latest_entry_but_keeps_history_and_receipt(state):
    s = state
    receipt = s.followups.submit(s.cid, s.rid, "first", "检查磁盘延迟。", owner_key=OWNER)
    assert s.followups.worker.run_once()
    with s.repository.database_transaction() as db:
        child = s.store._load(db, s.cid)
        child.update(status="COMPLETED", report_available=True)
        s.store._save(db, child)
    page = s.followups.get(s.cid, s.rid, owner_key=OWNER)
    assert not page.can_ask and page.reason == "LATEST_REPORT_REQUIRED"
    assert page.items[0].diagnosis_run_id == child["run_id"]
    with pytest.raises(AgentStoreError) as blocked:
        s.followups.submit(s.cid, s.rid, "second", "继续分析。", owner_key=OWNER)
    assert blocked.value.code == "AGENT_REPORT_CHANGED"
    assert s.followups.submit(s.cid, s.rid, "first", "检查磁盘延迟。", owner_key=OWNER) == receipt


def test_historical_corrections_and_reports_are_reference_not_current_problem(state):
    s = state
    first = s.followups.submit(s.cid, s.rid, "old", "旧问题发生于 10:05。", owner_key=OWNER)
    task = s.followups.store.claim_task()
    assert s.followups.store.finish_task(task["followup_id"], answer="这是此前保存的解释。")
    current = "更正：另一个服务的磁盘抖动发生于 11:10。"
    s.followups.submit(s.cid, s.rid, "new", current, owner_key=OWNER,
        intent="CORRECTION", target_followup_id=first.followup_id)
    assert s.followups.worker.run_once()
    run = s.store.get_run(s.cid)
    assert run["effective_problem_text"] == current
    reference = json.loads(run["continuation_context"]["followup_context_json"])
    assert reference["prior_turns"][0]["text"] == "旧问题发生于 10:05。"
    assert reference["correction"]["target_answer_markdown"] == "这是此前保存的解释。"
    assert s.backend.calls == []


@pytest.mark.parametrize("retain_second", [False, True])
def test_retry_uses_saved_material_selection_without_reviving_excluded_logs(state, retain_second):
    s = state
    old, retained = upload(s, "excluded.zip", b"excluded"), upload(s, "retained.zip", b"retained")
    with s.repository.database_transaction() as db:
        parent = s.store._load(db, s.cid)
        parent["report_inputs"]["attachment_ids"] = [old, retained]
        s.store._save(db, parent)
    s.followups.submit(s.cid, s.rid, "first", "用已选择的材料分析。", owner_key=OWNER)
    task = s.followups.store.claim_task()
    selected = [retained] if retain_second else []
    assert s.followups.continue_diagnosis(task, dict(effective_problem_text="只采用本轮已选择的材料。",
        diagnosis_goal="核对新方向。", reason="排除旧日志。", attachment_ids=selected))
    with s.repository.database_transaction() as db:
        child = s.store._load(db, s.cid)
        child.update(status="FAILED", report_available=False)
        s.store._save(db, child)
    s.followups.submit(s.cid, s.rid, "retry", "时间改为 10:05，请再分析。", owner_key=OWNER)
    assert s.followups.worker.run_once()
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == selected
    assert s.backend.calls == []
