"""Existing conversation text must satisfy Skill inputs before public questions."""
from __future__ import annotations

import hashlib
import json

import pytest

from problem_locator.agent.intake import IntakeDecision, IntakeValue
from problem_locator.contracts import ReviewPolicy, SubmitSupplement
from tests.deterministic.integration.test_website_agent import website, _post, _preupload
from tests.deterministic.journey.test_rpc_timeout import PARAMETER_GROUP_A


def _text(values):
    return "RPC 调用超时，现场信息如下：\n" + "\n".join(f"{name}：{value}" for name, value in values.items())


def _start(website, values, *, logs=True):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("first-inputs").conversation_id
    prefix = f"/api/v1/agent/conversations/{conversation}"
    attachments = [_preupload(client, prefix)] if logs else []
    message = {"request_id": "first", "text": _text(values), "attachment_ids": attachments}
    receipt = _post(client, prefix + "/messages", message)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = service.get_conversation(conversation)
    assert view.case_status == "WAITING_INPUT" and view.status == "INTAKE"
    assert view.current_questions == [] and engine.calls == []
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))
    return conversation, prefix, message, receipt


@pytest.mark.parametrize("review", [False, True])
def test_complete_first_message_reaches_report_and_archive_without_reasking(website, review):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    conversation, prefix, message, receipt = _start(website, PARAMETER_GROUP_A)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", view.failure
    case = stack.application.get_case(view.case_id).case_view
    assert case.raw_problem_text == message["text"]
    assert {fact.provenance.input_name: fact.statement for fact in case.user_facts} == PARAMETER_GROUP_A
    assert len(engine.calls) == 1
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))
    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    report = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    content = client.get(report["download_url"])
    assert content.status_code == 200 and hashlib.sha256(content.content).hexdigest() == report["sha256"]
    assert stack.archive.run_once()
    for _ in range(3):
        assert not service.run_once(conversation)
        assert _post(client, prefix + "/messages", message) == receipt
        assert client.get(prefix).json()["data"]["current_questions"] == []
    stream = client.get(prefix + "/events")
    events = [json.loads(frame[6:]) for frame in stream.content.split(b"\n\n") if frame.startswith(b"data: ")]
    assert events[-1]["type"] == "conversation.completed"
    assert not any(event["type"] == "assistant.question" for event in events)
    assert len(engine.calls) == 1


def test_clarification_action_adopts_partial_facts_then_asks_only_remaining_input(website, monkeypatch):
    stack, store, engine, service, client = website
    original = engine.intake
    monkeypatch.setattr(engine, "intake", lambda request: original(request).model_copy(update={"action": "NEED_CLARIFICATION"}))
    seven = {key: value for key, value in PARAMETER_GROUP_A.items() if key != "rpc_method"}
    conversation, prefix, _, _ = _start(website, seven)
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    case = stack.application.get_case(view.case_id).case_view
    assert {fact.provenance.input_name: fact.statement for fact in case.user_facts} == seven
    remaining = [item for item in case.pending_requirements if item.status.value == "OPEN"]
    assert [item.name for item in remaining] == ["rpc_method"]
    assert view.current_questions == [remaining[0].prompt]
    assert all(event.data["questions"] == view.current_questions for event in store.list_events(conversation, limit=500)
        if event.type == "assistant.question")
    for _ in range(10):
        assert not service.run_once(conversation)
    assert len(engine.calls) == 1
    _post(client, prefix + "/messages", {"request_id": "remaining", "text": "调用方法是 ReserveStock"})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    assert service.get_conversation(conversation).case_status == "RESOLVED"
    assert len(engine.calls) == 2


def test_saved_source_backed_draft_is_adopted_even_when_model_only_returns_new_fact(website, monkeypatch):
    stack, store, engine, service, client = website
    seven = {key: value for key, value in PARAMETER_GROUP_A.items() if key != "rpc_method"}
    conversation, prefix, _, _ = _start(website, seven)
    first = service.get_conversation(conversation).messages[0]
    store.set_draft(conversation, {"user_facts": [dict(name=key, value=value,
        source_message_id=first.message_id, source_quote=value) for key, value in seven.items()]})
    requests = []

    def only_new(request):
        requests.append(request)
        assert len(request.draft_user_facts) == 7 and request.frozen_user_facts == {}
        last = next(item for item in reversed(request.messages) if item.role == "USER")
        return IntakeDecision(action="NEED_CLARIFICATION", message="已收到。", problem_fields=[],
            user_facts=[IntakeValue(name="rpc_method", value="ReserveStock",
                source_message_id=last.message_id, source_quote="ReserveStock")])

    monkeypatch.setattr(engine, "intake", only_new)
    _post(client, prefix + "/messages", {"request_id": "last", "text": "ReserveStock"})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", view.failure
    assert len(requests) == 1
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))


@pytest.mark.parametrize("repeat_time_with_offset", [False, True])
def test_repeated_frozen_values_are_not_resubmitted_with_new_fact(website, monkeypatch, repeat_time_with_offset):
    stack, _, engine, service, client = website
    seven = {key: value for key, value in PARAMETER_GROUP_A.items() if key != "rpc_method"}
    conversation, prefix, _, _ = _start(website, seven)
    assert service.run_once(conversation)
    commands = []
    execute = stack.application.execute

    def recorded(self, command):
        if isinstance(command, SubmitSupplement):
            commands.append(command)
        return execute(command)

    monkeypatch.setattr(type(stack.application), "execute", recorded)

    repeated = {**PARAMETER_GROUP_A}
    if repeat_time_with_offset:
        repeated["problem_time"] = "2026-07-31T08:00:03+08:00"

    def restate(request):
        assert "problem_time" in {item.name for item in request.frozen_input_requirements}
        message = next(item for item in reversed(request.messages) if item.role == "USER")
        return IntakeDecision(action="SUBMIT_SUPPLEMENT", message="已收到。", problem_fields=[],
            user_facts=[IntakeValue(name=key, value=value, source_message_id=message.message_id,
                source_quote=value) for key, value in repeated.items()])

    monkeypatch.setattr(engine, "intake", restate)
    _post(client, prefix + "/messages", {"request_id": "restate", "text": _text(repeated)})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    assert service.get_conversation(conversation).case_status == "RESOLVED"
    assert len(commands) == 1 and commands[0].inputs == {"rpc_method": "ReserveStock"}


def test_followup_arriving_during_first_intake_keeps_questions_hidden(website, monkeypatch):
    stack, store, engine, service, client = website
    conversation, prefix, _, _ = _start(website, {})
    original = engine.intake

    def during_intake(request):
        _post(client, prefix + "/messages", {"request_id": "in-flight", "text": _text(PARAMETER_GROUP_A)})
        return original(request)

    monkeypatch.setattr(engine, "intake", during_intake)
    assert service.run_once(conversation)
    assert service.get_conversation(conversation).current_questions == []
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))
    monkeypatch.setattr(engine, "intake", original)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    assert service.get_conversation(conversation).case_status == "RESOLVED"
    assert len(engine.calls) == 2


def test_invalid_clarification_facts_are_filtered_and_missing_inputs_are_asked_once(website, monkeypatch):
    _, store, engine, service, _ = website
    conversation, _, _, _ = _start(website, {})
    calls = []

    def invalid(request):
        calls.append(request)
        first = next(item for item in request.messages if item.role == "USER")
        return IntakeDecision(action="NEED_CLARIFICATION", message="需要补充。", problem_fields=[],
            user_facts=[IntakeValue(name="undeclared_input", value=first.text,
                source_message_id=first.message_id, source_quote=first.text)])

    monkeypatch.setattr(engine, "intake", invalid)
    assert service.run_once(conversation)
    view = service.get_conversation(conversation)
    assert view.status == "WAITING_INPUT" and view.failure is None
    assert view.current_questions
    assert not service.run_once(conversation) and len(calls) == 1
    assert len([event for event in store.list_events(conversation, limit=500)
                if event.type == "assistant.question"]) == 1


def test_first_message_tolerates_context_quotes_duplicates_unknown_items_and_explicit_timezone(website, monkeypatch):
    stack, store, engine, service, client = website
    values = {**PARAMETER_GROUP_A, "problem_time": "2026-07-31T08:00:03+08:00"}

    def noisy_output(request):
        engine.calls.append(request)
        first = next(item for item in request.messages if item.role == "USER")
        facts = [IntakeValue(name=name, value=value, source_message_id=first.message_id,
            source_quote=first.text) for name, value in values.items()]
        facts.extend([facts[1], IntakeValue(name="extra_note", value="RPC", source_message_id=first.message_id,
            source_quote=first.text), facts[-1].model_copy(update={"source_message_id": "not-a-user-message"})])
        return IntakeDecision(action="NEED_CLARIFICATION", message="请补充剩余参数。",
            problem_fields=[IntakeValue(name="actual_behavior", value="RPC 调用超时",
                source_message_id=first.message_id, source_quote=first.text)], user_facts=facts)

    monkeypatch.setattr(engine, "intake", noisy_output)
    conversation, prefix, message, receipt = _start(website, values)
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", view.failure
    case = stack.application.get_case(view.case_id).case_view
    assert case.raw_problem_text == message["text"]
    assert {fact.provenance.input_name: fact.statement for fact in case.user_facts} == PARAMETER_GROUP_A
    assert len(engine.calls) == 1
    assert not any(event.type == "assistant.question" for event in store.list_events(conversation, limit=500))
    assert stack.archive.run_once()
    for _ in range(3):
        assert not service.run_once(conversation)
        assert _post(client, prefix + "/messages", message) == receipt
    assert len(engine.calls) == 1 and service.get_conversation(conversation).status == "COMPLETED"


@pytest.mark.parametrize("with_facts", [False, True], ids=["attachment-only", "partial-facts-and-attachment"])
def test_completed_intake_replay_after_adoption_only_finishes_coverage(website, monkeypatch, with_facts):
    stack, store, engine, service, _ = website
    values = {key: value for key, value in PARAMETER_GROUP_A.items() if key != "rpc_method"} if with_facts else {}
    conversation, _, _, _ = _start(website, values)
    with monkeypatch.context() as patch:
        # Simulate the interval after the authoritative command committed but
        # before the Agent coverage receipt was written. Do not repeat a model.
        patch.setattr(store, "finish_intake", lambda *args, **kwargs: None)
        assert service.run_once(conversation)
    before = stack.application.get_case(store.get_conversation(conversation).case_id).case_view
    assert before.status.value == "WAITING_INPUT"
    assert len(engine.calls) == 1 and store.get_intake_state(conversation)["pending"]
    assert all(item.status.value == "FULFILLED" for item in before.pending_requirements if item.kind.value == "ATTACHMENT")

    def unexpected_command(self, command):
        raise AssertionError("已采用的补充不能重复提交")

    monkeypatch.setattr(type(stack.application), "execute", unexpected_command)
    assert service.run_once(conversation)
    assert not service.run_once(conversation)
    view = store.get_conversation(conversation)
    assert view.status == "WAITING_INPUT" and view.failure is None
    assert len(engine.calls) == 1 and not store.get_intake_state(conversation)["pending"]
    assert stack.application.get_case(view.case_id).case_view == before
    assert view.current_questions == [item.prompt for item in before.pending_requirements if item.status.value == "OPEN"]
