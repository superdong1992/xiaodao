from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from problem_locator.agent.models import AgentEvent, AgentStoreError, ConversationDetail, ConversationReportView
from problem_locator.interfaces import agent_http
from problem_locator.interfaces.error_mapping import error_envelope, validation_error_from
from fastapi.responses import JSONResponse


CONVERSATION = "10000000-0000-0000-0000-000000000001"
ATTACHMENT = "20000000-0000-0000-0000-000000000001"
OWNER = "a" * 64
RUN = "50000000-0000-0000-0000-000000000001"
CASE = "30000000-0000-0000-0000-000000000001"
JOB = "40000000-0000-0000-0000-000000000001"
WHEN = "2026-09-07T08:00:00.000Z"
BASE = "/api/v1/agent"
VIEW = f"{BASE}/conversations/{CONVERSATION}"
PAYLOAD = b"test archive bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
INCLUDES = ("history", "report", "artifacts")


def conversation_detail(result, include=INCLUDES, *, last_event_id=0):
    ready = result["report_state"] == "READY"
    artifact = result.get("artifact")
    return dict(
        schema_version=3, conversation_id=CONVERSATION, title="测试会话", selected_run_id=RUN,
        current_run=dict(run_id=RUN, ordinal=1, status="INTAKE", report_state="PENDING", created_at=WHEN, updated_at=WHEN),
        capabilities=dict(can_send=True, can_stop=True, can_rediagnose=False, can_rename=True, can_delete=True),
        status="COMPLETED" if ready else "FAILED" if result["report_state"] == "UNAVAILABLE" else "INTAKE",
        case_id=result.get("case_id"), job_id=result.get("source_job_id"),
        case_status=result.get("case_status"), case_revision=result.get("case_revision"),
        source_job_id=result.get("source_job_id"), archive_status=result.get("archive_status", "NOT_REQUIRED"),
        current_questions=[], failure=result.get("failure"), progress=None, report_state=result["report_state"],
        included=list(include), history=[] if "history" in include else None,
        attachments=[] if "history" in include else None,
        result=result if "report" in include else None,
        artifacts=([{**artifact, "download_url": None}] if artifact else []) if "artifacts" in include else None,
        last_event_id=last_event_id, created_at=WHEN, updated_at=WHEN,
    )


def event(sequence, kind="agent.progress"):
    data = {"stage": "DIAGNOSE", "message": "正在分析日志"}
    if kind == "archive.updated":
        data = {"status": "READY", "artifacts": []}
    return AgentEvent(
        schema_version=2, run_id=RUN, sequence=sequence, conversation_id=CONVERSATION, case_id=CASE,
        type=kind, created_at=WHEN, data=data,
    )


class FakeAgent:
    def __init__(self, events=None):
        self.events = events or []
        self.calls = []
        self.thread_ids = []
        self.closed = True
        self.report = dict(schema_version=1, conversation_id=CONVERSATION, report_state="PENDING")
        self.attachment = dict(
            attachment_id=ATTACHMENT, conversation_id=CONVERSATION,
            request_id="upload-1", name="logs.zip", content_type="application/zip",
            size=len(PAYLOAD), sha256=DIGEST, status="RESERVED", created_at=WHEN,
            case_attachment_id=None,
        )

    def create_conversation(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.thread_ids.append(threading.get_ident())
        return dict(conversation_id=CONVERSATION, request_id=kwargs["request_id"], schema_version=1)

    def get_conversation(self, **kwargs):
        self.calls.append(("get", kwargs))
        return conversation_detail(self.report, kwargs["include"], last_event_id=len(self.events))

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        return dict(conversation_id=CONVERSATION, message_id=ATTACHMENT,
                    request_id=kwargs["request_id"], event_id=1, status="ACCEPTED")

    def list_events(self, conversation_id, after_sequence, limit, owner_key=None):
        self.calls.append(("events", (conversation_id, after_sequence, limit)))
        if after_sequence > len(self.events):
            raise AgentStoreError("EVENT_CURSOR_INVALID", "事件游标超出会话历史。", 400)
        return dict(events=self.events[after_sequence:after_sequence + limit], stream_closed=self.closed)

    def prepare_attachment(self, **kwargs):
        self.calls.append(("prepare", kwargs))
        return self.attachment

    def upload_attachment(self, **kwargs):
        self.calls.append(("upload", kwargs))
        self.thread_ids.append(threading.get_ident())
        parts = []
        while chunk := kwargs["content"].read(1024 * 1024):
            parts.append(chunk)
        assert b"".join(parts) == PAYLOAD
        return {**self.attachment, "status": "READY"}


def app_for(service):
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def validation(_request, exc):
        return JSONResponse(error_envelope(validation_error_from(exc)), status_code=400)

    agent_http.register_agent_routes(app, service, "http://xiaodao.internal/prefix")
    return app


def run_request(service, method, path, **kwargs):
    supplied = kwargs.get("headers", {})
    kwargs["headers"] = [("X-Agent-Owner-Key", OWNER), *supplied] if isinstance(supplied, list) else {"X-Agent-Owner-Key": OWNER, **supplied}
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(service)),
                                     base_url="http://local", headers={"X-Agent-Owner-Key": OWNER}) as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(scenario())


def test_conversation_and_attachment_routes_have_typed_contracts():
    schema = app_for(None).openapi()
    assert set(schema["paths"]) == {
        f"{BASE}/conversations", f"{BASE}/conversations/{{conversation_id}}",
        f"{BASE}/conversations/{{conversation_id}}/messages", f"{BASE}/conversations/{{conversation_id}}/events",
        f"{BASE}/attachments", f"{BASE}/attachments/{{attachment_id}}/content",
        f"{BASE}/conversations/{{conversation_id}}/stop",
        f"{BASE}/conversations/{{conversation_id}}/files/{{artifact_id}}/content",
        f"{BASE}/conversations/{{conversation_id}}/runs/{{run_id}}/feedback",
    }
    response = schema["paths"][f"{BASE}/conversations/{{conversation_id}}/events"]["get"]["responses"]["200"]
    assert set(response["content"]) == {"text/event-stream"}
    assert response["content"]["text/event-stream"]["schema"]["$ref"].endswith("/AgentEvent")
    assert "AgentEvent" in schema["components"]["schemas"]
    assert "AgentErrorEnvelope" in schema["components"]["schemas"]
    route = schema["paths"][f"{BASE}/conversations/{{conversation_id}}"]["get"]
    assert route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SuccessEnvelope_ConversationDetailResponse_")
    assert next(item for item in route["parameters"] if item["name"] == "include")["in"] == "query"
    assert "conversation_id" in schema["components"]["schemas"]["PrepareAgentAttachmentBody"]["required"]


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", f"{BASE}/conversations", {"json": {"request_id": "create-1"}}),
    ("GET", VIEW, {}),
    ("GET", f"{VIEW}?include=none", {}),
    ("GET", f"{VIEW}?include=report", {}),
    ("GET", f"{VIEW}/events", {}),
    ("POST", f"{VIEW}/messages", {"json": {"request_id": "m-1", "text": "日志超时"}}),
    ("POST", f"{BASE}/attachments", {"json": dict(conversation_id=CONVERSATION, request_id="a-1", name="logs.zip", content_type="application/zip", declared_size=1, declared_sha256=DIGEST)}),
    ("PUT", f"{BASE}/attachments/{ATTACHMENT}/content", {"content": b"x", "headers": {
        "Idempotency-Key": ATTACHMENT, "Content-Type": "application/zip", "X-Content-SHA256": DIGEST}}),
])
def test_unconfigured_service_returns_same_503_envelope(method, path, kwargs):
    response = run_request(None, method, path, **kwargs)
    assert response.status_code == 503
    assert response.json() == dict(ok=False, data=None, error=dict(
        code="AGENT_UNAVAILABLE", message="Agent 服务尚未配置。", details=[], retryable=False))


def test_create_and_send_delegate_to_worker_with_original_text():
    fake = FakeAgent()
    result = run_request(fake, "POST", f"{BASE}/conversations", json={"request_id": "业务请求/原文 1"})
    assert result.json()["data"]["conversation_id"] == CONVERSATION
    assert fake.thread_ids[0] != threading.get_ident()
    result = run_request(fake, "POST", f"{VIEW}/messages", json={"request_id": "m-1", "text": "付款服务超时。"})
    assert result.json()["data"]["status"] == "ACCEPTED"
    assert fake.calls[-1][1]["text"] == "付款服务超时。"


def test_null_text_with_attachment_is_valid_and_normalized():
    fake = FakeAgent()
    result = run_request(fake, "POST", f"{VIEW}/messages", json={
        "request_id": "m-1", "text": None, "attachment_ids": [ATTACHMENT],
    })
    assert result.status_code == 200
    assert fake.calls[-1][1]["text"] == ""


@pytest.mark.parametrize("body", [
    {}, {"request_id": 1, "text": "x"}, {"request_id": "x"},
    {"request_id": "x", "text": " "}, {"request_id": "x", "text": 123},
    {"request_id": "x", "text": "x", "unknown": True},
    {"request_id": "x", "attachment_ids": [ATTACHMENT, ATTACHMENT]},
    {"request_id": "x", "attachment_ids": ["arbitrary-id"]},
    {"request_id": "x", "attachment_ids": "[]"},
])
def test_message_validation_rejects_bad_input_before_dispatch(body):
    fake = FakeAgent()
    response = run_request(fake, "POST", f"{VIEW}/messages", json=body)
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert not fake.calls


@pytest.mark.parametrize("path", [
    f"{BASE}/conversations/NOT-A-UUID", VIEW + "?unknown=1", VIEW + "/events?after=1",
    VIEW + "?include=report&case_id=" + CASE,
])
def test_path_and_unknown_query_rejected(path):
    fake = FakeAgent()
    assert run_request(fake, "GET", path).status_code == 400
    assert not fake.calls


@pytest.mark.parametrize("method,suffix", [("GET", "/status"), ("GET", "/report"), ("POST", "/attachments")])
def test_removed_aliases_are_not_dispatched(method, suffix):
    fake = FakeAgent()
    assert run_request(fake, method, VIEW + suffix).status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize("query", [
    "include=", "include=unknown", "include=none,report", "include=history,history",
    "include=report,", "include=,report", "include=Report", "include=%20history",
    "include=report&include=artifacts", "include=none&unknown=1",
])
def test_invalid_include_is_rejected_before_reading_any_state(query):
    fake = FakeAgent()
    response = run_request(fake, "GET", VIEW + "?" + query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not fake.calls


@pytest.mark.parametrize("query,expected", [
    ("", INCLUDES), ("?include=none", ()), ("?include=history", ("history",)),
    ("?include=report", ("report",)), ("?include=artifacts", ("artifacts",)),
    ("?include=artifacts,history", ("history", "artifacts")),
    ("?include=artifacts,report,history", INCLUDES),
])
def test_include_selection_is_explicit_and_canonical(query, expected):
    fake = FakeAgent()
    response = run_request(fake, "GET", VIEW + query)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == 3 and data["included"] == list(expected)
    assert (data["history"] is not None) == ("history" in expected)
    assert (data["attachments"] is not None) == ("history" in expected)
    assert (data["result"] is not None) == ("report" in expected)
    assert (data["artifacts"] is not None) == ("artifacts" in expected)
    assert fake.calls == [("get", {"conversation_id": CONVERSATION, "include": expected, "owner_key": OWNER, "run_id": None, "history_before": None, "history_limit": 50})]


def test_lightweight_conversation_delegates_without_loading_history_or_report():
    fake = FakeAgent()
    response = run_request(fake, "GET", VIEW + "?include=none")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["report_state"] == "PENDING"
    assert data["failure"] is None
    assert all(data[key] is None for key in ("history", "attachments", "result", "artifacts"))
    assert fake.calls == [("get", {"conversation_id": CONVERSATION, "include": (), "owner_key": OWNER, "run_id": None, "history_before": None, "history_limit": 50})]


@pytest.mark.parametrize("state", ["PENDING", "UNAVAILABLE"])
def test_report_waiting_and_unavailable_are_successful_read_states(state):
    fake = FakeAgent()
    fake.report["report_state"] = state
    response = run_request(fake, "GET", VIEW + "?include=report")
    assert response.status_code == 200
    data = response.json()["data"]["result"]
    assert data["report_state"] == state
    assert all(data[key] is None for key in ("format", "report", "markdown", "artifact", "source_job_id"))
    assert fake.calls == [("get", {"conversation_id": CONVERSATION, "include": ("report",), "owner_key": OWNER, "run_id": None, "history_before": None, "history_limit": 50})]


def ready_report(format="problem-locator-diagnosis-v3", status="COMPLETED"):
    report = json.loads((Path(__file__).resolve().parents[3] / "fixtures/contracts/positive/user-result.json").read_bytes())
    report["status"] = status
    if status != "COMPLETED":
        report["evidence_gaps"] = ["缺少服务端日志。"]
        report["completion_criteria_mapping"][0]["status"] = "UNKNOWN"
    if status == "INCONCLUSIVE":
        report.update(root_cause=None, causal_factors=[])
    content = json.dumps(report).encode()
    artifact = dict(artifact_id=ATTACHMENT, kind="USER_RESULT", name="diagnosis-result.json",
                    content_type="application/json", resource_kind="FILE", size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(), created_by_job_id=JOB,
                    created_at=WHEN, downloadable=True)
    value = dict(schema_version=1, conversation_id=CONVERSATION, case_id=CASE, case_revision=8,
                 case_status={"COMPLETED": "RESOLVED", "PARTIAL": "PARTIALLY_RESOLVED", "INCONCLUSIVE": "UNRESOLVED"}[status],
                 archive_status="PENDING", report_state="READY", source_job_id=JOB,
                 format=format, report=report, markdown=None, artifact=artifact, failure=None)
    if format == "markdown":
        value.update(report=None, markdown="# 定位报告\r\n\n原因仍待确认。\n")
        value["artifact"].update(kind="GENERIC_REPORT", content_type="text/markdown", name="report.md")
    if format == "generic-v1":
        value.update(artifact=None, report=dict(status="RESOLVED", conclusion="已找到超时原因。",
            root_cause_analysis="服务端处理超过客户端截止时间。", skill_name="generic-locator",
            source_job_id=JOB, source_outcome_id=ATTACHMENT, occurred_at=WHEN))
    return value


@pytest.mark.parametrize("format,status", [
    ("problem-locator-diagnosis-v3", "COMPLETED"),
    ("problem-locator-diagnosis-v3", "PARTIAL"),
    ("problem-locator-diagnosis-v3", "INCONCLUSIVE"),
    ("markdown", "COMPLETED"), ("generic-v1", "COMPLETED"),
])
@pytest.mark.parametrize("archive", ["PENDING", "FAILED"])
def test_ready_report_formats_do_not_wait_for_archive(format, status, archive):
    expected = ready_report(format, status)
    expected["archive_status"] = archive

    fake = FakeAgent()
    fake.report = expected
    response = run_request(fake, "GET", VIEW + "?include=report")
    assert response.status_code == 200
    assert response.json()["data"]["result"] == expected
    assert fake.calls == [("get", {"conversation_id": CONVERSATION, "include": ("report",), "owner_key": OWNER, "run_id": None, "history_before": None, "history_limit": 50})]


@pytest.mark.parametrize("change", [
    {"report_state": "PENDING"}, {"format": None}, {"markdown": "额外正文"},
    {"case_status": "UNRESOLVED"}, {"source_job_id": ATTACHMENT},
    {"case_id": None}, {"case_revision": None}, {"artifact": None},
])
def test_report_rejects_inconsistent_result_structure(change):
    with pytest.raises(ValueError):
        ConversationReportView.model_validate_json(json.dumps({**ready_report(), **change}))


def test_report_http_does_not_revalidate_already_typed_payload(monkeypatch):
    parsed = ConversationDetail.model_validate_json(json.dumps(conversation_detail(ready_report())))

    class ReportAgent(FakeAgent):
        def get_conversation(self, **kwargs):
            return parsed

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP adapter must not validate a typed report twice")

    monkeypatch.setattr(ConversationReportView, "model_validate", forbidden)
    monkeypatch.setattr(ConversationReportView, "model_validate_json", forbidden)
    monkeypatch.setattr(ConversationDetail, "model_validate", forbidden)
    monkeypatch.setattr(ConversationDetail, "model_validate_json", forbidden)
    response = run_request(ReportAgent(), "GET", VIEW)
    assert response.status_code == 200
    assert response.json()["data"]["result"]["report"]["status"] == "COMPLETED"
    assert response.json()["data"]["artifacts"][0]["download_url"] == (
        f"http://xiaodao.internal/prefix{VIEW}/files/{ATTACHMENT}/content?run_id={RUN}")


@pytest.mark.parametrize("include", ["none", "report", "artifacts", "history,report,artifacts"])
def test_conversation_projections_preserve_controlled_operational_error(include):
    class UncertainAgent(FakeAgent):
        def get_conversation(self, **kwargs):
            raise AgentStoreError("DISPATCH_REJECTED", "最终状态暂时无法确认。", 503,
                                  details=[{"field": "persistence", "actual": "UNKNOWN"}])

    response = run_request(UncertainAgent(), "GET", VIEW + "?include=" + include)
    assert response.status_code == 503
    assert response.json()["error"]["details"] == [{"field": "persistence", "actual": "UNKNOWN"}]


def test_artifact_links_come_from_configured_base_and_validated_ids():
    class RedirectingAgent(FakeAgent):
        def get_conversation(self, **kwargs):
            value = conversation_detail(ready_report(), kwargs["include"])
            value["artifacts"][0]["download_url"] = "https://evil.example/private?token=secret"
            return value

    response = run_request(RedirectingAgent(), "GET", VIEW + "?include=artifacts")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["result"] is None
    assert data["case_revision"] == 8 and data["source_job_id"] == JOB
    assert data["artifacts"][0]["download_url"] == (
        f"http://xiaodao.internal/prefix{VIEW}/files/{ATTACHMENT}/content?run_id={RUN}")
    assert "evil.example" not in response.text and "secret" not in response.text


@pytest.mark.parametrize("mutation", ["identity", "projection", "nested-report"])
def test_invalid_dictionary_response_is_not_published(mutation):
    class BrokenAgent(FakeAgent):
        def get_conversation(self, **kwargs):
            value = conversation_detail(ready_report(), kwargs["include"])
            if mutation == "identity":
                value["conversation_id"] = ATTACHMENT
            elif mutation == "projection":
                value["included"] = []
            else:
                value["result"]["report"] = {}
            return value

    response = run_request(BrokenAgent(), "GET", VIEW)
    assert response.status_code == 500
    assert response.json()["ok"] is False and response.json()["data"] is None


@pytest.mark.parametrize("cursor", ["-1", "01", "1.0", " 1", "+1", "9223372036854775808"])
def test_event_cursor_is_strict_before_opening_stream(cursor):
    fake = FakeAgent()
    result = run_request(fake, "GET", VIEW + "/events", headers={"Last-Event-ID": cursor})
    assert result.status_code == 400
    assert not fake.calls


def test_duplicate_event_cursor_rejected():
    response = run_request(FakeAgent(), "GET", VIEW + "/events",
                           headers=[("Last-Event-ID", "0"), ("Last-Event-ID", "1")])
    assert response.status_code == 400


def test_sse_replays_bounded_batches_and_resumes_without_duplicate():
    fake = FakeAgent([event(i) for i in range(1, 203)])
    response = run_request(fake, "GET", VIEW + "/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-transform" in response.headers["cache-control"]
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [frame["sequence"] for frame in frames] == list(range(2, 203))
    assert all(frame["conversation_id"] == CONVERSATION for frame in frames)
    assert [call[1][2] for call in fake.calls] == [20] * 11


def test_sse_business_frames_are_single_line_data_for_default_message_handlers():
    response = run_request(FakeAgent([event(1), event(2)]), "GET", VIEW + "/events")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert "content-length" not in response.headers
    assert response.content.startswith(b": connected\n\n")
    frames = response.content.split(b"\n\n")
    assert frames.pop() == b""
    assert frames.pop(0) == b": connected"
    assert len(frames) == 2
    for sequence, frame in enumerate(frames, 1):
        assert frame.startswith(b"data: ")
        assert b"\n" not in frame and b"\r" not in frame
        assert json.loads(frame.removeprefix(b"data: ")) == event(sequence).model_dump(mode="json")
    assert not any(line.startswith((b"event:", b"id:", b"retry:")) for line in response.content.splitlines())


def test_sse_establishes_empty_live_stream_before_waiting_for_events(monkeypatch):
    monkeypatch.setattr(agent_http, "_EVENT_POLL_SECONDS", 3600)

    async def scenario():
        fake = FakeAgent()
        fake.closed = False
        disconnected = asyncio.Event()
        sent = []
        request_received = False

        async def receive():
            nonlocal request_received
            if not request_received:
                request_received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": VIEW + "/events", "raw_path": (VIEW + "/events").encode(),
                 "root_path": "", "query_string": b"", "headers": [(b"x-agent-owner-key", OWNER.encode())],
                 "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000)}
        await asyncio.wait_for(app_for(fake)(scope, receive, send), timeout=2)
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
        assert dict(sent[0]["headers"])[b"content-type"] == b"text/event-stream; charset=utf-8"
        assert sent[1]["body"] == b": connected\n\n" and sent[1]["more_body"] is True
        assert len(fake.calls) == 1

    asyncio.run(scenario())


def test_largest_valid_escaped_message_remains_replayable():
    accepted = AgentEvent(
        schema_version=2, run_id=RUN, sequence=1, conversation_id=CONVERSATION, type="message.accepted", created_at=WHEN,
        data={"run_id": RUN, "message_id": ATTACHMENT, "request_id": "\x01" * 65536,
              "text": "\x01" * 65536, "attachment_ids": [], "status": "QUEUED",
              "created_at": WHEN, "notice": None},
    )
    response = run_request(FakeAgent([accepted]), "GET", VIEW + "/events")
    assert response.status_code == 200
    frames = [line for line in response.content.splitlines() if line.startswith(b"data: ")]
    assert len(frames) == 1
    assert json.loads(frames[0][6:]) == accepted.model_dump(mode="json")


def test_sse_escapes_user_line_breaks_without_injecting_frames():
    original_text = "中文🙂\r\ndata: fake\n\nevent: result.available\n\"quoted\""
    accepted = AgentEvent(
        schema_version=2, run_id=RUN, sequence=1, conversation_id=CONVERSATION, type="message.accepted", created_at=WHEN,
        data={"run_id": RUN, "message_id": ATTACHMENT, "request_id": "line-breaks",
              "text": original_text, "attachment_ids": [], "status": "QUEUED",
              "created_at": WHEN, "notice": None},
    )
    response = run_request(FakeAgent([accepted]), "GET", VIEW + "/events")
    assert response.status_code == 200
    frames = response.content.split(b"\n\n")
    assert len(frames) == 3 and frames[-1] == b""
    assert frames[0] == b": connected"
    assert b"\n" not in frames[1] and b"\r" not in frames[1]
    assert json.loads(frames[1][6:])["data"]["text"] == original_text


def test_sse_out_of_range_cursor_returns_json_before_headers():
    response = run_request(FakeAgent([event(1)]), "GET", VIEW + "/events", headers={"Last-Event-ID": "2"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")


def test_sse_cannot_mix_other_conversation_events():
    wrong = event(1).model_copy(update={"conversation_id": ATTACHMENT})
    response = run_request(FakeAgent([wrong]), "GET", VIEW + "/events")
    assert response.status_code == 503
    assert ATTACHMENT not in response.text


def test_sse_heartbeat_and_live_archive_completion(monkeypatch):
    class Live(FakeAgent):
        def list_events(self, conversation_id, after_sequence, limit, owner_key=None):
            result = super().list_events(conversation_id, after_sequence, limit)
            if len(self.calls) == 3:
                self.events = [event(1, "archive.updated")]
                self.closed = True
            return result
    fake = Live()
    fake.closed = False
    monkeypatch.setattr(agent_http, "_HEARTBEAT_SECONDS", 0.001)
    monkeypatch.setattr(agent_http, "_EVENT_POLL_SECONDS", 0.002)
    response = run_request(fake, "GET", VIEW + "/events")
    assert ": heartbeat" in response.text
    assert '"type":"archive.updated"' in response.text


def test_sse_disconnect_stops_reader_without_cancelling_work():
    class Disconnected:
        async def is_disconnected(self):
            return True
    async def scenario():
        fake = FakeAgent()
        iterator = agent_http._events(Disconnected(), fake, CONVERSATION, 0,
            agent_http.AgentEventBatch(events=[], stream_closed=False))
        assert await anext(iterator) == b": connected\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        assert not fake.calls
    asyncio.run(scenario())


def test_slow_sse_consumer_does_not_prefetch_more_durable_batches():
    class Connected:
        async def is_disconnected(self):
            return False
    async def scenario():
        fake = FakeAgent([event(i) for i in range(1, 22)])
        iterator = agent_http._events(Connected(), fake, CONVERSATION, 0,
            agent_http.AgentEventBatch(events=fake.events[:20], stream_closed=False))
        await anext(iterator)
        first = await anext(iterator)
        assert first.startswith(b"data: ")
        assert json.loads(first[6:])["sequence"] == 1
        assert not fake.calls
        await iterator.aclose()
        assert not fake.calls
    asyncio.run(scenario())


def test_multiple_subscribers_receive_same_durable_events():
    async def scenario():
        fake = FakeAgent([event(1)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(fake)), base_url="http://local") as client:
            one, two = await asyncio.gather(client.get(VIEW + "/events"), client.get(VIEW + "/events"))
        assert one.text == two.text
    asyncio.run(scenario())


def test_prepare_provides_raw_upload_descriptor_and_upload_uses_bounded_stream():
    fake = FakeAgent()
    response = run_request(fake, "POST", BASE + "/attachments", json=dict(
        conversation_id=CONVERSATION, request_id="a-1", name="logs.zip", content_type="application/zip",
        declared_size=len(PAYLOAD), declared_sha256=DIGEST,
    ))
    upload = response.json()["data"]["upload"]
    assert fake.calls[0][1]["conversation_id"] == CONVERSATION
    assert upload["url"] == f"http://xiaodao.internal/prefix{BASE}/attachments/{ATTACHMENT}/content"
    assert upload["expected_content_length"] == len(PAYLOAD)
    assert upload["expires_at"] is None
    response = run_request(fake, "PUT", f"{BASE}/attachments/{ATTACHMENT}/content",
                           content=PAYLOAD, headers=upload["required_headers"])
    assert response.json()["data"]["status"] == "READY"
    stream = fake.calls[-1][1]["content"]
    assert stream.closed
    assert max(stream.read_requests) <= 1024 * 1024
    assert fake.thread_ids[-1] != threading.get_ident()


@pytest.mark.parametrize("conversation", [None, "not-a-uuid"])
def test_attachment_reservation_requires_explicit_valid_conversation(conversation):
    fake = FakeAgent()
    body = dict(request_id="a-1", name="logs.zip", content_type="application/zip",
                declared_size=len(PAYLOAD), declared_sha256=DIGEST)
    if conversation is not None:
        body["conversation_id"] = conversation
    response = run_request(fake, "POST", BASE + "/attachments", json=body)
    assert response.status_code == 400 and not fake.calls


@pytest.mark.parametrize("header,value", [
    ("Idempotency-Key", CASE), ("Content-Length", "01"), ("X-Content-SHA256", "A" * 64),
])
def test_invalid_upload_headers_never_reach_service(header, value):
    fake = FakeAgent()
    headers = {"Idempotency-Key": ATTACHMENT, "Content-Type": "application/zip",
               "Content-Length": str(len(PAYLOAD)), "X-Content-SHA256": DIGEST, header: value}
    assert run_request(fake, "PUT", f"{BASE}/attachments/{ATTACHMENT}/content",
                       content=PAYLOAD, headers=headers).status_code == 400
    assert not fake.calls


def test_unexpected_exception_does_not_leak_internal_paths_or_secrets():
    class Broken(FakeAgent):
        def get_conversation(self, **kwargs):
            raise RuntimeError("/srv/private/token=top-secret/logs.txt")
    response = run_request(Broken(), "GET", VIEW)
    assert response.status_code == 500
    assert "top-secret" not in response.text
    assert "/srv" not in response.text


@pytest.mark.parametrize("headers", [{}, {"X-Agent-Owner-Key": ""}, {"X-Agent-Owner-Key": "A" * 64},
    [("X-Agent-Owner-Key", OWNER), ("X-Agent-Owner-Key", OWNER)]])
def test_trusted_owner_header_is_required_before_any_state_access(headers):
    async def scenario():
        fake = FakeAgent()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(fake)), base_url="http://local") as client:
            response = await client.get(VIEW, headers=headers)
        assert response.status_code == 400 and not fake.calls
    asyncio.run(scenario())


def test_management_routes_preserve_owners_cursors_and_frozen_stop_identity():
    fake = FakeAgent()
    summary = dict(conversation_id=CONVERSATION, title="修改后的标题", created_at=WHEN, updated_at=WHEN,
                   current_run=conversation_detail(fake.report)["current_run"],
                   capabilities=conversation_detail(fake.report)["capabilities"])

    def directory(**kwargs):
        fake.calls.append(("list", kwargs))
        return dict(items=[summary], next_cursor="opaque-next")

    def rename(**kwargs):
        fake.calls.append(("rename", kwargs))
        return summary

    def stop(**kwargs):
        fake.calls.append(("stop", kwargs))
        return dict(conversation_id=CONVERSATION, run_id=RUN, request_id=kwargs["request_id"], status="CANCELLING")

    def delete(**kwargs):
        fake.calls.append(("delete", kwargs))
        return dict(conversation_id=CONVERSATION, status="DELETED")

    fake.list_conversations, fake.rename_conversation = directory, rename
    fake.stop_conversation, fake.delete_conversation = stop, delete
    assert run_request(fake, "GET", BASE + "/conversations").json()["data"]["next_cursor"] == "opaque-next"
    assert fake.calls[-1] == ("list", dict(owner_key=OWNER, cursor=None, limit=20))
    assert run_request(fake, "GET", BASE + "/conversations?cursor=opaque%2Bcursor&limit=100").status_code == 200
    assert fake.calls[-1][1]["cursor"] == "opaque+cursor"
    assert run_request(fake, "PATCH", VIEW, json={"title": summary["title"]}).status_code == 200
    assert fake.calls[-1] == ("rename", dict(owner_key=OWNER, conversation_id=CONVERSATION, title=summary["title"]))
    assert run_request(fake, "POST", VIEW + "/stop", json={"request_id": "stop-original", "run_id": RUN}).json()["data"]["status"] == "CANCELLING"
    assert fake.calls[-1] == ("stop", dict(owner_key=OWNER, conversation_id=CONVERSATION, request_id="stop-original", run_id=RUN))
    assert run_request(fake, "DELETE", VIEW).json()["data"]["status"] == "DELETED"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=01", "limit=-1", "limit=x", "limit=2&limit=3", "cursor=", "owner_key=x"])
def test_directory_rejects_invalid_paging_before_state_read(query):
    fake = FakeAgent()
    assert run_request(fake, "GET", BASE + "/conversations?" + query).status_code == 400
    assert fake.calls == []


def test_history_and_report_run_selectors_forward_without_extra_reads():
    fake = FakeAgent()
    response = run_request(fake, "GET", VIEW + "?include=history,report&run_id=" + RUN + "&history_before=opaque%2Bbefore&history_limit=100")
    assert response.status_code == 200, response.text
    assert fake.calls == [("get", dict(conversation_id=CONVERSATION, owner_key=OWNER,
        include=("history", "report"), run_id=RUN, history_before="opaque+before", history_limit=100))]


@pytest.mark.parametrize("suffix", ["?run_id=bad", "?history_limit=0", "?history_limit=101", "?history_before=",
    "?history_before=a&history_before=b", "?history_limit=01"])
def test_invalid_history_selector_does_not_read_state(suffix):
    fake = FakeAgent()
    assert run_request(fake, "GET", VIEW + suffix).status_code == 400
    assert not fake.calls
