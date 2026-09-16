"""A recoverable ROUTE explanation must reach durable website delivery once."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace

import pytest

from problem_locator.contracts import ApplicationError, ApplicationPortError, ErrorCode, ReviewPolicy
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.model_json import parse_model_json_bytes
from tests.deterministic.integration.test_website_agent import (
    PARAMETER_GROUP_A, _converse_to_waiting, _post, website,
)


def _inject_route_quotes(stack, monkeypatch):
    backend = stack.runtime._route_backend
    execute = backend.execute
    observed = {"phases": []}

    def quoted(**kwargs):
        result = execute(**kwargs)
        observed["phases"].append(kwargs.get("backend_phase"))
        if kwargs.get("backend_phase") != "ROUTE":
            return result
        value = json.loads(result.final_result)
        value["reason"] = '选择 "rpc_timeout"；保留路径 C:\\logs\\new 和字面量 \\n。'
        text = json.dumps(value, ensure_ascii=False).replace(r'\"rpc_timeout\"', '"rpc_timeout"')
        raw = "\ufeff```json\r\n" + text + "\r\n```"
        event = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                            "result": raw}, ensure_ascii=False).encode() + b"\r\n"
        stream = AgentStreamTelemetry()
        for offset in range(0, len(event), 7):
            stream.write(event[offset:offset + 7])
        assert stream.final_result == raw
        observed.update(raw=raw.encode(), expected=value)
        return replace(result, final_result=stream.final_result)

    monkeypatch.setattr(backend, "execute", quoted)
    return observed


@pytest.mark.parametrize("review", [False, True])
def test_recovered_route_reaches_report_archive_and_replay_once(website, monkeypatch, caplog, review):
    stack, store, engine, service, client = website
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    observed = _inject_route_quotes(stack, monkeypatch)
    caplog.set_level(logging.INFO, logger="problem_locator.dfx")
    conversation, prefix, _ = _converse_to_waiting(website)
    _post(client, prefix + "/messages", {"request_id": "message:2",
        "text": "\n".join(f"{name}={value}" for name, value in PARAMETER_GROUP_A.items())})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(20)
    view = service.get_conversation(conversation)
    assert view.case_status == "RESOLVED", view
    state = stack.repository.read_case(view.case_id)
    route = next(job for job in state.jobs.values() if job.job_type.value == "ROUTE")
    assert route.status.value == "SUCCEEDED"
    records = stack.runtime._execution_records
    raw = records.read_audit_bytes(route.job_id, "route-response.raw.txt")
    effective = records.read_audit_bytes(route.job_id, "route-response.effective.txt")
    receipt = json.loads(records.read_audit_bytes(route.job_id, "route-json-recovery.json"))
    assert raw == observed["raw"]
    assert receipt["case_id"] == view.case_id and receipt["job_id"] == route.job_id
    assert receipt["phase"] == "ROUTE" and receipt["diagnostic_id"]
    assert parse_model_json_bytes(effective).value == observed["expected"]
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["effective_sha256"] == hashlib.sha256(effective).hexdigest()
    assert receipt["raw_size_bytes"] == len(raw)
    assert receipt["effective_size_bytes"] == len(effective)
    reconstructed = bytearray(raw)
    for offset in reversed(receipt["inserted_escape_offsets"]):
        assert raw[offset:offset + 1] == b'"'
        reconstructed[offset:offset] = b"\\"
    assert bytes(reconstructed) == effective
    outcome = records.read_published_outcome(route.job_id).job_outcome
    assert outcome.payload.reason == observed["expected"]["reason"]
    assert outcome.payload.skill_ref.id == observed["expected"]["skill_id"]
    assert outcome.payload.confidence == observed["expected"]["confidence"]
    events = [record for record in caplog.records
              if getattr(record, "dfx_event", None) == "runtime.route.reason_quotes_recovered"]
    assert len(events) == 1
    fields = events[0].dfx_fields
    assert fields["diagnostic_id"] == receipt["diagnostic_id"]
    assert fields["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert fields["effective_response_sha256"] == hashlib.sha256(effective).hexdigest()
    assert fields["inserted_escape_count"] == 2
    assert "rpc_timeout" not in json.dumps(fields)

    artifacts = client.get(f"/api/v1/cases/{view.case_id}/artifacts").json()["data"]["artifacts"]
    report_ref = next(item for item in artifacts if item["kind"] == "USER_RESULT")
    report = client.get(report_ref["download_url"])
    assert report.status_code == 200
    assert hashlib.sha256(report.content).hexdigest() == report_ref["sha256"]
    assert stack.archive.run_once()
    assert service.get_conversation(conversation).archive_status == "READY"
    replay = client.get(prefix + "/events")
    assert b"result.available" in replay.content and b"conversation.completed" in replay.content
    assert client.get(prefix).json()["data"]["failure"] is None
    assert observed["phases"].count("ROUTE") == 1
    assert observed["phases"].count("METHODS_SPECIALIST") == 1
    assert observed["phases"].count("METHODS_REVIEWER") == int(review)
    assert len(engine.calls) == 1


@pytest.mark.parametrize("failed_file", [
    "route-response.raw.txt", "route-response.effective.txt", "route-json-recovery.json",
])
def test_route_recovery_cannot_succeed_without_durable_audit(website, monkeypatch, failed_file):
    stack, store, engine, service, client = website
    observed = _inject_route_quotes(stack, monkeypatch)
    records = stack.runtime._execution_records
    publish = records.publish_audit_bytes

    def fail_selected(job_id, filename, payload):
        if filename == failed_file:
            raise ApplicationPortError(ApplicationError(code=ErrorCode.EXECUTION_RECORD_FAILED,
                message="Injected recovery audit write failure.", details=[], retryable=True))
        return publish(job_id, filename, payload)

    monkeypatch.setattr(records, "publish_audit_bytes", fail_selected)
    conversation = _post(client, "/api/v1/agent/conversations", {"request_id": "create:1"})["conversation_id"]
    prefix = f"/api/v1/agent/conversations/{conversation}"
    _post(client, prefix + "/messages", {"request_id": "message:1", "text": "RPC timeout"})
    assert service.run_once(conversation)
    assert stack.scheduler.wait_until_idle(15)
    view = client.get(prefix).json()["data"]
    assert view["case_status"] == "INTERRUPTED"
    assert view["status"] == "INTERRUPTED"
    assert view["failure"]["code"] == "EXECUTION_RECORD_FAILED"
    assert view["failure"]["retryable"] is False
    assert {item["field"]: item["actual"] for item in view["failure"]["details"]}["phase"] == "EXECUTION_RECORD"
    assert client.get(prefix).json()["data"]["failure"] == view["failure"]
    assert b"conversation.interrupted" in client.get(prefix + "/events").content
    assert observed["phases"] == ["ROUTE"] and engine.calls == []
    assert not any(event.type == "result.available" for event in store.list_events(conversation))
