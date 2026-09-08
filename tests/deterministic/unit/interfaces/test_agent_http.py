from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from problem_locator.agent.models import AgentEvent, AgentStoreError
from problem_locator.interfaces import agent_http
from problem_locator.interfaces.error_mapping import error_envelope, validation_error_from
from fastapi.responses import JSONResponse


CONVERSATION = "10000000-0000-0000-0000-000000000001"
ATTACHMENT = "20000000-0000-0000-0000-000000000001"
CASE = "30000000-0000-0000-0000-000000000001"
WHEN = "2026-09-07T08:00:00.000Z"
BASE = "/api/v1/agent"
VIEW = f"{BASE}/conversations/{CONVERSATION}"
PAYLOAD = b"test archive bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def event(sequence, kind="agent.progress"):
    data = {"stage": "DIAGNOSE", "message": "正在核对证据"}
    if kind == "archive.updated":
        data = {"status": "READY", "artifacts": []}
    return AgentEvent(
        sequence=sequence, conversation_id=CONVERSATION, case_id=CASE,
        type=kind, created_at=WHEN, data=data,
    )


class FakeAgent:
    def __init__(self, events=None):
        self.events = events or []
        self.calls = []
        self.thread_ids = []
        self.closed = True
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
        return dict(
            schema_version=1, conversation_id=CONVERSATION, status="INTAKE", case_id=None,
            job_id=None, case_status=None, archive_status="NOT_REQUIRED", current_questions=[],
            messages=[], attachments=[], last_event_id=len(self.events), created_at=WHEN, updated_at=WHEN,
        )

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        return dict(conversation_id=CONVERSATION, message_id=ATTACHMENT,
                    request_id=kwargs["request_id"], event_id=1, status="ACCEPTED")

    def list_events(self, conversation_id, after_sequence, limit):
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
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(service)),
                                     base_url="http://local") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(scenario())


def test_all_six_routes_are_present_without_runtime_and_have_typed_contracts():
    schema = app_for(None).openapi()
    assert len(schema["paths"]) == 6
    response = schema["paths"][f"{BASE}/conversations/{{conversation_id}}/events"]["get"]["responses"]["200"]
    assert set(response["content"]) == {"text/event-stream"}
    assert response["content"]["text/event-stream"]["schema"]["$ref"].endswith("/AgentEvent")
    assert "AgentEvent" in schema["components"]["schemas"]
    assert "AgentErrorEnvelope" in schema["components"]["schemas"]


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", f"{BASE}/conversations", {"json": {"request_id": "create-1"}}),
    ("GET", VIEW, {}),
    ("GET", f"{VIEW}/events", {}),
    ("POST", f"{VIEW}/messages", {"json": {"request_id": "m-1", "text": "日志超时"}}),
    ("POST", f"{VIEW}/attachments", {"json": dict(request_id="a-1", name="logs.zip", content_type="application/zip", declared_size=1, declared_sha256=DIGEST)}),
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
])
def test_path_and_unknown_query_rejected(path):
    fake = FakeAgent()
    assert run_request(fake, "GET", path).status_code == 400
    assert not fake.calls


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
                 "root_path": "", "query_string": b"", "headers": [],
                 "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000)}
        await asyncio.wait_for(app_for(fake)(scope, receive, send), timeout=2)
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
        assert dict(sent[0]["headers"])[b"content-type"] == b"text/event-stream; charset=utf-8"
        assert sent[1]["body"] == b": connected\n\n" and sent[1]["more_body"] is True
        assert len(fake.calls) == 1

    asyncio.run(scenario())


def test_largest_valid_escaped_message_remains_replayable():
    accepted = AgentEvent(
        sequence=1, conversation_id=CONVERSATION, type="message.accepted", created_at=WHEN,
        data={"message_id": ATTACHMENT, "request_id": "\x01" * 65536,
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
        sequence=1, conversation_id=CONVERSATION, type="message.accepted", created_at=WHEN,
        data={"message_id": ATTACHMENT, "request_id": "line-breaks",
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
        def list_events(self, conversation_id, after_sequence, limit):
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
    response = run_request(fake, "POST", VIEW + "/attachments", json=dict(
        request_id="a-1", name="logs.zip", content_type="application/zip",
        declared_size=len(PAYLOAD), declared_sha256=DIGEST,
    ))
    upload = response.json()["data"]["upload"]
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
