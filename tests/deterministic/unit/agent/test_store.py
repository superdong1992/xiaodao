"""Website history, idempotency and atomic terminal publication regression tests."""
from __future__ import annotations

import sqlite3
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from problem_locator.agent.models import AgentEvent, AgentStoreError, SendMessageRequest, EVENT_PAYLOAD_MODELS
from problem_locator.agent.store import AgentStore
from problem_locator.contracts import ApplicationErrorDetail, CaseStatus, ExecutionFailure, GenericResult, IdempotencyRecord
from tests.deterministic.unit.storage.test_state_repository import (
    CASE_ID, JOB_ID, _empty_mutation, _finish, _open, _populate,
)


@pytest.fixture
def store(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository, runtime_epoch="epoch-one")
    yield store
    repository.close()


def _conversation(store, key="website:create:1"):
    return store.create_conversation(key).conversation_id


def _artifact_data():
    return {"artifact_id": "00000000-0000-0000-0000-000000000030", "kind": "USER_RESULT",
        "name": "diagnosis-result.json", "content_type": "application/json", "resource_kind": "FILE",
        "size": 123, "sha256": "a" * 64, "created_by_job_id": JOB_ID,
        "created_at": "2026-07-31T00:00:00.000Z", "downloadable": True}


def _event_payloads():
    return {
        "message.accepted": {"message_id": JOB_ID, "request_id": "message:1", "text": "问题描述",
            "attachment_ids": [], "status": "QUEUED", "created_at": "2026-07-31T00:00:00.000Z", "notice": None},
        "message.updated": {"message_id": JOB_ID, "status": "APPLIED", "notice": None},
        "assistant.question": {"questions": ["请提供问题时间。"]},
        "agent.progress": {"stage": "VERIFYING", "message": "正在核对证据"},
        "case.updated": {"status": "RUNNING", "case_revision": 1},
        "result.available": {"status": "RESOLVED", "artifacts": [_artifact_data()], "result_field": "final_result"},
        "archive.updated": {"status": "PENDING", "artifacts": []},
        "agent.failed": {"code": "AGENT_EXECUTION_FAILED", "message": "本次定位未能完成，请重新发起任务。"},
        "conversation.interrupted": {"code": "AGENT_INTERRUPTED", "message": "服务已重启，本次任务已中断，请重新发起。"},
        "conversation.completed": {"status": "COMPLETED"},
        "attachment.updated": {"attachment_id": JOB_ID, "conversation_id": CASE_ID, "request_id": "attachment:1",
            "name": "logs.zip", "content_type": "application/zip", "size": 1, "sha256": "b" * 64,
            "status": "READY", "created_at": "2026-07-31T00:00:00.000Z", "case_attachment_id": None},
    }


@pytest.mark.parametrize("kind", list(EVENT_PAYLOAD_MODELS))
def test_every_public_event_data_has_strict_payload_validation_and_schema(kind):
    import jsonschema
    payload = _event_payloads()[kind]
    event = {"sequence": 1, "conversation_id": CASE_ID, "type": kind,
             "created_at": "2026-07-31T00:00:00.000Z", "data": payload}
    schema = AgentEvent.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(event, schema)
    parsed = AgentEvent.model_validate(event)
    assert parsed.data == payload
    assert AgentEvent.model_validate_json(parsed.model_dump_json()) == parsed
    assert {entry["properties"]["type"]["const"] for entry in schema["oneOf"]} == set(EVENT_PAYLOAD_MODELS)
    for field in payload:
        invalid = deepcopy(event)
        invalid["data"][field] = {"storage_key": "/private/secret"}
        with pytest.raises(ValidationError):
            AgentEvent.model_validate(invalid)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
    extra = deepcopy(event)
    extra["data"]["internal_path"] = "/private/secret"
    with pytest.raises(ValidationError):
        AgentEvent.model_validate(extra)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(extra, schema)


@pytest.mark.parametrize("field,value", [("size", True), ("size", "123"), ("downloadable", 1),
                                         ("downloadable", "true"), ("created_by_job_id", {}),
                                         ("storage_key", "/private/secret"), ("metadata", {"raw_log": "secret"})])
def test_report_event_rejects_coerced_or_internal_nested_artifact_metadata(field, value):
    payload = _event_payloads()["result.available"]
    payload["artifacts"][0][field] = value
    with pytest.raises(ValidationError):
        AgentEvent(sequence=1, conversation_id=CASE_ID, type="result.available", created_at="now", data=payload)


def test_event_semantic_values_cannot_override_safe_failure_or_progress_text():
    for kind, field, invalid in [("agent.progress", "message", "internal command /private/token"),
                                 ("agent.failed", "code", "INTERNAL_STACK_TRACE"),
                                 ("agent.failed", "message", "secret exception"),
                                 ("case.updated", "case_revision", True),
                                 ("conversation.completed", "status", "RUNNING")]:
        payload = _event_payloads()[kind]
        payload[field] = invalid
        with pytest.raises(ValidationError):
            AgentEvent(sequence=1, conversation_id=CASE_ID, type=kind, created_at="now", data=payload)


def test_create_and_message_retries_return_exact_receipt_even_after_close(store):
    conversation = _conversation(store)
    assert _conversation(store) == conversation
    accepted = store.submit_message(conversation, "message:1", "支付请求超时")
    assert store.submit_message(conversation, "message:1", "支付请求超时") == accepted
    assert len(store.list_events(conversation)) == 1
    store.fail_conversation(conversation)
    assert store.submit_message(conversation, "message:1", "支付请求超时") == accepted
    with pytest.raises(AgentStoreError, match="内容不能更改"):
        store.submit_message(conversation, "message:1", "另一问题")
    with pytest.raises(AgentStoreError, match="另建任务"):
        store.submit_message(conversation, "message:2", "新问题")


def test_concurrent_retries_publish_one_message_and_monotonic_events(store):
    conversation = _conversation(store)
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(lambda _: store.submit_message(conversation, "one", "超时"), range(16)))
    assert len({receipt.message_id for receipt in receipts}) == 1
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: store.submit_message(conversation, f"next:{index}", "补充说明"), range(30)))
    events = store.list_events(conversation)
    assert [event.sequence for event in events] == list(range(1, 32))
    assert [event.sequence for event in store.list_events(conversation, after=10, limit=3)] == [11, 12, 13]
    with pytest.raises(AgentStoreError):
        store.list_events(conversation, limit=501)
    with pytest.raises(AgentStoreError):
        store.list_events(conversation, after=32)


def _project_waiting_questions(store, conversation, questions, revision=1, *, attachment_questions=()):
    case = SimpleNamespace(case_id=CASE_ID, active_job_id=None,
        status=CaseStatus.WAITING_INPUT, archive_status="NOT_REQUIRED", case_revision=revision,
        diagnosis_state=SimpleNamespace(pending_requirements=[SimpleNamespace(
            requirement_id="question-" + str(index),
            kind=SimpleNamespace(value="ATTACHMENT" if question in attachment_questions else "INPUT"),
            status=SimpleNamespace(value="OPEN"), prompt=question) for index, question in enumerate(questions)]))
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, conversation), SimpleNamespace(case=case))


def _stored_conversation_body(store, conversation):
    with store.repository.database_read() as db:
        return db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (conversation,)).fetchone()[0]


def test_first_applied_message_holds_questions_until_intake_finishes_once(store):
    conversation = _conversation(store)
    receipt = store.submit_message(conversation, "first", "设备型号已经提供：X1")
    store.set_message_status(conversation, receipt.message_id, "PROCESSING")
    store.set_message_status(conversation, receipt.message_id, "APPLIED")
    _project_waiting_questions(store, conversation, ["请提供设备型号。"])
    view = store.get_conversation(conversation)
    assert view.status == "INTAKE" and view.case_status == "WAITING_INPUT"
    assert view.current_questions == []
    assert not any(item.type == "assistant.question" for item in store.list_events(conversation))
    assert store.get_intake_state(conversation) == {
        "pending": True, "covered_message_ids": [], "pending_commands": False, "ready": True}
    store.finish_intake(conversation, [receipt.message_id])
    view = store.get_conversation(conversation)
    assert view.status == "WAITING_INPUT" and view.current_questions == ["请提供设备型号。"]
    assert view.messages[0].status == "APPLIED"
    assert store.get_intake_state(conversation)["covered_message_ids"] == [receipt.message_id]
    previous = _stored_conversation_body(store, conversation)
    events = store.list_events(conversation)
    store.finish_intake(conversation, [receipt.message_id])
    assert _stored_conversation_body(store, conversation) == previous
    assert store.list_events(conversation) == events
    assert not any(key.startswith("intake_") for key in view.model_dump())


def test_intake_finish_cannot_cover_a_concurrently_received_message(store):
    conversation = _conversation(store)
    first = store.submit_message(conversation, "first", "型号 X1")
    _project_waiting_questions(store, conversation, ["请提供版本。"])
    second = store.submit_message(conversation, "second", "版本 V2")
    store.finish_intake(conversation, [first.message_id])
    assert store.get_intake_state(conversation) == {
        "pending": True, "covered_message_ids": [first.message_id], "pending_commands": False, "ready": True}
    assert store.get_conversation(conversation).current_questions == []
    assert not any(item.type == "assistant.question" for item in store.list_events(conversation))
    store.finish_intake(conversation, [second.message_id, first.message_id])
    assert store.get_intake_state(conversation)["covered_message_ids"] == [first.message_id, second.message_id]
    assert not store.get_intake_state(conversation)["pending"]
    assert store.get_conversation(conversation).current_questions == ["请提供版本。"]


def test_intake_finish_uses_latest_authoritative_remaining_questions(store):
    conversation = _conversation(store)
    first = store.submit_message(conversation, "first", "型号 X1，版本 V2")
    _project_waiting_questions(store, conversation, ["请提供型号。", "请提供版本。", "请上传日志。"])
    _project_waiting_questions(store, conversation, ["请上传日志。"], revision=2)
    assert not any(item.type == "assistant.question" for item in store.list_events(conversation))
    store.finish_intake(conversation, [first.message_id])
    questions = [item.data["questions"] for item in store.list_events(conversation) if item.type == "assistant.question"]
    assert questions == [["请上传日志。"]]
    _project_waiting_questions(store, conversation, ["请上传日志。"], revision=2)
    assert [item.data["questions"] for item in store.list_events(conversation)
            if item.type == "assistant.question"] == questions


@pytest.mark.parametrize("interrupted", [False, True], ids=["failed", "interrupted"])
def test_terminal_conversation_clears_current_attachment_selection_question(store, interrupted):
    conversation = _conversation(store)
    message = store.submit_message(conversation, "first", "问题描述")
    store.set_message_status(conversation, message.message_id, "APPLIED")
    _project_waiting_questions(store, conversation, ["请上传日志。"], attachment_questions=["请上传日志。"])
    notice = {"requirement_id": "question-0", "message": "请重新选择一个日志归档。"}
    store.finish_intake(conversation, [message.message_id], attachment_notice=notice)
    assert store.get_conversation(conversation).current_questions == [notice["message"]]
    store.fail_conversation(conversation, interrupted=interrupted)
    view = store.get_conversation(conversation)
    assert view.status == ("INTERRUPTED" if interrupted else "FAILED")
    assert view.current_questions == []
    with store.repository.database_read() as database:
        assert store._load(database, conversation)["intake_attachment_notice"] is None
    events = store.list_events(conversation)
    store.finish_intake(conversation, [message.message_id], attachment_notice=notice)
    assert store.get_conversation(conversation).current_questions == []
    assert store.list_events(conversation) == events


def test_intake_coverage_and_question_publication_roll_back_together(store, monkeypatch):
    conversation = _conversation(store)
    first = store.submit_message(conversation, "first", "型号 X1")
    _project_waiting_questions(store, conversation, ["请上传日志。"])
    previous = _stored_conversation_body(store, conversation)
    events = store.list_events(conversation)
    append = store._append

    def fail_question(db, body, event_type, data, dedupe_key):
        result = append(db, body, event_type, data, dedupe_key)
        if event_type == "assistant.question":
            raise sqlite3.OperationalError("injected intake publication failure")
        return result

    monkeypatch.setattr(store, "_append", fail_question)
    with pytest.raises(sqlite3.OperationalError):
        store.finish_intake(conversation, [first.message_id])
    assert _stored_conversation_body(store, conversation) == previous
    assert store.list_events(conversation) == events
    assert store.get_intake_state(conversation)["pending"]


@pytest.mark.parametrize("terminal", ["closed", "report"])
def test_late_intake_finish_cannot_overwrite_closed_or_report_ready_conversation(store, terminal):
    conversation = _conversation(store)
    first = store.submit_message(conversation, "first", "型号 X1")
    store.record_dispatch(conversation, "pending", {"operation": "CreateCase"})
    if terminal == "closed":
        store.fail_conversation(conversation)
    else:
        with store.repository.database_transaction() as db:
            body = store._load(db, conversation)
            body.update(status="RUNNING", report_available=True)
            store._save(db, body)
    previous = _stored_conversation_body(store, conversation)
    events = store.list_events(conversation)
    assert store.get_intake_state(conversation) == {
        "pending": False, "covered_message_ids": [], "pending_commands": False, "ready": False}
    store.finish_intake(conversation, [first.message_id], questions=["不得覆盖终态。"])
    assert _stored_conversation_body(store, conversation) == previous
    assert store.list_events(conversation) == events


def test_intake_state_read_is_private_read_only_and_safe_for_legacy_body(store):
    conversation = _conversation(store)
    with store.repository.database_transaction() as db:
        body = store._load(db, conversation)
        body.pop("intake_pending")
        body.pop("intake_covered_message_ids")
        store._save(db, body)
    previous = _stored_conversation_body(store, conversation)
    assert store.get_intake_state(conversation) == {
        "pending": False, "covered_message_ids": [], "pending_commands": False, "ready": True}
    assert _stored_conversation_body(store, conversation) == previous
    assert store.list_events(conversation) == []


def test_finished_intake_preserves_pending_command_discovery_with_index(store):
    conversation = _conversation(store)
    message = store.submit_message(conversation, "first", "型号 X1")
    store.record_dispatch(conversation, "pending", {"operation": "SubmitSupplement"})
    store.finish_intake(conversation, [message.message_id])
    assert store.get_intake_state(conversation) == {
        "pending": False, "covered_message_ids": [message.message_id], "pending_commands": True, "ready": True}
    with store.repository.database_read() as db:
        indexes = {row[1] for row in db.execute("PRAGMA index_list('agent_dispatches')")}
        assert "agent_dispatches_conversation_status" in indexes
    store.complete_dispatch("pending", {})
    assert not store.get_intake_state(conversation)["pending_commands"]


def test_update_intake_keeps_gate_until_explicit_finish_and_custom_question(store):
    conversation = _conversation(store)
    first = store.submit_message(conversation, "first", "型号 X1")
    store.update_intake(conversation, {}, ["请描述需要定位的问题。"])
    assert store.get_intake_state(conversation)["pending"]
    assert store.get_conversation(conversation).current_questions == []
    store.finish_intake(conversation, [first.message_id], questions=["请描述需要定位的问题。"])
    assert store.get_conversation(conversation).current_questions == ["请描述需要定位的问题。"]
    assert not store.get_intake_state(conversation)["pending"]


def test_intake_coverage_rejects_foreign_messages_without_clearing_gate(store):
    conversation, other = _conversation(store), _conversation(store, "other")
    store.submit_message(conversation, "first", "型号 X1")
    foreign = store.submit_message(other, "other-first", "型号 Y2")
    previous = _stored_conversation_body(store, conversation)
    with pytest.raises(AgentStoreError, match="不属于本会话"):
        store.finish_intake(conversation, [foreign.message_id])
    assert _stored_conversation_body(store, conversation) == previous


def test_intake_ready_skips_running_cases_but_keeps_unknown_bound_case_queryable(store):
    conversation = _conversation(store)
    store.submit_message(conversation, "first", "型号 X1")
    store.bind_case(conversation, CASE_ID)
    assert store.get_intake_state(conversation)["ready"]
    _populate(store.repository)
    assert store.get_intake_state(conversation)["pending"]
    assert not store.get_intake_state(conversation)["ready"]
    _project_waiting_questions(store, conversation, ["请上传日志。"], revision=2)
    assert store.get_intake_state(conversation)["ready"]


def test_new_message_preserves_legacy_authoritative_questions_before_hiding_them(store):
    conversation = _conversation(store)
    _project_waiting_questions(store, conversation, ["请上传日志。"])
    with store.repository.database_transaction() as db:
        body = store._load(db, conversation)
        body.pop("intake_authoritative_questions")
        body.pop("intake_question_revision")
        body.pop("intake_pending")
        body.pop("intake_covered_message_ids")
        store._save(db, body)
    first = store.submit_message(conversation, "first", "暂时没有日志。")
    assert store.get_conversation(conversation).current_questions == []
    store.finish_intake(conversation, [first.message_id])
    assert store.get_conversation(conversation).current_questions == ["请上传日志。"]


def test_message_bytes_and_attachment_ownership_are_validated(store):
    conversation, other = _conversation(store), _conversation(store, "other")
    with pytest.raises(ValidationError):
        SendMessageRequest(request_id="r", text="中" * 22000)
    with pytest.raises(ValidationError):
        SendMessageRequest(request_id="r", text="  ")
    with pytest.raises(ValidationError):
        SendMessageRequest(request_id="r", text=42)
    upload = store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64)
    with pytest.raises(AgentStoreError, match="未上传完成"):
        store.submit_message(conversation, "early", attachment_ids=[upload.attachment_id])
    store.complete_attachment(upload.attachment_id, "/private/logs.zip")
    with pytest.raises(AgentStoreError, match="不属于本会话"):
        store.submit_message(other, "foreign", attachment_ids=[upload.attachment_id])
    store.submit_message(conversation, "ready", text=None, attachment_ids=[upload.attachment_id])
    assert "/private" not in store.get_conversation(conversation).model_dump_json()
    assert "/private" not in str([event.model_dump() for event in store.list_events(conversation)])


def test_attachment_reservation_and_import_are_idempotent_with_atomic_quota(store):
    conversation = _conversation(store)
    first = store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64, max_total_bytes=15)
    assert store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64) == first
    with pytest.raises(AgentStoreError, match="内容不能更改"):
        store.reserve_attachment(conversation, "upload", "other.zip", "application/zip", 10, "a" * 64)
    with pytest.raises(AgentStoreError, match="超出"):
        store.reserve_attachment(conversation, "next", "next.zip", "application/zip", 10, "b" * 64, max_total_bytes=15)
    store.complete_attachment(first.attachment_id)
    imported = store.bind_attachment(first.attachment_id, "case-upload")
    assert store.bind_attachment(first.attachment_id, "case-upload") == imported
    with pytest.raises(AgentStoreError):
        store.bind_attachment(first.attachment_id, "different-upload")


def test_explicit_upload_retry_publishes_new_transitions_but_duplicate_does_not(store):
    conversation = _conversation(store)
    upload = store.reserve_attachment(conversation, "upload", "logs.zip", "application/zip", 10, "a" * 64)
    store.set_attachment_status(upload.attachment_id, "UPLOADING")
    store.set_attachment_status(upload.attachment_id, "FAILED")
    store.set_attachment_status(upload.attachment_id, "UPLOADING")
    store.set_attachment_status(upload.attachment_id, "UPLOADING")
    assert [event.data["status"] for event in store.list_events(conversation)] == ["RESERVED", "UPLOADING", "FAILED", "UPLOADING"]


def test_progress_is_allowlisted_deduplicated_and_does_not_change_case_revision(store):
    conversation = _conversation(store)
    store.bind_case(conversation, CASE_ID)
    _populate(store.repository)
    first = store.append_case_progress(CASE_ID, JOB_ID, "VERIFYING")
    assert store.append_case_progress(CASE_ID, JOB_ID, "VERIFYING") == first
    assert store.repository.read_case(CASE_ID).case.case_revision == 1
    assert first.data == {"stage": "VERIFYING", "message": "正在核对证据"}
    assert first.job_id == JOB_ID
    assert store.append_case_progress("unbound", JOB_ID, "VERIFYING") is None
    with pytest.raises(ValueError):
        store.append_event(conversation, "agent.progress", {"stage": "VERIFYING", "message": "secret path"}, dedupe_key="secret")
    with pytest.raises(ValidationError):
        AgentEvent(sequence=1, conversation_id=conversation, type="case.updated", created_at="now",
                   data={"status": "RUNNING", "case_revision": 1, "storage_key": "secret"})


def test_expected_create_binding_exists_before_core_commit_returns(store):
    conversation = _conversation(store)
    store.expect_case(conversation, "core-create")
    from pathlib import Path
    from problem_locator.contracts import StateFile
    state = StateFile.model_validate_json(Path("tests/fixtures/contracts/positive/state.json").read_bytes())
    aggregate = state.cases[CASE_ID]
    record = IdempotencyRecord(operation="CreateCase", idempotency_key="core-create", request_hash="a" * 64,
        case_id=CASE_ID, created_at=aggregate.case.created_at, business_receipt={"operation": "CreateCase",
                "case_id": CASE_ID, "primary_resource_id": CASE_ID, "case_revision": 1, "job_id": JOB_ID, "status": "RUNNING"})
    store.repository.commit(1, None, _empty_mutation(upsert_case=aggregate.case,
        insert_jobs=list(aggregate.jobs.values()), insert_idempotency_records=[record]))
    view = store.get_conversation(conversation)
    assert (view.case_id, view.job_id, view.case_status) == (CASE_ID, JOB_ID, "RUNNING")
    assert store.list_events(conversation)[0].type == "case.updated"


def _create_with_adopting_message(store):
    from pathlib import Path
    from problem_locator.contracts import StateFile
    conversation = _conversation(store)
    receipt = store.submit_message(conversation, "message", "用于本次诊断的问题描述")
    store.set_message_status(conversation, receipt.message_id, "PROCESSING")
    store.expect_case(conversation, "core-create")
    store.record_dispatch(conversation, "create-dispatch", {"operation": "CreateCase",
        "command": {"idempotency_key": "core-create"}})
    store.begin_adoption(conversation, receipt.message_id, "create-dispatch")
    state = StateFile.model_validate_json(Path("tests/fixtures/contracts/positive/state.json").read_bytes())
    aggregate = state.cases[CASE_ID]
    record = IdempotencyRecord(operation="CreateCase", idempotency_key="core-create", request_hash="a" * 64,
        case_id=CASE_ID, created_at=aggregate.case.created_at, business_receipt={"operation": "CreateCase",
            "case_id": CASE_ID, "primary_resource_id": CASE_ID, "case_revision": 1,
            "job_id": JOB_ID, "status": "RUNNING"})
    mutation = _empty_mutation(upsert_case=aggregate.case, insert_jobs=list(aggregate.jobs.values()),
                               insert_idempotency_records=[record])
    return conversation, receipt.message_id, mutation


def test_accepted_command_adopts_message_in_case_transaction_before_fast_terminal(store):
    conversation, message_id, mutation = _create_with_adopting_message(store)
    store.repository.commit(1, None, mutation)
    _finish(store.repository)
    # The core scheduler completed before the website worker received its
    # command response. The committed CreateCase receipt already proves use.
    assert store.get_conversation(conversation).messages[0].status == "APPLIED"
    before = store.list_events(conversation)
    store.finish_adoption(conversation, message_id, accepted=True)
    store.finish_adoption(conversation, message_id, accepted=True)
    assert store.list_events(conversation) == before
    applied = next(event.sequence for event in before if event.type == "message.updated" and event.data["status"] == "APPLIED")
    assert applied < next(event.sequence for event in before if event.type == "conversation.completed")


def test_failed_core_commit_rolls_back_message_adoption_with_case_state(store, monkeypatch):
    conversation, message_id, mutation = _create_with_adopting_message(store)
    before = store.list_events(conversation)
    original = store._project_case

    def fail_after_projection(db, body, aggregate):
        original(db, body, aggregate)
        raise sqlite3.OperationalError("injected atomic adoption failure")

    monkeypatch.setattr(store, "_project_case", fail_after_projection)
    with pytest.raises(Exception):
        store.repository.commit(1, None, mutation)
    assert store.get_conversation(conversation).messages[0].status == "PROCESSING"
    assert store.list_events(conversation) == before
    store.finish_adoption(conversation, message_id, accepted=False)
    assert store.get_conversation(conversation).messages[0].status == "UNUSED"


def test_old_task_terminal_does_not_adopt_unaccepted_concurrent_supplement(store):
    conversation = _conversation(store)
    store.bind_case(conversation, CASE_ID)
    _populate(store.repository)
    receipt = store.submit_message(conversation, "late", "尚未接纳的补充")
    store.set_message_status(conversation, receipt.message_id, "PROCESSING")
    store.record_dispatch(conversation, "supplement", {"operation": "SubmitSupplement",
        "command": {"idempotency_key": "late-supplement", "case_id": CASE_ID}})
    store.begin_adoption(conversation, receipt.message_id, "supplement")
    _finish(store.repository)
    assert store.get_conversation(conversation).messages[0].status == "UNUSED"
    store.finish_adoption(conversation, receipt.message_id, accepted=False)
    assert store.get_conversation(conversation).messages[0].status == "UNUSED"


def test_restart_clears_unaccepted_adoption_without_replaying_command(store):
    conversation, message_id, _ = _create_with_adopting_message(store)
    reopened = _open(store.repository.layout.data_root)
    try:
        second = AgentStore(reopened, runtime_epoch="epoch-two")
        second.recover()
        assert second.get_conversation(conversation).status == "INTERRUPTED"
        assert second.get_conversation(conversation).messages[0].status == "UNUSED"
        assert second.pending_dispatches(conversation) == []
        with reopened.database_read() as db:
            assert db.execute("SELECT count(*) FROM agent_message_adoptions").fetchone()[0] == 0
        second.finish_adoption(conversation, message_id, accepted=False)
    finally:
        reopened.close()


def test_terminal_case_snapshot_and_public_events_roll_back_together(store, monkeypatch):
    conversation = _conversation(store)
    store.bind_case(conversation, CASE_ID)
    _populate(store.repository)
    before = store.get_conversation(conversation)
    original = store._project_case

    def fail_after_project(db, body, aggregate):
        original(db, body, aggregate)
        raise sqlite3.OperationalError("injected publication failure")

    monkeypatch.setattr(store, "_project_case", fail_after_project)
    with pytest.raises(Exception):
        _finish(store.repository)
    assert store.get_conversation(conversation) == before
    with store.repository.database_read() as db:
        assert db.execute("SELECT count(*) FROM completed_cases").fetchone()[0] == 0


def test_safe_failure_never_exposes_internal_failure_and_marks_queued_unused(store):
    conversation = _conversation(store)
    store.submit_message(conversation, "message", "问题描述")
    store.bind_case(conversation, CASE_ID)
    _populate(store.repository)
    _finish(store.repository)
    view = store.get_conversation(conversation)
    assert view.status == "FAILED"
    assert view.messages[0].status == "UNUSED"
    assert store.list_events(conversation)[-1].type == "conversation.completed"
    store.fail_conversation(conversation, code="secret-token")
    assert "secret-token" not in str([event.model_dump() for event in store.list_events(conversation)])


def test_terminal_failure_snapshot_and_events_commit_atomically(store, monkeypatch):
    conversation = _conversation(store)
    store.submit_message(conversation, "one", "问题描述")
    before = store.get_conversation(conversation)
    events_before = store.list_events(conversation)
    append = store._append

    def fail_after_event(db, body, event_type, data, dedupe_key):
        result = append(db, body, event_type, data, dedupe_key)
        if event_type == "conversation.completed":
            raise sqlite3.OperationalError("injected transaction failure")
        return result

    monkeypatch.setattr(store, "_append", fail_after_event)
    with pytest.raises(sqlite3.OperationalError):
        store.fail_conversation(conversation, "INTAKE_OUTPUT_INVALID", phase="INTAKE")
    assert store.get_conversation(conversation) == before
    assert store.list_events(conversation) == events_before
    monkeypatch.setattr(store, "_append", append)
    store.fail_conversation(conversation, "INTAKE_OUTPUT_INVALID", phase="INTAKE")
    failure = store.get_conversation(conversation).failure
    assert failure is not None and failure.retryable is False
    store.fail_conversation(conversation, "secret-token", phase="private/path")
    assert store.get_conversation(conversation).failure == failure


def test_restart_interrupts_started_conversations_once_and_preserves_empty_drafts(store):
    started, empty = _conversation(store), _conversation(store, "empty")
    store.submit_message(started, "message", "任务输入")
    store.record_dispatch(started, "dispatch", {"operation": "intake", "internal_path": "/secret"})
    store.set_draft(started, {"internal_draft": "source-backed fact"})
    assert "internal_draft" not in store.get_conversation(started).model_dump_json()
    reopened = _open(store.repository.layout.data_root)
    try:
        second = AgentStore(reopened, runtime_epoch="epoch-two")
        second.recover()
        assert second.get_conversation(started).status == "INTERRUPTED"
        assert second.get_conversation(empty).status == "INTAKE"
        assert second.get_dispatch(started, "dispatch")["status"] == "INTERRUPTED"
        events = second.list_events(started)
        second.recover()
        assert second.list_events(started) == events
        with pytest.raises(AgentStoreError, match="重启中断"):
            second.record_dispatch(started, "dispatch", {"operation": "intake", "internal_path": "/secret"})
        assert "/secret" not in str([event.model_dump() for event in events])
        second.submit_message(empty, "new", "重启后首条消息")
    finally:
        reopened.close()


def test_restart_keeps_durably_completed_case_failure_and_never_reopens_dispatch(store):
    conversation = _conversation(store)
    store.submit_message(conversation, "message", "任务输入")
    store.bind_case(conversation, CASE_ID)
    _populate(store.repository)
    _finish(store.repository)
    before = store.list_events(conversation)
    reopened = _open(store.repository.layout.data_root)
    try:
        second = AgentStore(reopened, runtime_epoch="epoch-two")
        second.recover()
        assert second.get_conversation(conversation).status == "FAILED"
        assert second.list_events(conversation) == before
        assert not any(event.type == "conversation.interrupted" for event in before)
    finally:
        reopened.close()


def test_case_failure_projects_typed_evidence_location_without_rejected_values(store):
    conversation = _conversation(store)
    store.bind_case(conversation, CASE_ID)
    aggregate = _populate(store.repository)
    diagnostic = "50000000-0000-4000-8000-000000000001"
    execution_failure = ExecutionFailure(stage="OUTCOME_VALIDATE", code="OUTCOME_INVALID", message="SECRET raw evidence",
        retryable=False, diagnostic_id=diagnostic, reason_code="METHOD_VALIDATION_FAILED", details=[ApplicationErrorDetail(field="findings[1].evidence_refs[2]",
            actual="SECRET rejected log", expected="SECRET expected log", resource_type=None, resource_id=None,
            resource_ref=None, limit=None, observed=None)])
    case = aggregate.case.model_copy(update={"status": CaseStatus.FAILED, "active_job_id": None, "case_revision": 2,
        "failure": SimpleNamespace(source_outcome_id="failure-outcome", diagnostic_id=diagnostic, reason_code=execution_failure.reason_code,
            code=execution_failure.code)})
    failed = aggregate.model_copy(update={"case": case, "outcomes": {"failure-outcome": SimpleNamespace(error=execution_failure)}})
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, conversation), failed)
    snapshot = store.get_conversation(conversation)
    assert snapshot.status == "FAILED"
    assert snapshot.failure.details == [{"field": "phase", "actual": "OUTCOME_VALIDATE"},
        {"field": "diagnostic_id", "actual": diagnostic}, {"field": "reason_code", "actual": "METHOD_VALIDATION_FAILED"},
        {"field": "location", "actual": "findings[1].evidence_refs[2]"}]
    assert "SECRET" not in snapshot.model_dump_json()


def test_notification_failure_cannot_reject_a_committed_message(store):
    conversation = _conversation(store)
    store.on_change = lambda _: (_ for _ in ()).throw(RuntimeError("subscriber failed"))
    receipt = store.submit_message(conversation, "message", "任务输入")
    assert store.get_conversation(conversation).messages[0].message_id == receipt.message_id
    assert store.list_events(conversation)[0].sequence == receipt.event_id


def test_dispatch_payload_is_frozen_and_not_exposed(store):
    conversation = _conversation(store)
    record = store.record_dispatch(conversation, "dispatch", {"revision": 1})
    assert store.record_dispatch(conversation, "dispatch", {"revision": 1}) == record
    assert store.pending_dispatches(conversation) == [record]
    with pytest.raises(AgentStoreError):
        store.record_dispatch(conversation, "dispatch", {"revision": 2})
    store.complete_dispatch("dispatch", {"accepted": True})
    assert store.pending_dispatches(conversation) == []
    assert store.get_dispatch(conversation, "dispatch")["result"] == {"accepted": True}
    assert "revision" not in store.get_conversation(conversation).model_dump_json()


@pytest.mark.parametrize("archive_outcome", ["READY", "FAILED"])
def test_report_is_published_before_archive_and_completion_waits(store, monkeypatch, archive_outcome):
    conversation = _conversation(store)
    store.submit_message(conversation, "queued", "运行时到达的补充")
    processing = store.submit_message(conversation, "processing", "尚未采纳的消息")
    store.set_message_status(conversation, processing.message_id, "PROCESSING")
    store.bind_case(conversation, CASE_ID)
    aggregate = _populate(store.repository)
    report = SimpleNamespace(kind=SimpleNamespace(value="USER_RESULT"), model_dump=lambda **kw: _artifact_data())
    monkeypatch.setattr("problem_locator.agent.store.project_artifact_summaries", lambda *args, **kwargs: [report])
    # This unit targets the transaction projection, independently of the report
    # renderer and archive worker exercised by integration/test_async_archive.
    pending_case = aggregate.case.model_copy(update={"status": CaseStatus.RESOLVED,
        "active_job_id": None, "case_revision": 2, "archive_status": "PENDING"})
    pending = aggregate.model_copy(update={"case": pending_case})
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, conversation), pending)
    assert store.get_conversation(conversation).status == "RUNNING"
    assert {message.status for message in store.get_conversation(conversation).messages} == {"UNUSED"}
    assert "result.available" in [event.type for event in store.list_events(conversation)]
    assert "conversation.completed" not in [event.type for event in store.list_events(conversation)]
    with pytest.raises(AgentStoreError, match="另建任务"):
        store.submit_message(conversation, "too-late", "已交付报告后的追问")
    ready = pending.model_copy(update={"case": pending_case.model_copy(update={"case_revision": 3, "archive_status": archive_outcome})})
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, conversation), ready)
        store._project_case(db, store._load(db, conversation), ready)
    events = store.list_events(conversation)
    assert len([event for event in events if event.type == "result.available"]) == 1
    assert len([event for event in events if event.type == "conversation.completed"]) == 1
    assert store.get_conversation(conversation).status == "COMPLETED"
    assert events[-1].type == "conversation.completed"


def test_generic_v1_available_event_references_authoritative_case_without_artifact(store):
    conversation = _conversation(store)
    store.bind_case(conversation, CASE_ID)
    aggregate = _populate(store.repository)
    result = GenericResult(status="RESOLVED", conclusion="已确认请求超时。",
        root_cause_analysis="服务端处理耗时超过请求期限。", skill_name="generic-locator",
        source_job_id=JOB_ID, source_outcome_id="00000000-0000-0000-0000-000000000020",
        occurred_at=aggregate.case.created_at)
    generic_case = aggregate.case.model_copy(update={"status": CaseStatus.RESOLVED,
        "active_job_id": None, "case_revision": 2, "generic_result": result})
    with store.repository.database_transaction() as db:
        store._project_case(db, store._load(db, conversation), aggregate.model_copy(update={"case": generic_case}))
    available = next(event for event in store.list_events(conversation) if event.type == "result.available")
    assert available.case_id == CASE_ID
    assert available.data == {"status": "RESOLVED", "artifacts": [], "result_field": "generic_result"}
    assert store.get_conversation(conversation).status == "COMPLETED"


def test_restart_pending_archive_preserves_report_and_completes_only_after_archive(tmp_path_factory):
    from problem_locator.contracts import canonical_json_bytes
    from problem_locator.dispatch.archive import ArchiveService
    from problem_locator.storage.state_repository import CaseStateRepository
    from tests.deterministic.contracts.fakes import InMemoryStateChangeNotifier
    from tests.deterministic.integration.test_async_archive import create_pending_archive
    from tests.deterministic.journey.test_rpc_timeout import _windows_extended_path

    # Uses the existing deterministic runtime/logparse executors, not a model.
    # A short test-owned directory leaves room for immutable Windows resource
    # paths containing several UUIDs and temporary publication filenames.
    root = tmp_path_factory.mktemp("ar") / "d"
    if os.name == "nt":
        root = _windows_extended_path(root)
    stack, case_id = create_pending_archive(root)
    try:
        first = AgentStore(stack.repository, runtime_epoch="epoch-one")
        conversation = _conversation(first)
        first.submit_message(conversation, "queued", "晚到的补充")
        first.bind_case(conversation, case_id)
        snapshot = stack.repository.read_snapshot(case_id)
        assert snapshot.cases[case_id].case.status is CaseStatus.RESOLVED, snapshot.cases[case_id].case.failure
        with stack.repository.database_transaction() as db:
            first._project_state(db, snapshot)
        report_event = next(event for event in first.list_events(conversation) if event.type == "result.available")
        initial_bytes = canonical_json_bytes(snapshot)
        assert first.get_conversation(conversation).archive_status == "PENDING"
        stack.repository.close()
        stack.repository = CaseStateRepository(stack.data_root, stack.coordination_lock, stack.clock, stack.ids)
        second = AgentStore(stack.repository, runtime_epoch="epoch-two")
        second.recover()
        assert canonical_json_bytes(stack.repository.read_snapshot(case_id)) == initial_bytes
        assert second.get_conversation(conversation).status == "RUNNING"
        assert not any(event.type in {"conversation.interrupted", "conversation.completed"} for event in second.list_events(conversation))
        assert next(event for event in second.list_events(conversation) if event.type == "result.available") == report_event
        archive = ArchiveService(stack.repository, stack.resources, stack.publication_guard, InMemoryStateChangeNotifier(), stack.clock)
        assert archive.run_once()
        assert second.get_conversation(conversation).status == "COMPLETED"
        assert second.get_conversation(conversation).archive_status == "READY"
        assert len([event for event in second.list_events(conversation) if event.type == "result.available"]) == 1
    finally:
        stack.shutdown()
