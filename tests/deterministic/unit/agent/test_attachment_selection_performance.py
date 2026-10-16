"""Attachment commit checks must read only the latest relevant message suffix."""
from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
import uuid

from problem_locator.agent.service import AgentConversationService
from problem_locator.storage.database import dialect_for
from tests.deterministic.unit.agent.test_store import _conversation, store
from tests.deterministic.unit.storage.test_state_repository import CASE_ID


def test_attachment_commit_rechecks_only_the_latest_selection_without_loading_history(store, monkeypatch):
    cid = _conversation(store)
    receipt = store.submit_message(cid, "first", "问题描述")
    selected = "00000000-0000-4000-8000-000000000701"
    ignored = "00000000-0000-4000-8000-000000000702"
    imported = "00000000-0000-4000-8000-000000000703"
    rid = store.get_run(cid)["run_id"]
    with store.repository.database_transaction() as db:
        template = json.loads(db.execute("SELECT body FROM agent_messages WHERE message_id=?",
            (receipt.message_id,)).fetchone()[0])
        for index in range(128 + 3):
            mid = str(uuid.uuid5(uuid.UUID(cid), f"history:{index}"))
            message = {**template, "message_id": mid, "request_id": f"history:{index}",
                "text": "x" * 65_536 if index < 128 else "补充说明", "status": "APPLIED", "attachment_ids": []}
            if index == 128:
                message["attachment_ids"] = [selected]
            if index == 130:
                message.update(status="UNUSED", attachment_ids=[ignored])
            db.execute("INSERT INTO agent_messages(message_id,conversation_id,request_id,fingerprint,body,receipt,run_id,event_sequence) "
                "VALUES (?,?,?,?,?,?,?,?)", (mid, cid, message["request_id"], "test", json.dumps(message), "{}", rid, index + 100))

    decoded, selections = [], []
    loads = json.loads

    def counted_loads(raw, *args, **kwargs):
        value = loads(raw, *args, **kwargs)
        if isinstance(value, dict) and "message_id" in value and "text" in value:
            decoded.append(value["message_id"])
        if value == [selected]:
            selections.append(value)
        return value

    def no_history(*args, **kwargs):
        raise AssertionError("attachment recheck must not load the conversation history")

    monkeypatch.setattr("problem_locator.agent.store.json.loads", counted_loads)
    monkeypatch.setattr(store, "get_conversation", no_history)
    case = SimpleNamespace(case_id=CASE_ID, case_revision=1)
    submitted = []
    service = SimpleNamespace(store=store, _check_run=lambda: None,
        _message_lock=lambda _: nullcontext(),
        uploads=SimpleNamespace(import_into_case=lambda *args, **kwargs: imported),
        application=SimpleNamespace(get_case=lambda _: SimpleNamespace(case_view=case)),
        _execute_command=lambda _cid, _label, command, **kwargs: submitted.append(command))

    assert AgentConversationService._supplement(service, cid, case, {}, [selected], "selection",
        message_id=receipt.message_id, selected_attachment_ids=[selected])

    assert decoded == []
    assert selections == [[selected], [selected]]
    assert len(submitted) == 1 and submitted[0].attachment_ids == [imported]
    with store.repository.database_read() as db:
        d = dialect_for(db)
        ids = d.json_document("body", "$.attachment_ids")
        query = (f"SELECT {ids} FROM agent_messages WHERE conversation_id=? AND run_id=? "
            f"AND {d.json_text('body', '$.status')}<>'UNUSED' AND {ids}<>'[]' "
            "ORDER BY event_sequence DESC LIMIT 1")
        plan = db.execute(("EXPLAIN " if d.postgres else "EXPLAIN QUERY PLAN ") + query, (cid, rid)).fetchall()
    descriptions = [row[0] if d.postgres else row[3] for row in plan]
    if not d.postgres:
        assert any("agent_messages_run" in row for row in descriptions), plan
        assert not any("TEMP B-TREE" in row for row in descriptions), plan
