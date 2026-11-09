"""A none-mode Skill keeps uploaded materials visible without claiming adoption."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from problem_locator.agent.store import AgentStore
from problem_locator.contracts import SpecializedInputPolicy
from tests.deterministic.unit.application.test_external_commands import _waiting_state, CASE_ID
from tests.deterministic.unit.agent.test_store import store, _conversation


@pytest.mark.parametrize("status", ["QUEUED", "APPLIED"])
def test_none_skill_notice_is_projected_while_running_and_survives_replay(store, status):
    conversation = _conversation(store)
    attachment = store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64)
    store.complete_attachment(attachment.attachment_id, "/private/material.zip")
    message = store.submit_message(conversation, "problem", "请分析配置问题", [attachment.attachment_id])
    if status == "APPLIED":
        store.set_message_status(conversation, message.message_id, "APPLIED")
    ref = _waiting_state().cases[CASE_ID].case.selected_skill_ref
    seen = []
    def policy(selected):
        seen.append(selected)
        return SpecializedInputPolicy(skill_ref=selected, optional_inputs=[], attachment_mode="none")
    store.specialized_input_policy = policy
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=None, status=SimpleNamespace(value="RUNNING"),
        case_revision=3, archive_status="NOT_REQUIRED", selected_skill_ref=ref)
    def project():
        with store.repository.database_transaction() as db:
            store._project_case(db, store._load(db, conversation), SimpleNamespace(case=case))
    project()
    project()
    view = store.get_conversation(conversation)
    assert seen == [ref, ref]
    assert view.status == "RUNNING" and view.current_questions == []
    assert view.messages[0].status == status
    assert view.messages[0].notice == "当前定位方法不需要附件，已上传的材料未用于本次诊断。"
    assert store.get_attachment(attachment.attachment_id).status == "READY"
    notices = [item for item in store.list_events(conversation) if item.type == "message.updated"
        and item.data.get("notice") == view.messages[0].notice]
    assert len(notices) == 1
    if status == "QUEUED":
        store.set_message_status(conversation, message.message_id, "PROCESSING")
        store.set_message_status(conversation, message.message_id, "APPLIED")
    assert store.get_conversation(conversation).messages[0].status == "APPLIED"
    assert store.get_conversation(conversation).messages[0].notice == view.messages[0].notice
    reopened = AgentStore(store.repository, runtime_epoch="epoch-one")
    assert reopened.get_conversation(conversation).messages[0].notice == view.messages[0].notice


def test_none_notice_does_not_use_an_unmatched_skill_policy(store):
    conversation = _conversation(store)
    attachment = store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64)
    store.complete_attachment(attachment.attachment_id, "/private/material.zip")
    store.submit_message(conversation, "problem", "问题", [attachment.attachment_id])
    ref = _waiting_state().cases[CASE_ID].case.selected_skill_ref
    store.specialized_input_policy = lambda selected: SpecializedInputPolicy(
        skill_ref=selected.model_copy(update={"content_hash": "f" * 64}), optional_inputs=[], attachment_mode="none")
    with store.repository.database_transaction() as db:
        body = store._load(db, conversation)
        store._project_unused_attachments(db, body, SimpleNamespace(selected_skill_ref=ref))
    assert "不需要附件" not in (store.get_conversation(conversation).messages[0].notice or "")
