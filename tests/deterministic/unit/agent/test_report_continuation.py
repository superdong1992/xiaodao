"""Confirmed corrections and material selection create an independent run."""
from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.continuation.models import CreateDraftRequest, UpdateDraftRequest, ConfirmDraftRequest
from problem_locator.interfaces.continuation_http import register_continuation_routes
from tests.deterministic.unit.storage.test_state_repository import _open

OWNER = "a" * 64
OTHER = "b" * 64
CASE = "00000000-0000-0000-0000-000000000001"
ARTIFACT = "00000000-0000-0000-0000-000000000070"


@pytest.fixture
def system(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository, runtime_epoch="test")
    engine = SimpleNamespace(calls=[])
    def prepare(**kwargs):
        engine.calls.append(kwargs)
        return {"intent": "CONTINUE", "effective_problem_text": "故障时间是 10:05，客户端和服务端均超时。", "change_note": "时间 10:00 → 10:05；补充服务端超时。"}
    engine.prepare_continuation = prepare
    agent = AgentConversationService(store, SimpleNamespace(), engine, repository.layout)
    created = store.create_conversation("create", owner_key=OWNER)
    cid, rid = created.conversation_id, created.run_id
    markdown = "# 原报告\n可能与客户端网络有关，仍需服务端日志。\n"
    report = SimpleNamespace(report_state="READY", format="markdown", case_id=CASE, markdown=markdown,
        artifact=SimpleNamespace(artifact_id=ARTIFACT, sha256=hashlib.sha256(markdown.encode()).hexdigest()))
    agent._conversation_detail = lambda *args, **kwargs: SimpleNamespace(result=report)
    with repository.database_transaction() as db:
        body = store._load(db, cid)
        body.update(status="COMPLETED", report_available=True, case_id=CASE, case_status="RESOLVED",
            report_inputs={"effective_problem_text": "故障时间是 10:00，仅客户端超时。", "attachment_ids": [], "runtime": {}})
        store._save(db, body)
    app = FastAPI()
    register_continuation_routes(app, agent)
    with TestClient(app, headers={"X-Agent-Owner-Key": OWNER}) as client:
        yield SimpleNamespace(repository=repository, store=store, agent=agent, service=agent.continuations,
            engine=engine, cid=cid, rid=rid, report=report, client=client, base=f"/api/v1/agent/conversations/{cid}")
    agent.shutdown(0)
    repository.close()


def create(s, **kwargs):
    return s.service.create(s.cid, s.rid, CreateDraftRequest(request_id="draft", **kwargs), owner_key=OWNER)


def confirm(s, draft, request_id="confirm"):
    return s.service.confirm(s.cid, draft.draft_id,
        ConfirmDraftRequest(request_id=request_id, expected_revision=draft.revision), owner_key=OWNER)


def upload(s, name, raw):
    digest = hashlib.sha256(raw).hexdigest()
    record = s.agent.uploads.prepare(s.cid, name, name, "application/zip", len(raw), digest)
    return s.agent.uploads.upload(record.attachment_id, record.attachment_id, "application/zip", len(raw), digest, io.BytesIO(raw)).attachment_id


def test_edit_is_preview_only_and_confirmation_is_idempotent(system):
    s = system
    draft = create(s, effective_problem_text="故障发生于 10:05，服务端也超时。", diagnosis_goal="验证服务端连接池")
    assert draft.status == "DRAFT" and s.engine.calls == []
    assert s.store.get_run(s.cid)["run_id"] == s.rid
    assert draft.input_changes.before_problem_text != draft.input_changes.after_problem_text
    result = confirm(s, draft)
    assert confirm(s, draft) == result
    assert confirm(s, draft, "another-click").run_id == result.run_id
    new = s.store.get_run(s.cid)
    assert new["run_id"] == result.run_id != s.rid and new["case_id"] is None
    assert new["parent_run_id"] == new["root_run_id"] == s.rid
    assert new["report_version"] == 2
    assert new["continuation_context"]["parent_report_markdown"] == s.report.markdown
    messages = s.store.get_conversation(s.cid).messages
    assert [item.text for item in messages] == [draft.effective_problem_text]
    assert s.report.markdown not in messages[0].text
    commands = []
    s.agent._execute_command = lambda cid, label, command, **kwargs: commands.append(command)
    assert s.agent.run_once(s.cid)
    assert commands[0].raw_problem_text == draft.effective_problem_text
    assert commands[0].initial_user_facts == []
    assert commands[0].continuation_context.goal == "验证服务端连接池"
    assert s.store.get_run(s.cid, s.rid)["report_available"]


def test_natural_correction_uses_one_background_intake_then_requires_confirmation(system):
    s = system
    draft = create(s, text="时间应为 10:05，服务端也超时。")
    assert draft.status == "PREPARING" and not s.engine.calls
    assert s.service.get(s.cid, draft.draft_id, owner_key=OWNER) == draft
    with pytest.raises(AgentStoreError, match="最新草稿"):
        confirm(s, draft)
    assert s.service.run_once(s.cid)
    prepared = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    assert prepared.status == "DRAFT" and "10:05" in prepared.effective_problem_text
    assert prepared.revision == 2 and len(s.engine.calls) == 1
    assert not s.service.run_once(s.cid)
    assert s.store.get_run(s.cid)["run_id"] == s.rid
    confirm(s, prepared)


@pytest.mark.parametrize("question_field", ["text", "diagnosis_goal", "change_note"])
def test_report_question_in_continuation_is_followup_and_never_opens_a_run(system, question_field):
    s = system
    question = "报告为什么更倾向于客户端网络异常？"
    before = s.store.get_run(s.cid)
    calls = []
    def ask(**kwargs):
        calls.append(kwargs)
        return {"intent": "FOLLOWUP", "effective_problem_text": kwargs["source_problem_text"],
                "change_note": "解释原报告的判断依据。"}
    s.engine.prepare_continuation = ask
    draft = create(s, **{question_field: question})
    assert draft.status == "PREPARING"
    assert s.service.run_once(s.cid)
    prepared = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    assert prepared.status == "FOLLOWUP"
    assert prepared.followup_text == question
    assert prepared.effective_problem_text == prepared.source_problem_text
    assert prepared.confirmed_run_id is None
    assert calls[0]["previous_report_markdown"] == s.report.markdown
    assert calls[0]["has_input_changes"] is False
    with pytest.raises(AgentStoreError) as rejected:
        confirm(s, prepared)
    assert rejected.value.code == "AGENT_FOLLOWUP_REQUIRED"
    with pytest.raises(AgentStoreError) as edited:
        s.service.update(s.cid, draft.draft_id, UpdateDraftRequest(request_id="bypass", expected_revision=prepared.revision), owner_key=OWNER)
    assert edited.value.code == "AGENT_FOLLOWUP_REQUIRED"
    assert s.store.get_run(s.cid) == before
    with s.repository.database_read() as db:
        assert db.execute("SELECT COUNT(*) FROM agent_conversation_runs WHERE conversation_id=?", (s.cid,)).fetchone()[0] == 1
    assert not s.service.run_once(s.cid)


def test_older_saved_draft_without_followup_field_is_readable(system):
    import json
    s = system
    draft = create(s)
    with s.repository.database_transaction() as db:
        row = db.execute("SELECT body FROM agent_continuation_drafts WHERE draft_id=?", (draft.draft_id,)).fetchone()
        value = json.loads(row[0])
        value.pop("followup_text")
        db.execute("UPDATE agent_continuation_drafts SET body=? WHERE draft_id=?", (json.dumps(value), draft.draft_id))
    restored = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    assert restored.status == "DRAFT" and restored.followup_text == ""
    confirm(s, restored)


def test_new_logs_cannot_be_discarded_by_a_followup_classification(system):
    s = system
    aid = upload(s, "server.zip", b"new logs")
    s.engine.prepare_continuation = lambda **kwargs: {"intent": "FOLLOWUP",
        "effective_problem_text": kwargs["source_problem_text"], "change_note": "核对原报告。"}
    draft = create(s, text="结合这个日志解释报告", attachment_ids=[aid])
    assert s.service.run_once(s.cid)
    prepared = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    assert prepared.status == "DRAFT" and prepared.attachment_ids == [aid]
    assert prepared.followup_text == ""
    assert s.store.get_run(s.cid)["run_id"] == s.rid


def test_append_replace_and_digest_dedup_use_final_materials(system):
    s = system
    old, new, duplicate = upload(s, "old.zip", b"old"), upload(s, "new.zip", b"new"), upload(s, "duplicate.zip", b"new")
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body["report_inputs"]["attachment_ids"] = [old]
        s.store._save(db, body)
    draft = create(s, attachment_ids=[new, duplicate])
    assert draft.attachment_ids == [old, new]
    assert [item.attachment_id for item in draft.input_changes.added_attachments] == [new]
    edited = s.service.update(s.cid, draft.draft_id, UpdateDraftRequest(request_id="edit", expected_revision=1,
        log_mode="REPLACE", attachment_ids=[new]), owner_key=OWNER)
    assert edited.attachment_ids == [new]
    assert [item.attachment_id for item in edited.input_changes.removed_attachments] == [old]
    confirm(s, edited)
    assert s.store.get_conversation(s.cid).messages[0].attachment_ids == [new]


def test_stale_edit_different_owner_and_second_draft_cannot_change_active_run(system):
    s = system
    draft = create(s)
    with pytest.raises(AgentStoreError) as denied:
        s.service.get(s.cid, draft.draft_id, owner_key=OTHER)
    assert denied.value.status_code == 404
    s.service.update(s.cid, draft.draft_id, UpdateDraftRequest(request_id="edit", expected_revision=1,
        effective_problem_text="修正后的完整问题"), owner_key=OWNER)
    with pytest.raises(AgentStoreError) as stale:
        confirm(s, draft)
    assert stale.value.code == "AGENT_DRAFT_CHANGED"
    first = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    second = s.service.create(s.cid, s.rid, CreateDraftRequest(request_id="second"), owner_key=OWNER)
    confirm(s, first)
    with pytest.raises(AgentStoreError) as busy:
        confirm(s, second, "confirm-second")
    assert busy.value.code == "AGENT_DIAGNOSIS_BUSY"


def test_report_versions_advance_only_after_publication_and_keep_previous_on_failure(system):
    s = system
    first = s.service.versions(s.cid, s.rid, owner_key=OWNER)
    assert first.latest_run_id == s.rid and first.can_continue
    result = confirm(s, create(s))
    during = s.service.versions(s.cid, s.rid, owner_key=OWNER)
    assert during.latest_run_id == s.rid and len(during.versions) == 1 and not during.can_continue
    s.store.fail_conversation(s.cid)
    assert s.service.versions(s.cid, s.rid, owner_key=OWNER).can_continue
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid, run_id=result.run_id)
        body.update(status="COMPLETED", report_available=True)
        s.store._save(db, body)
    latest = s.service.versions(s.cid, result.run_id, owner_key=OWNER)
    assert [item.version for item in latest.versions] == [1, 2]
    assert latest.latest_run_id == result.run_id and latest.can_continue
    with pytest.raises(AgentStoreError) as old:
        s.service.create(s.cid, s.rid, CreateDraftRequest(request_id="old"), owner_key=OWNER)
    assert old.value.code == "AGENT_REPORT_CHANGED"


def test_http_draft_confirm_and_versions_require_owner_and_do_not_reuse_message_route(system):
    s = system
    route = s.base + f"/runs/{s.rid}/continuation-drafts"
    bad = s.client.post(route, json={"request_id": "draft"}, headers={"X-Agent-Owner-Key": OTHER})
    assert bad.status_code == 404
    response = s.client.post(route, json={"request_id": "draft", "effective_problem_text": "新的完整问题"})
    assert response.status_code == 200, response.text
    draft = response.json()["data"]
    path = s.base + "/continuation-drafts/" + draft["draft_id"]
    assert s.client.get(path).json()["data"] == draft
    assert s.client.get(path + "?share=true").status_code == 400
    response = s.client.post(path + "/confirm", json={"request_id": "confirm", "expected_revision": draft["revision"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["run_id"] != s.rid
    versions = s.client.get(s.base + f"/runs/{s.rid}/report-versions")
    assert versions.status_code == 200 and len(versions.json()["data"]["versions"]) == 1


def test_failed_preparation_can_be_edited_without_repeating_model_and_deleted_draft_cannot_publish(system):
    s = system
    def fail(**kwargs):
        raise RuntimeError("backend failed")
    s.engine.prepare_continuation = fail
    draft = create(s, text="更正时间")
    assert s.service.run_once(s.cid)
    failed = s.service.get(s.cid, draft.draft_id, owner_key=OWNER)
    assert failed.status == "FAILED"
    fixed = s.service.update(s.cid, draft.draft_id, UpdateDraftRequest(request_id="fix", expected_revision=failed.revision,
        effective_problem_text="故障时间 10:05"), owner_key=OWNER)
    assert fixed.status == "DRAFT"
    s.agent.delete_conversation(s.cid, owner_key=OWNER)
    with pytest.raises(AgentStoreError) as deleted:
        confirm(s, fixed)
    assert deleted.value.status_code == 404


def test_preparation_recovery_marks_uncertain_work_editable_without_replay(system):
    s = system
    draft = create(s, text="时间应为 10:05")
    with s.repository.database_transaction() as db:
        db.execute("UPDATE agent_continuation_drafts SET status='RUNNING' WHERE draft_id=?", (draft.draft_id,))
    s.service.recover()
    assert s.service.get(s.cid, draft.draft_id, owner_key=OWNER).status == "FAILED"
    assert not s.service.run_once(s.cid)
    assert s.engine.calls == []


@pytest.mark.parametrize("conversation_state", ["deleted", "missing"])
def test_preparation_recovery_skips_deleted_or_missing_conversations_without_blocking_start(system, conversation_state):
    s = system
    draft = create(s, text="时间应为 10:05")
    with s.repository.database_transaction() as db:
        db.execute("UPDATE agent_continuation_drafts SET status='RUNNING' WHERE draft_id=?", (draft.draft_id,))
    if conversation_state == "deleted":
        s.agent.delete_conversation(s.cid, owner_key=OWNER)
    else:
        with s.repository.database_transaction() as db:
            db.execute("DELETE FROM agent_conversations WHERE conversation_id=?", (s.cid,))

    s.agent.start(runtime_epoch="restarted")
    try:
        assert s.agent._thread.is_alive() and s.agent._control_thread.is_alive()
        with s.repository.database_read() as db:
            row = db.execute("SELECT status FROM agent_continuation_drafts WHERE draft_id=?", (draft.draft_id,)).fetchone()
        assert row == ("RUNNING",)
        assert s.engine.calls == []
    finally:
        assert s.agent.shutdown(2)


def test_natural_preparation_reuses_bounded_intake_and_writes_only_a_draft(tmp_path):
    import json
    from problem_locator.agent.intake import ClaudeIntakeEngine, INTAKE_RESOURCE_LIMITS
    from tests.deterministic.unit.agent.test_intake import _Backend
    expected = {"intent": "CONTINUE", "effective_problem_text": "故障时间 10:05；服务端超时。", "change_note": "10:00 → 10:05"}
    backend = _Backend(json.dumps(expected, ensure_ascii=False))
    engine = ClaudeIntakeEngine("not-launched", workspace_root=tmp_path / "workspaces", backend=backend)
    result = engine.prepare_continuation(source_problem_text="故障时间 10:00；服务端超时。", text="时间应为 10:05")
    assert result == expected and len(backend.calls) == 1
    call = backend.calls[0]
    assert call["backend_phase"] == "INTAKE" and call["file_access"] == "none"
    assert call["broker_environment"] is None and call["resource_limits"] == INTAKE_RESOURCE_LIMITS
    assert "10:00" in call["prompt"] and "10:05" in call["prompt"]
    assert not list((call["workspace_root"] / "output").iterdir())
    assert json.loads((call["workspace_root"] / "runtime" / "continuation-result.json").read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize("source_text", ["设备 10 出现 RPC 超时。", "设备 10 出现 RPC 超时；timeout_seconds=10。",
    "timeout_seconds=100", "timeout_seconds=10", "retry_count=100\ntimeout_seconds=100\nobserved_requests=100"])
def test_report_inputs_retain_named_facts_when_only_their_value_occurs_in_source(system, source_text):
    from problem_locator.application.preparation import make_user_fact
    from problem_locator.contracts import UserFactInput
    from problem_locator.continuation.service import capture_report_inputs

    # 实际用户事实把名称和取值分开保存。设备号中的“10”不能替代超时或重试次数。
    facts = [make_user_fact(UserFactInput(name=name, value="10"), item_id=item_id,
                           trigger_id=CASE, created_revision=1)
             for name, item_id in [("timeout_seconds", CASE), ("retry_count", ARTIFACT)]]
    ref = SimpleNamespace(model_dump=lambda **kwargs: {"name": "test-ref", "version": "1"})
    job = SimpleNamespace(generic_supplement_texts=["retry_count=10", "observed_requests=10"], attachment_refs=[], skill_ref=None,
                          logparse_tool_ref=None, generic_skill_name="generic-problem-locator",
                          agent_profile_ref=ref, context_policy_ref=ref)
    case = SimpleNamespace(raw_problem_text=source_text, generic_result_v2=SimpleNamespace(source_job_id="report-job"),
                           diagnosis_state=SimpleNamespace(user_facts=facts))
    aggregate = SimpleNamespace(case=case, jobs={"report-job": job})
    with system.repository.database_transaction() as db:
        body = system.store._load(db, system.cid)
        body["model_configuration_fingerprint"] = "d" * 64
        capture_report_inputs(db, body, aggregate)
        system.store._save(db, body)
    effective = body["report_inputs"]["effective_problem_text"]
    assert effective.startswith(source_text)
    # 必须保留完整字段取值；100 的前缀、设备号和正文中的片段都不能替代确认信息。
    assert [line for line in effective.splitlines() if line == "timeout_seconds=10"] == ["timeout_seconds=10"]
    assert [line for line in effective.splitlines() if line == "retry_count=10"] == ["retry_count=10"]
    assert [line for line in effective.splitlines() if line == "observed_requests=10"] == ["observed_requests=10"]
    assert body["report_inputs"]["runtime"]["model"] == "d" * 64

    draft = create(system)
    assert draft.effective_problem_text == effective
    confirm(system, draft)
    commands = []
    system.agent._execute_command = lambda cid, label, command, **kwargs: commands.append(command)
    assert system.agent.run_once(system.cid)
    assert commands[0].raw_problem_text == effective


def test_initial_case_and_confirmed_continuation_keep_their_own_model_configuration(system):
    s = system
    s.agent.model_configuration_fingerprint = "a" * 64
    initial = s.store.create_conversation("model-config-initial", owner_key=OWNER)
    s.store.submit_message(initial.conversation_id, "initial-input", "请定位服务超时。")
    commands = []
    s.agent._execute_command = lambda cid, label, command, **kwargs: commands.append(command)
    assert s.agent.run_once(initial.conversation_id)
    assert s.store.get_run(initial.conversation_id)["model_configuration_fingerprint"] == "a" * 64
    assert commands[0].raw_problem_text == "请定位服务超时。"

    s.agent.model_configuration_fingerprint = "b" * 64
    continued = confirm(s, create(s))
    assert s.store.get_run(s.cid, continued.run_id)["model_configuration_fingerprint"] == "b" * 64
    s.agent.model_configuration_fingerprint = "c" * 64
    assert s.agent.run_once(s.cid)
    assert s.store.get_run(s.cid, continued.run_id)["model_configuration_fingerprint"] == "b" * 64
    assert s.store.get_run(initial.conversation_id)["model_configuration_fingerprint"] == "a" * 64


@pytest.mark.parametrize(("old", "new", "expected"), [
    ("a" * 64, "b" * 64, ["模型执行配置已更新"]),
    ("a" * 64, "a" * 64, []),
    (None, "b" * 64, ["旧版报告未记录模型执行配置，无法比较。"]),
    ("a" * 64, None, ["本版报告未记录模型执行配置，无法比较。"]),
])
def test_report_versions_compare_model_configuration_without_claiming_resolved_models(system, old, new, expected):
    s = system
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid)
        body["report_inputs"]["runtime"]["model"] = old
        s.store._save(db, body)
    s.agent.model_configuration_fingerprint = new
    continued = confirm(s, create(s))
    with s.repository.database_transaction() as db:
        body = s.store._load(db, s.cid, run_id=continued.run_id)
        body.update(status="COMPLETED", report_available=True,
                    report_inputs={"effective_problem_text": "新问题", "attachment_ids": [], "runtime": {"model": new}})
        s.store._save(db, body)
    versions = s.service.versions(s.cid, continued.run_id, owner_key=OWNER)
    assert versions.versions[0].runtime_changes == []
    assert versions.versions[1].runtime_changes == expected
