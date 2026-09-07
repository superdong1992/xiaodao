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
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == list(range(2, 203))
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert all(frame["conversation_id"] == CONVERSATION for frame in frames)
    assert [call[1][2] for call in fake.calls] == [20] * 11


def test_largest_valid_escaped_message_remains_replayable():
    accepted = AgentEvent(
        sequence=1, conversation_id=CONVERSATION, type="message.accepted", created_at=WHEN,
        data={"message_id": ATTACHMENT, "request_id": "\x01" * 65536,
              "text": "\x01" * 65536, "attachment_ids": [], "status": "QUEUED",
              "created_at": WHEN, "notice": None},
    )
    response = run_request(FakeAgent([accepted]), "GET", VIEW + "/events")
    assert response.status_code == 200
    assert "id: 1" in response.text


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
    assert "event: archive.updated" in response.text


def test_sse_disconnect_stops_reader_without_cancelling_work():
    class Disconnected:
        async def is_disconnected(self):
            return True
    async def scenario():
        fake = FakeAgent()
        iterator = agent_http._events(Disconnected(), fake, CONVERSATION, 0,
            agent_http.AgentEventBatch(events=[], stream_closed=False))
        assert await anext(iterator) == b"retry: 2000\n\n"
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
        assert b"id: 1\n" in first
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
