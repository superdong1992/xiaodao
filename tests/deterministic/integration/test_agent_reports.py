"""Native report/status endpoints cross real Agent, query, publication and SSE paths."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace

import pytest

from problem_locator.contracts import ErrorCode, ReviewPolicy, canonical_json_bytes
from problem_locator.operational import OperationalState
from tests.deterministic.integration.test_website_agent import (
    PARAMETER_GROUP_A, _converse_to_waiting, _post, website,
)
from tests.deterministic.unit.application.test_reports import generic_report


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True and response.json()["error"] is None
    return response.json()["data"]


def _finish_diagnosis(website):
    stack, store, engine, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    before = _get(client, prefix + "/report")
    assert before["report_state"] == "PENDING" and before["report"] is None
    assert _get(client, prefix + "/status")["report_state"] == "PENDING"
    _post(client, prefix + "/messages", {"request_id": "answer",
        "text": "\n".join(f"{key}={value}" for key, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(25)
    return conversation, prefix


@pytest.mark.parametrize("result_status", ["COMPLETED", "PARTIAL", "INCONCLUSIVE"])
def test_native_report_reads_all_published_diagnosis_results_and_same_raw_artifact(
    website, monkeypatch, result_status,
):
    stack, store, engine, service, client = website
    stack.runtime._methods_evidence_validation = "advisory" if result_status == "PARTIAL" else "strict"
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT
    if result_status == "INCONCLUSIVE":
        backend = stack.runtime._diagnose_backend
        execute = backend.execute

        def reject_review(**kwargs):
            result = execute(**kwargs)
            if kwargs.get("backend_phase") == "METHODS_REVIEWER":
                path = kwargs["workspace_root"] / "output" / "method-review.draft.json"
                draft = json.loads(path.read_bytes())
                draft["verdict"] = "NEED_MORE_EVIDENCE"
                for item in draft["findings"]:
                    item.update(verdict="NEED_MORE_EVIDENCE", reason="现有证据不足以接受该结论。")
                path.write_bytes(canonical_json_bytes(draft))
            return result

        monkeypatch.setattr(backend, "execute", reject_review)
    conversation, prefix = _finish_diagnosis(website)
    result = _get(client, prefix + "/report")
    assert result["report_state"] == "READY"
    assert result["format"] == "problem-locator-diagnosis-v3"
    assert result["report"]["status"] == result_status and result["markdown"] is None
    assert result["conversation_id"] == conversation
    assert result["case_status"] == {"COMPLETED": "RESOLVED", "PARTIAL": "PARTIALLY_RESOLVED",
        "INCONCLUSIVE": "UNRESOLVED"}[result_status]
    artifact = result["artifact"]
    assert artifact["created_by_job_id"] == result["source_job_id"]
    assert "storage_key" not in artifact and "download_url" not in artifact
    raw = client.get(f'/api/v1/artifacts/{artifact["artifact_id"]}/content', params={"case_id": result["case_id"]})
    assert raw.status_code == 200 and raw.json() == result["report"]
    assert len(raw.content) == artifact["size"] and hashlib.sha256(raw.content).hexdigest() == artifact["sha256"]
    assert _get(client, prefix + "/status")["report_state"] == "READY"
    if result_status != "INCONCLUSIVE":
        assert result["archive_status"] == "PENDING"
        assert stack.archive.run_once()
    replay = client.get(prefix + "/events")
    assert replay.status_code == 200
    frames = [json.loads(frame[6:]) for frame in replay.content.split(b"\n\n") if frame.startswith(b"data: ")]
    assert any(item["type"] == "result.available" for item in frames)
    assert frames[-1]["type"] == "conversation.completed"
    assert _get(client, prefix + "/report")["report"] == result["report"]
    assert len(engine.calls) == 1


def test_pending_and_failed_conversation_are_business_states_not_http_errors(website):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("report-state").conversation_id
    prefix = f"/api/v1/agent/conversations/{conversation}"
    for suffix in ("/report", "/status"):
        data = _get(client, prefix + suffix)
        assert data["report_state"] == "PENDING" and data["case_id"] is None
        assert "messages" not in data and "attachments" not in data
    store.fail_conversation(conversation, "INTAKE_EXECUTION_FAILED", phase="INTAKE")
    for suffix in ("/report", "/status"):
        data = _get(client, prefix + suffix)
        assert data["report_state"] == "UNAVAILABLE"
        assert data["failure"]["code"] == "INTAKE_EXECUTION_FAILED"
        assert data["failure"]["retryable"] is False
    assert engine.calls == []
    for suffix in ("/report", "/status"):
        response = client.get(f"/api/v1/agent/conversations/{uuid.uuid4()}{suffix}")
        assert response.status_code == 404 and response.json()["ok"] is False


@pytest.mark.parametrize("archive_state", ["PENDING", "FAILED", "UNCERTAIN"])
def test_archive_delays_and_failures_keep_native_report_readable(website, monkeypatch, archive_state):
    stack, store, engine, service, client = website
    conversation, prefix = _finish_diagnosis(website)
    original = _get(client, prefix + "/report")
    if archive_state == "FAILED":
        def fail_archive(*args, **kwargs):
            raise OSError("private disk path must not become a public message")
        monkeypatch.setattr(stack.resources, "stage_archive", fail_archive)
        assert stack.archive.run_once()
    elif archive_state == "UNCERTAIN":
        operational = OperationalState()
        service.application = replace(stack.application, operational_state=operational)
        stack.application.queries._operational = operational
        stack.archive.operational_state = operational
        attempted = []

        def fail_status(case_id, status, artifact=None):
            attempted.append(status)
            raise RuntimeError("SECRET /private/path failed persistence")

        monkeypatch.setattr(stack.archive, "_set_status", fail_status)
        assert stack.archive.run_once()
        assert attempted == ["READY", "FAILED"]
    persisted = store.get_conversation(conversation)
    events = store.list_events(conversation, limit=500)
    snapshot_calls = []
    read_snapshot = stack.repository.read_snapshot

    def record_snapshot(*args, **kwargs):
        snapshot_calls.append((args, kwargs))
        return read_snapshot(*args, **kwargs)

    monkeypatch.setattr(stack.repository, "read_snapshot", record_snapshot)
    result = _get(client, prefix + "/report")
    assert len(snapshot_calls) == 1
    assert result["report_state"] == "READY" and result["report"] == original["report"]
    assert result["artifact"] == original["artifact"]
    assert result["archive_status"] == ("FAILED" if archive_state == "FAILED" else "PENDING")
    if archive_state == "UNCERTAIN":
        assert result["failure"]["retryable"] is False
        assert result["failure"]["message"] == "报告已生成，但归档状态暂时无法确认。"
        assert "SECRET" not in json.dumps(result)
        assert persisted.failure is None
        assert not any(item.type == "conversation.completed" for item in events)
    assert store.get_conversation(conversation) == persisted and store.list_events(conversation, limit=500) == events


def test_status_report_and_sse_are_read_only_and_never_load_message_or_attachment_history(website, monkeypatch):
    stack, store, engine, service, client = website
    conversation, prefix = _finish_diagnosis(website)
    assert stack.archive.run_once()
    before = store.get_conversation(conversation)
    assert before.status == "COMPLETED" and before.messages and before.attachments
    model_record = stack.data_root.parent / "agent.jsonl"
    model_bytes = model_record.read_bytes()
    snapshot_calls, statements = [], []
    read_snapshot = stack.repository.read_snapshot

    def record_snapshot(*args, **kwargs):
        snapshot_calls.append((args, kwargs))
        return read_snapshot(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("只读状态、报告及 SSE 不得加载历史、写入或启动模型。")

    with monkeypatch.context() as patch:
        patch.setattr(stack.repository, "read_snapshot", record_snapshot)
        patch.setattr(stack.repository, "database_transaction", forbidden)
        patch.setattr(stack.repository, "commit", forbidden)
        patch.setattr(store, "get_conversation", forbidden)
        for backend in {id(item): item for item in (stack.runtime._route_backend, stack.runtime._diagnose_backend)}.values():
            patch.setattr(backend, "execute", forbidden)
        patch.setattr(engine, "intake", forbidden)
        with stack.repository.database_read() as db:
            db.set_trace_callback(statements.append)
        try:
            status = _get(client, prefix + "/status")
            assert status["last_event_id"] == before.last_event_id
            assert "messages" not in status and "attachments" not in status
            assert snapshot_calls == []
            report = _get(client, prefix + "/report")
            assert _get(client, prefix + "/report") == report
            assert len(snapshot_calls) == 2
            replay = client.get(prefix + "/events")
            assert replay.status_code == 200 and "conversation.completed" in replay.text
            assert len(snapshot_calls) == 2
        finally:
            with stack.repository.database_read() as db:
                db.set_trace_callback(None)
    assert not any("agent_messages" in statement.lower() or "agent_attachments" in statement.lower() for statement in statements)
    assert not any(statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ", "BEGIN IMMEDIATE")) for statement in statements)
    assert model_record.read_bytes() == model_bytes and store.get_conversation(conversation) == before


@pytest.mark.parametrize("version", [1, 2])
def test_native_report_projects_historical_generic_versions_without_rewriting_artifacts(website, monkeypatch, version):
    stack, store, engine, service, client = website
    aggregate, resources, generic = generic_report(version)
    conversation = service.create_conversation(f"generic-history-{version}").conversation_id
    store.bind_case(conversation, aggregate.case.case_id)
    # Historical snapshots enter through the same repository port as restored
    # SQLite data; no model execution or special HTTP-only report fixture.
    from problem_locator.contracts import StateFile
    snapshot = StateFile.model_construct(cases={aggregate.case.case_id: aggregate})
    calls = []

    def read_snapshot(case_id):
        calls.append(case_id)
        return snapshot

    monkeypatch.setattr(stack.repository, "read_snapshot", read_snapshot)
    monkeypatch.setattr(stack.application.queries, "_resource_store", resources)
    prefix = f"/api/v1/agent/conversations/{conversation}"
    data = _get(client, prefix + "/report")
    assert calls == [aggregate.case.case_id] and data["report_state"] == "READY"
    assert data["source_job_id"] == generic.source_job_id
    if version == 1:
        assert data["format"] == "generic-v1" and data["report"] == generic.model_dump(mode="json")
        assert data["artifact"] is data["markdown"] is None and resources.opened == []
    else:
        assert data["format"] == "markdown" and data["markdown"].encode() == resources.content
        assert data["artifact"]["sha256"] == hashlib.sha256(resources.content).hexdigest()
        assert data["report"] is None and len(resources.opened) == 1
    assert engine.calls == []


def test_native_report_preserves_delivery_uncertainty_until_authoritative_result_exists(website):
    stack, store, engine, service, client = website
    conversation, prefix, _ = _converse_to_waiting(website)
    before = store.get_conversation(conversation)
    operational = OperationalState()
    operational.record(case_id=before.case_id, job_id=str(uuid.uuid4()), phase="RESULT_DELIVERY",
        error_code=ErrorCode.STATE_WRITE_FAILED)
    service.application = replace(stack.application, operational_state=operational)
    stack.application.queries._operational = operational
    for suffix in ("/status", "/report"):
        response = client.get(prefix + suffix)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "DISPATCH_REJECTED"
        assert "最终状态暂时无法确认" in response.json()["error"]["message"]
    assert store.get_conversation(conversation) == before
    assert not any(item.type == "conversation.completed" for item in store.list_events(conversation))


def test_interrupted_history_without_volatile_case_returns_unavailable_and_original_failure(website):
    stack, store, engine, service, client = website
    conversation = service.create_conversation("interrupted-history").conversation_id
    service.send_message(conversation, "accepted-before-restart", "重启前接收的问题。")
    missing_case = str(uuid.uuid4())
    store.bind_case(conversation, missing_case)
    store.runtime_epoch = "restarted-report-epoch"
    store.recover()
    before = store.get_conversation(conversation)
    assert before.status == "INTERRUPTED" and before.failure is not None
    prefix = f"/api/v1/agent/conversations/{conversation}"
    for suffix in ("/status", "/report"):
        result = _get(client, prefix + suffix)
        assert result["report_state"] == "UNAVAILABLE" and result["case_id"] == missing_case
        assert result["failure"] == before.failure.model_dump(mode="json")
    assert _get(client, prefix + "/report")["report"] is None
    assert store.get_conversation(conversation) == before and engine.calls == []


def test_missing_case_cannot_hide_corruption_of_an_already_ready_report(website, monkeypatch):
    stack, store, engine, service, client = website
    conversation, prefix = _finish_diagnosis(website)
    assert stack.archive.run_once()
    assert _get(client, prefix + "/status")["report_state"] == "READY"
    from problem_locator.contracts import StateFile
    monkeypatch.setattr(stack.repository, "read_snapshot", lambda *args, **kwargs: StateFile.model_construct(cases={}))
    response = client.get(prefix + "/report")
    assert response.status_code == 404 and response.json()["error"]["code"] == "CASE_NOT_FOUND"
    assert store.get_status(conversation).report_state == "READY"
