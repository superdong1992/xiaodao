"""Attachment worker handoff keeps the complete durable command chain single-use."""
from __future__ import annotations

from collections import Counter
import json
import threading

from problem_locator.contracts import CreateCase, PrepareAttachment, SubmitSupplement, UploadAttachmentContent
from tests.deterministic.integration.test_intake_adoption import _start
from tests.deterministic.integration.test_website_agent import OWNER_KEY, website
from tests.deterministic.journey.test_rpc_timeout import PARAMETER_GROUP_A


def test_background_attachment_import_does_not_block_case_creation_or_repeat_intake(website, monkeypatch):
    stack, store, engine, service, _ = website
    first, _, message, _ = _start(website, PARAMETER_GROUP_A)
    first_case = store.get_run(first)["case_id"]
    entered, release, created, adopted = [threading.Event() for _ in range(4)]
    imports, commands = [], []
    original_import = service.uploads.import_into_case
    execute = stack.application.execute
    finish = store.finish_intake

    def held_import(cid, *args, **kwargs):
        imports.append(cid)
        entered.set()
        assert release.wait(10)
        return original_import(cid, *args, **kwargs)

    def recorded(self, command):
        if isinstance(command, (PrepareAttachment, SubmitSupplement)) and command.case_id == first_case:
            commands.append(type(command).__name__)
        elif isinstance(command, UploadAttachmentContent):
            commands.append(type(command).__name__)
        response = execute(command)
        if isinstance(command, CreateCase) and command.raw_problem_text == "另一会话的 RPC timeout":
            created.set()
        return response

    def finished(cid, *args, **kwargs):
        result = finish(cid, *args, **kwargs)
        if cid == first:
            adopted.set()
        return result

    monkeypatch.setattr(service.uploads, "import_into_case", held_import)
    monkeypatch.setattr(type(stack.application), "execute", recorded)
    monkeypatch.setattr(store, "finish_intake", finished)
    service.start()
    try:
        assert entered.wait(5)
        second = service.create_conversation("ordinary-background", owner_key=OWNER_KEY).conversation_id
        service.send_message(second, "ordinary-input", "另一会话的 RPC timeout", owner_key=OWNER_KEY)
        assert created.wait(5), "大附件尚未释放时，其他会话应已完成 Case 创建"
        assert not release.is_set() and store.get_run(second)["case_id"]
        assert len([request for request in engine.calls if request.conversation_id == first]) == 1
    finally:
        release.set()
    assert adopted.wait(10)
    with service._attachment_workers._condition:
        assert service._attachment_workers._condition.wait_for(
            lambda: first not in service._attachment_workers._tasks, 5)
    assert imports == [first]
    assert Counter(commands) == {"PrepareAttachment": 1, "UploadAttachmentContent": 1, "SubmitSupplement": 1}
    assert len([request for request in engine.calls if request.conversation_id == first]) == 1
    rid = store.get_run(first)["run_id"]
    assert store.get_attachment_import(message["attachment_ids"][0], rid)
    with stack.repository.database_read() as db:
        dispatches = [(json.loads(row[0]), row[1]) for row in db.execute(
            "SELECT payload,status FROM agent_dispatches WHERE conversation_id=? AND run_id=?", (first, rid))]
    assert Counter(payload["operation"] for payload, _ in dispatches) == {
        "CreateCase": 1, "INTAKE": 1, "PrepareAttachment": 1, "SubmitSupplement": 1}
    assert all(status == "COMPLETED" for _, status in dispatches)
