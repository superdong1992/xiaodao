"""Attachment timing and explicit selection must not lose available text inputs."""
from __future__ import annotations

import hashlib
import json
import threading

import pytest

from problem_locator.agent.intake import IntakeDecision, IntakeValue
from tests.deterministic.integration.test_intake_adoption import _start, _text
from tests.deterministic.integration.test_website_agent import _post, _preupload, website
from tests.deterministic.journey.test_rpc_timeout import ARCHIVE, PARAMETER_GROUP_A


_SELECT_ONE = "当前仅支持一份日志归档，请合并后上传，或重新选择一个附件。"


def _upload_another(client, prefix):
    data = ARCHIVE.read_bytes()
    prepared = _post(client, "/api/v1/agent/attachments", {"conversation_id": prefix.rsplit("/", 1)[-1],
        "request_id": "logs:2", "name": "other-logs.zip",
        "content_type": "application/zip", "declared_size": len(data), "declared_sha256": hashlib.sha256(data).hexdigest()})
    upload = prepared["upload"]
    response = client.put(upload["url"], content=data,
        headers={key: value for key, value in upload["required_headers"].items() if value is not None})
    assert response.status_code == 200, response.text
    return prepared["attachment"]["attachment_id"]


def _assert_report(website, conversation, prefix, expected_calls):
    stack, store, engine, service, client = website
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", view.failure
    case = stack.application.get_case(view.case_id).case_view
    assert {item.provenance.input_name: item.statement for item in case.user_facts} == PARAMETER_GROUP_A
    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    report = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    content = client.get(report["download_url"])
    assert content.status_code == 200 and hashlib.sha256(content.content).hexdigest() == report["sha256"]
    assert stack.archive.run_once()
    assert service.get_conversation(conversation).status == "COMPLETED"
    assert not service.run_once(conversation) and len(engine.calls) == expected_calls
    events = store.list_events(conversation, limit=500)
    assert not any(event.type == "agent.failed" for event in events)
    stream = client.get(prefix + "/events")
    frames = [json.loads(frame[6:]) for frame in stream.content.split(b"\n\n") if frame.startswith(b"data: ")]
    assert frames[-1]["type"] == "conversation.completed"
    return case, frames


def test_blank_attachment_message_extracts_the_preceding_uncovered_description(website):
    _stack, store, engine, service, client = website
    conversation, prefix, original, _receipt = _start(website, PARAMETER_GROUP_A, logs=False)
    attachment_id = _preupload(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "attachment", "text": None, "attachment_ids": [attachment_id]})
    assert service.run_once(conversation)
    assert len(engine.calls) == 1
    assert [item.text for item in engine.calls[0].messages if item.role == "USER"] == [original["text"], ""]
    case, _ = _assert_report(website, conversation, prefix, 1)
    assert case.raw_problem_text == original["text"]
    assert all(item.status == "APPLIED" for item in store.get_conversation(conversation).messages)
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))


def test_attachment_after_completed_intake_has_no_extra_model_or_case_queries(website, monkeypatch):
    stack, _store, engine, service, client = website
    conversation, prefix, _original, _receipt = _start(website, PARAMETER_GROUP_A, logs=False)
    assert service.run_once(conversation) and len(engine.calls) == 1
    attachment_id = _preupload(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "attachment", "attachment_ids": [attachment_id]})
    get_case = stack.application.get_case
    calling_thread, queries = threading.get_ident(), []

    def counted(self, *args, **kwargs):
        if threading.get_ident() == calling_thread:
            queries.append(args)
        return get_case(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(stack.application), "get_case", counted)
        assert service.run_once(conversation)
    # Existing advance, attachment reservation and supplement revision reads.
    # Selection and question publication add no authoritative Case queries.
    assert len(queries) == 3 and len(engine.calls) == 1
    _assert_report(website, conversation, prefix, 1)


@pytest.mark.parametrize("with_facts", [False, True], ids=["no-extracted-facts", "all-facts"])
def test_multiple_archives_wait_for_explicit_selection_and_single_id_recovers(website, with_facts):
    stack, store, engine, service, client = website
    values = PARAMETER_GROUP_A if with_facts else {}
    conversation, prefix, _original, _receipt = _start(website, values, logs=False)
    first, second = _preupload(client, prefix), _upload_another(client, prefix)
    multiple = {"request_id": "multiple", "attachment_ids": [first, second]}
    accepted = _post(client, prefix + "/messages", multiple)
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.status == "WAITING_INPUT" and view.failure is None
    assert _SELECT_ONE in view.current_questions and len(engine.calls) == 1
    case = stack.application.get_case(view.case_id).case_view
    assert {item.provenance.input_name: item.statement for item in case.user_facts} == values
    assert not stack.repository.read_case(view.case_id).attachments
    assert store.get_attachment(first).status == store.get_attachment(second).status == "READY"
    assert view.current_questions == [_SELECT_ONE if item.kind.value == "ATTACHMENT" else item.prompt
        for item in case.pending_requirements if item.status.value == "OPEN"]
    # A repeated authoritative projection must preserve the actionable notice,
    # as must GET refresh and an idempotent message retry.
    aggregate = stack.repository.read_case(view.case_id)
    with stack.repository.database_transaction() as database:
        store._project_case(database, store._load(database, conversation), aggregate)
    assert client.get(prefix).json()["data"]["current_questions"] == view.current_questions
    assert _post(client, prefix + "/messages", multiple) == accepted
    assert not service.run_once(conversation) and len(engine.calls) == 1
    assert store.get_conversation(conversation).last_event_id == view.last_event_id
    assert not any(event.type == "agent.failed" for event in store.list_events(conversation, limit=500))
    # Selecting the second ID replaces the earlier two-ID choice. The first
    # upload remains retained and is never silently imported or deleted.
    _post(client, prefix + "/messages", {"request_id": "choose-second", "attachment_ids": [second]})
    assert service.run_once(conversation) and len(engine.calls) == 1
    assert store.get_attachment(first).status == "READY" and store.get_attachment(second).status == "IMPORTED"
    assert len(stack.repository.read_case(view.case_id).attachments) == 1
    if not with_facts:
        current = service.get_conversation(conversation)
        assert _SELECT_ONE not in current.current_questions and current.failure is None
        _post(client, prefix + "/messages", {"request_id": "facts", "text": _text(PARAMETER_GROUP_A)})
        assert service.run_once(conversation)
    _case, events = _assert_report(website, conversation, prefix, 1 if with_facts else 2)
    assert any(event["type"] == "assistant.question" and _SELECT_ONE in event["data"]["questions"] for event in events)


def test_selection_arriving_during_intake_is_not_covered_or_overwritten(website, monkeypatch):
    _stack, store, engine, service, client = website
    conversation, prefix, _original, _receipt = _start(website, PARAMETER_GROUP_A, logs=False)
    first, second = _preupload(client, prefix), _upload_another(client, prefix)
    _post(client, prefix + "/messages", {"request_id": "multiple", "attachment_ids": [first, second]})
    intake = engine.intake
    newer = []

    def during_intake(request):
        newer.append(_post(client, prefix + "/messages", {"request_id": "choose-second", "attachment_ids": [second]}))
        return intake(request)

    monkeypatch.setattr(engine, "intake", during_intake)
    assert service.run_once(conversation)
    state = store.get_intake_state(conversation)
    assert state["pending"] and newer[0]["message_id"] not in state["covered_message_ids"]
    assert service.get_conversation(conversation).current_questions == []
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))
    monkeypatch.setattr(engine, "intake", intake)
    assert service.run_once(conversation)
    _assert_report(website, conversation, prefix, 1)
    assert store.get_attachment(first).status == "READY" and store.get_attachment(second).status == "IMPORTED"


def test_new_text_still_checks_frozen_fact_corrections_while_only_attachment_is_missing(website, monkeypatch):
    stack, store, engine, service, client = website
    conversation, prefix, _original, _receipt = _start(website, PARAMETER_GROUP_A, logs=False)
    assert service.run_once(conversation) and len(engine.calls) == 1
    before = stack.application.get_case(service.get_conversation(conversation).case_id).case_view

    def correction(request):
        engine.calls.append(request)
        assert not any(item.kind == "INPUT" for item in request.requirements)
        message = request.messages[-1]
        return IntakeDecision(action="NEED_CLARIFICATION", message="收到更正。", problem_fields=[], user_facts=[
            IntakeValue(name="client_slot", value="slot_9", source_message_id=message.message_id, source_quote="slot_9")])

    monkeypatch.setattr(engine, "intake", correction)
    _post(client, prefix + "/messages", {"request_id": "correct", "text": "client_slot=slot_9"})
    assert service.run_once(conversation) and len(engine.calls) == 2
    view = store.get_conversation(conversation)
    assert view.messages[-1].status == "UNUSED" and "请新建定位任务" in view.messages[-1].notice
    assert view.failure is None and stack.application.get_case(view.case_id).case_view == before
