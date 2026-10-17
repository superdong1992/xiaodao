"""Use the chosen evidence set and keep failed-attempt user input visible."""
from __future__ import annotations

import json

import pytest

from problem_locator.agent.models import AgentStoreError
from tests.deterministic.unit.agent.test_followup_handoff import DECISION, ask, counts, item, state
from tests.deterministic.unit.agent.test_report_continuation import OWNER, system, upload


def decision(ids):
    return {key: value for key, value in {**DECISION, "attachment_ids": ids}.items() if key != "log_mode"}


def baseline(s, ids):
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid, run_id=s.rid)
        body["report_inputs"]["attachment_ids"] = ids
        s.store._save(db, body)


def clarify(s, key, text, ids):
    receipt = s.followups.submit(s.cid, s.rid, key, text, owner_key=OWNER, attachment_ids=ids)
    task = s.followups.store.claim_task()
    assert s.followups.store.finish_task(receipt.followup_id, answer="请补充故障时间。")
    return task


def claim(s, key="reply", text="发生在10:05，请继续核对。", ids=()):
    s.followups.submit(s.cid, s.rid, key, text, owner_key=OWNER, attachment_ids=list(ids))
    return s.followups.store.claim_task()


def expire(s, aid):
    with s.repository.database_transaction() as db:
        raw = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=?", (aid,)).fetchone()[0]
        value = json.loads(raw)
        value["status"] = "EXPIRED"
        db.execute("UPDATE agent_attachments SET body=? WHERE attachment_id=?", (json.dumps(value), aid))


def context(s, task):
    _, history, snapshot = s.followups.store.task_context(task)
    return s.followups.decision_context(task, history, snapshot)


def test_explicit_selection_keeps_replacement_and_later_supplement_after_clarification(state):
    s = state
    old, replacement, extra = [upload(s, name + ".zip", name.encode()) for name in ("old", "replacement", "server")]
    baseline(s, [old])
    clarify(s, "replace", "旧日志日期错误，请改用本次日志。", [replacement])
    clarify(s, "add", "再补服务端日志，与上一条一起检查。", [extra])
    task = claim(s)
    assert s.followups.continue_diagnosis(task, decision([replacement, extra]))
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == [replacement, extra]
    assert counts(s) == (2, 0, 1) and s.engine.calls == []


def test_explicit_selection_can_remove_one_original_archive_without_new_upload(state):
    s = state
    wrong, retained = upload(s, "wrong.zip", b"wrong"), upload(s, "retained.zip", b"retained")
    baseline(s, [wrong, retained])
    task = claim(s, text="排除wrong.zip，只用原报告中的retained.zip。")
    assert s.followups.continue_diagnosis(task, decision([retained]))
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == [retained]


def test_material_context_marks_expired_alias_and_preserves_fresh_identical_upload(state, monkeypatch):
    s = state
    old = upload(s, "expired.zip", b"same bytes")
    clarify(s, "old", "先检查这份日志。", [old])
    expire(s, old)
    fresh = upload(s, "fresh.zip", b"same bytes")
    task = claim(s, ids=[fresh])
    monkeypatch.setattr(s.store, "get_attachment", lambda *args: pytest.fail("材料必须批量读取，不能逐个查询"))
    value = context(s, task)
    by_id = {entry["attachment_id"]: entry for entry in value["materials"]}
    assert set(by_id) == {old, fresh}
    assert by_id[old]["available"] is False and by_id[fresh]["available"] is True
    assert by_id[old]["origins"][0]["source"] == "FOLLOWUP"
    assert by_id[fresh]["origins"][0]["source"] == "CURRENT"
    assert value["latest_attempt"] is None


def test_unselected_expired_history_does_not_block_explicit_fresh_selection(state):
    s = state
    old = upload(s, "expired.zip", b"old")
    clarify(s, "old", "这份日志先待确认。", [old])
    expire(s, old)
    fresh = upload(s, "fresh.zip", b"fresh")
    task = claim(s, ids=[fresh])
    assert s.followups.continue_diagnosis(task, decision([fresh]))
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == [fresh]


@pytest.mark.parametrize("kind", ["unreferenced", "expired", "cancelled"])
def test_explicit_selection_cannot_use_unavailable_or_unauthorized_material(state, kind):
    s = state
    aid = upload(s, "candidate.zip", b"candidate")
    if kind == "expired":
        clarify(s, "candidate", "这份日志需要核对。", [aid])
        expire(s, aid)
    if kind == "cancelled":
        receipt = ask(s, request_id="cancelled", attachments=[aid])
        s.followups.stop(s.cid, s.rid, receipt.followup_id, "stop", owner_key=OWNER)
    task = claim(s)
    assert s.followups.continue_diagnosis(task, decision([aid]))
    assert counts(s) == (1, 0, 0)
    assert item(s).diagnosis_run_id is None and "尚未开始" in item(s).answer_markdown


def test_empty_material_context_without_linked_attempt_does_not_query_database(state, monkeypatch):
    s = state
    task = claim(s)
    _, history, snapshot = s.followups.store.task_context(task)
    monkeypatch.setattr(s.repository, "database_read", lambda: pytest.fail("普通无附件追问不应增加数据库查询"))
    assert s.followups.decision_context(task, history, snapshot) == {"materials": [], "latest_attempt": None}


def fail_attempt_with_messages(s, task, named_messages):
    assert s.followups.continue_diagnosis(task, decision([]))
    run_id = s.store.get_run(s.cid)["run_id"]
    for key, text, aid, status in named_messages:
        receipt = s.store.submit_message(s.cid, key, text, [aid] if aid else [], target_run_id=run_id)
        with s.repository.database_transaction() as db:
            raw = db.execute("SELECT body FROM agent_messages WHERE message_id=?", (receipt.message_id,)).fetchone()[0]
            value = json.loads(raw)
            value["status"] = status
            db.execute("UPDATE agent_messages SET body=? WHERE message_id=?", (json.dumps(value), receipt.message_id))
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body.update(status="FAILED", report_available=False, current_questions=["请补充发生时间。"],
            failure={"code": "AGENT_INTERRUPTED", "message": "本次诊断已中断。", "details": [], "retryable": False})
        s.store._save(db, body)
    return run_id


def test_latest_failed_attempt_preserves_user_corrections_and_materials_only(state):
    s = state
    older = upload(s, "older.zip", b"older")
    old_run = fail_attempt_with_messages(s, claim(s, key="first"), [("old-time", "旧尝试的时间10:01。", older, "APPLIED")])
    applied, pending, unused = [upload(s, name + ".zip", name.encode()) for name in ("applied", "pending", "unused")]
    last_run = fail_attempt_with_messages(s, claim(s, key="second"), [
        ("correct-time", "更正：发生时间10:05，服务端也有超时。", applied, "APPLIED"),
        ("pending-detail", "补充：实例为server-2，尚未采纳。", pending, "QUEUED"),
        ("discarded-detail", "这条信息未采用。", unused, "UNUSED"),
    ])
    task = claim(s, key="retry", text="按刚才补充的信息继续。")
    value = context(s, task)
    attempt = value["latest_attempt"]
    assert attempt["run_id"] == last_run != old_run and attempt["status"] == "FAILED"
    assert attempt["failure"] == {"code": "AGENT_INTERRUPTED", "message": "本次诊断已中断。"}
    messages = attempt["user_messages"]
    assert any(entry["text"].startswith("更正：") and entry["status"] == "APPLIED" for entry in messages)
    assert any(entry["text"].startswith("补充：") and entry["status"] == "QUEUED" for entry in messages)
    assert not any("旧尝试" in entry["text"] for entry in messages)
    candidate_ids = {entry["attachment_id"] for entry in value["materials"]}
    assert {applied, pending} <= candidate_ids
    assert older not in candidate_ids and unused not in candidate_ids
    selected = decision([applied, pending])
    selected["effective_problem_text"] = "发生时间10:05，服务端也有超时；实例为server-2。"
    assert s.followups.continue_diagnosis(task, selected)
    message = s.store.get_conversation(s.cid).messages[0]
    assert message.text == selected["effective_problem_text"] and message.attachment_ids == [applied, pending]


def test_explicit_selection_revalidates_availability_before_atomic_handoff(state, monkeypatch):
    s = state
    aid = upload(s, "selected.zip", b"selected")
    task = claim(s, ids=[aid])
    original = s.service._baseline
    def changed(*args):
        result = original(*args)
        expire(s, aid)
        return result
    monkeypatch.setattr(s.service, "_baseline", changed)
    assert s.followups.continue_diagnosis(task, decision([aid]))
    assert counts(s) == (1, 0, 0)
    assert item(s).diagnosis_run_id is None and "尚未开始" in item(s).answer_markdown


def test_latest_attempt_keeps_its_selected_input_without_reviving_older_unused_material(state):
    s = state
    retained, discarded = upload(s, "retained.zip", b"keep"), upload(s, "discarded.zip", b"discard")
    fail_attempt_with_messages(s, claim(s, key="first"), [
        ("keep", "使用这份补充日志。", retained, "APPLIED"),
        ("discard", "错误的日志，不采用。", discarded, "UNUSED"),
    ])
    second = claim(s, key="second")
    assert s.followups.continue_diagnosis(second, decision([retained]))
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body.update(status="FAILED", report_available=False)
        s.store._save(db, body)
    value = context(s, claim(s, key="third"))
    assert value["latest_attempt"]["initial_attachment_ids"] == [retained]
    assert {entry["attachment_id"] for entry in value["materials"]} == {retained}


def test_worker_receives_failed_attempt_correction_and_selects_its_uploaded_material(state):
    s = state
    fresh = upload(s, "server.zip", b"fresh server logs")
    run_id = fail_attempt_with_messages(s, claim(s, key="first"), [
        ("time", "确认故障时间10:05，服务端也超时。", fresh, "APPLIED"),
    ])
    s.followups.submit(s.cid, s.rid, "resume", "按刚才补充的内容继续。", owner_key=OWNER)
    s.backend.decision = decision([fresh])
    assert s.followups.worker.run_once()
    assert len(s.backend.calls) == 1 and s.engine.calls == []
    prompt = s.backend.calls[0]["prompt"]
    assert run_id in prompt and "确认故障时间10:05，服务端也超时。" in prompt
    assert item(s).diagnosis_run_id == s.store.get_run(s.cid)["run_id"]
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == [fresh]


def test_repeated_failure_keeps_correction_in_last_adopted_problem_text(state):
    s = state
    correction = "用户已确认：trace_id=81237，故障时间为10:05。"
    fail_attempt_with_messages(s, claim(s, key="first"), [("correction", correction, None, "APPLIED")])
    second = claim(s, key="second", text="按刚才的更正继续。")
    selected = decision([])
    selected["effective_problem_text"] = "客户端与服务端均超时。\n" + correction
    assert s.followups.continue_diagnosis(second, selected)
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body.update(status="FAILED", report_available=False)
        s.store._save(db, body)
    attempt = context(s, claim(s, key="third", text="继续核对。"))["latest_attempt"]
    assert attempt["effective_problem_text"] == selected["effective_problem_text"]
    assert attempt["user_messages"] == [], "整理后的初始输入不能冒充这轮新收到的用户补充"


def test_none_launcher_rejects_full_attempt_context_limit_before_acceptance(state):
    s = state
    fail_attempt_with_messages(s, claim(s, key="first"), [
        ("detail-" + str(index), "x" * 65536, None, "APPLIED") for index in range(4)
    ])
    s.followups.read_search_supported = False
    before = len(s.followups.get(s.cid, s.rid, owner_key=OWNER).items)
    with pytest.raises(AgentStoreError) as rejected:
        s.followups.submit(s.cid, s.rid, "long-retry", "根据我刚才补充的信息继续。", owner_key=OWNER)
    assert rejected.value.code == "AGENT_FOLLOWUP_CONTEXT_LIMIT" and rejected.value.status_code == 413
    assert len(s.followups.get(s.cid, s.rid, owner_key=OWNER).items) == before
    assert not s.followups.worker.run_once() and s.backend.calls == []
