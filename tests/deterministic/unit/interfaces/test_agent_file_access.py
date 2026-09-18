"""Ownership and resource leases hold across the actual ASGI stream lifetime."""
from __future__ import annotations

import asyncio
import io
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from problem_locator.agent.models import AgentStoreError, ConversationDetail
from problem_locator.interfaces.http_app import create_http_app
from tests.deterministic.unit.interfaces.fakes import FakeApplicationService, FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness
from tests.deterministic.unit.interfaces.test_agent_http import (
    ATTACHMENT, CASE, CONVERSATION, OWNER, RUN, VIEW, conversation_detail, ready_report,
)


class GuardedAgent:
    def __init__(self):
        self.active = 0
        self.entered = self.exited = 0

    def authorize_case(self, case_id, owner_key=None):
        if case_id == CASE:
            if owner_key != OWNER:
                raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除。", 404)
            return CONVERSATION
        return None

    def authorize_attachment(self, attachment_id, owner_key=None):
        return self.authorize_case(CASE, owner_key)

    @contextmanager
    def operation_lease(self, conversation_id, *, owner_key=None):
        assert conversation_id == CONVERSATION
        self.authorize_case(CASE, owner_key)
        self.active += 1
        self.entered += 1
        try:
            yield
        finally:
            self.active -= 1
            self.exited += 1

    def get_conversation(self, conversation_id, *, include, run_id=None, owner_key=None):
        assert conversation_id == CONVERSATION and include == ("artifacts",) and run_id == RUN
        self.authorize_case(CASE, owner_key)
        assert self.active == 1
        return ConversationDetail.model_validate(conversation_detail(ready_report(), include))


class FileQuery:
    def __init__(self):
        self.calls = []
        self.streams = []

    def open_artifact(self, case_id, artifact_id):
        self.calls.append((case_id, artifact_id))
        stream = io.BytesIO(b"immutable bytes")
        self.streams.append(stream)
        return SimpleNamespace(stream=stream, artifact=SimpleNamespace(
            size=15, content_type="application/json", sha256="a" * 64))


def app_for(agent, query):
    return create_http_app(command_port=FakeApplicationService(), query_port=query,
        state_admin=FakeStateAdmin(readiness=readiness()), agent_service=agent, public_base_url="http://local")


@pytest.mark.parametrize("suffix", [f"/api/v1/cases/{CASE}", f"/api/v1/cases/{CASE}/artifacts",
    f"/api/v1/artifacts/{ATTACHMENT}/content?case_id={CASE}", f"/api/v1/attachments/{ATTACHMENT}/content"])
@pytest.mark.parametrize("owner", [None, "b" * 64])
def test_core_routes_cannot_bypass_agent_ownership(suffix, owner):
    async def scenario():
        agent, query = GuardedAgent(), FileQuery()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(agent, query)), base_url="http://local") as client:
            headers = {} if owner is None else {"X-Agent-Owner-Key": owner}
            method = "PUT" if suffix.startswith("/api/v1/attachments/") else "GET"
            response = await client.request(method, suffix, headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"
        assert query.calls == [] and agent.active == 0
        assert response.headers.get("X-Problem-Locator-Correlation-ID")
    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_file_lease_lasts_until_stream_finishes_or_client_cancels(native, cancel):
    async def scenario():
        agent, query = GuardedAgent(), FileQuery()
        started, release = asyncio.Event(), asyncio.Event()
        path = VIEW + f"/files/{ATTACHMENT}/content" if native else f"/api/v1/artifacts/{ATTACHMENT}/content"
        query_string = f"run_id={RUN}" if native else f"case_id={CASE}"
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": query_string.encode(), "headers": [(b"x-agent-owner-key", OWNER.encode())],
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
            elif message.get("body"):
                assert message["body"] == b"immutable bytes"
                started.set()
                await release.wait()

        task = asyncio.create_task(app_for(agent, query)(scope, receive, send))
        await asyncio.wait_for(started.wait(), 3)
        assert agent.active == 1 and agent.entered == 1 and agent.exited == 0
        assert not query.streams[0].closed
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await asyncio.wait_for(task, 3)
        assert agent.active == 0 and agent.exited == 1
        assert query.streams[0].closed
        assert query.calls == [(CASE, ATTACHMENT)]
    asyncio.run(scenario())
