"""Authenticated sharing permits bounded reads while preserving write ownership."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from problem_locator.application.queries import ApplicationQueryService
from problem_locator.interfaces import session_auth
from problem_locator.interfaces.http_app import create_http_app
from problem_locator.interfaces.session_auth import WebsiteAuthConfig
from tests.deterministic.unit.agent.test_conversation_detail import publish, setup
from tests.deterministic.unit.application.test_reports import ARTIFACT_ID, CASE_ID, OTHER_ID
from tests.deterministic.unit.interfaces.fakes import FakeApplicationService, FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness
from tests.deterministic.unit.interfaces.test_session_auth import FakeRedis, expected_owner, session_value


@pytest.fixture
def shared_app(setup, monkeypatch):
    owner = expected_owner("original-user")
    created = setup.store.create_conversation("sharing:create", owner_key=owner)
    setup.conversation_id = created.conversation_id
    setup.store.submit_message(created.conversation_id, "sharing:message", "需要分享的诊断问题。")
    publish(setup)
    redis = FakeRedis({
        "airobot2-session:owner": session_value("original-user"),
        "airobot2-session:reader": session_value("another-user"),
    })
    monkeypatch.setattr(session_auth, "Redis", lambda **_: redis)
    queries = ApplicationQueryService(setup.repository, setup.resources, SimpleNamespace(),
        operational_state=setup.operational)
    setup.service.application.open_artifact = queries.open_artifact
    app = create_http_app(command_port=FakeApplicationService(), query_port=queries,
        state_admin=FakeStateAdmin(readiness=readiness()), agent_service=setup.service,
        public_base_url="http://local", website_auth=WebsiteAuthConfig(redis_host="redis.internal"))
    return SimpleNamespace(app=app, context=setup, redis=redis, owner=owner,
        cid=created.conversation_id, rid=created.run_id,
        path=f"/api/v1/agent/conversations/{created.conversation_id}")


def request(system, method, path, *, session="reader", **kwargs):
    async def scenario():
        headers = {} if session is None else {"cookie": f"sessionid={session}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=system.app),
                                     base_url="http://local") as client:
            return await client.request(method, path, headers=headers, **kwargs)
    return asyncio.run(scenario())


def test_another_logged_in_user_can_only_read_with_explicit_share(shared_app):
    system = shared_app
    for suffix in ("", "?share=false"):
        response = request(system, "GET", system.path + suffix)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"
    response = request(system, "GET", system.path + "?share=true")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    detail = response.json()["data"]
    assert detail["conversation_id"] == system.cid and detail["selected_run_id"] == system.rid
    assert detail["history"][0]["message"]["text"] == "需要分享的诊断问题。"
    assert detail["result"]["report"] == system.context.expected.model_dump(mode="json")
    assert detail["capabilities"] == dict.fromkeys(
        ("can_send", "can_stop", "can_rediagnose", "can_rename", "can_delete"), False)
    owner_shared = request(system, "GET", system.path + "?share=true", session="owner")
    assert owner_shared.status_code == 200
    assert owner_shared.json()["data"]["capabilities"] == detail["capabilities"]
    owned = request(system, "GET", system.path + "?share=false", session="owner")
    assert owned.status_code == 200
    assert owned.json()["data"]["capabilities"]["can_rename"] is True
    assert "share" not in parse_qs(urlsplit(owned.json()["data"]["artifacts"][0]["download_url"]).query)


@pytest.mark.parametrize("session", [None, "expired"])
@pytest.mark.parametrize("download", [False, True])
def test_share_still_requires_current_redis_login(shared_app, session, download):
    path = shared_app.path + (f"/files/{ARTIFACT_ID}/content" if download else "")
    response = request(shared_app, "GET", path + "?share=true", session=session)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert shared_app.context.resources.opened == []


@pytest.mark.parametrize("query", ["share=", "share=1", "share=True", "share=false&share=true"])
@pytest.mark.parametrize("download", [False, True])
def test_share_rejects_non_boolean_or_repeated_query_values(shared_app, query, download):
    path = shared_app.path + (f"/files/{ARTIFACT_ID}/content" if download else "")
    response = request(shared_app, "GET", path + "?" + query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert shared_app.context.resources.opened == []


def test_shared_download_url_preserves_run_and_requires_share_on_file_request(shared_app):
    system = shared_app
    detail = request(system, "GET", system.path + "?include=artifacts&share=true").json()["data"]
    download_url = detail["artifacts"][0]["download_url"]
    assert parse_qs(urlsplit(download_url).query) == {"run_id": [system.rid], "share": ["true"]}
    assert system.context.resources.opened == []
    for share in ("", "&share=false"):
        response = request(system, "GET", urlsplit(download_url).path + f"?run_id={system.rid}" + share)
        assert response.status_code == 404
    response = request(system, "GET", download_url)
    assert response.status_code == 200
    assert response.content == system.context.resources.content
    assert response.headers["x-content-sha256"] == detail["artifacts"][0]["sha256"]
    assert response.headers["cache-control"] == "no-store"
    assert len(system.context.resources.opened) == 1
    assert system.context.resources.streams[0].closed


@pytest.mark.parametrize("target", ["artifact", "run", "conversation"])
def test_share_cannot_download_a_file_outside_the_selected_conversation_run(shared_app, target):
    system = shared_app
    conversation = OTHER_ID if target == "conversation" else system.cid
    artifact = OTHER_ID if target == "artifact" else ARTIFACT_ID
    run = OTHER_ID if target == "run" else system.rid
    path = f"/api/v1/agent/conversations/{conversation}/files/{artifact}/content?run_id={run}&share=true"
    response = request(system, "GET", path)
    assert response.status_code == 404
    assert system.context.resources.opened == []


@pytest.mark.parametrize("operation", ["create", "rename", "delete", "stop", "message", "feedback", "followup"])
def test_share_query_never_authorizes_agent_writes(shared_app, operation):
    system = shared_app
    method, path, body = {
        "create": ("POST", "/api/v1/agent/conversations", {"request_id": "share-write"}),
        "rename": ("PATCH", system.path, {"title": "不应修改"}),
        "delete": ("DELETE", system.path, None),
        "stop": ("POST", system.path + "/stop", {"request_id": "share-stop", "run_id": system.rid}),
        "message": ("POST", system.path + "/messages", {"request_id": "share-message", "text": "不应发送"}),
        "feedback": ("PUT", system.path + f"/runs/{system.rid}/feedback",
                     {"request_id": "share-feedback", "rating": "LIKE"}),
        "followup": ("POST", system.path + f"/runs/{system.rid}/followups",
                     {"request_id": "share-followup", "text": "不应追问"}),
    }[operation]
    response = request(system, method, path + "?share=true", **({"json": body} if body else {}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    owned = system.context.service.get_conversation(system.cid, owner_key=system.owner)
    assert owned.title != "不应修改" and len(owned.messages) == 1


@pytest.mark.parametrize("target", ["directory", "events", "feedback", "followups", "followup-events"])
def test_share_does_not_expand_directory_events_feedback_or_followup_reads(shared_app, target):
    system = shared_app
    followups = system.path + f"/runs/{system.rid}/followups"
    path = {"directory": "/api/v1/agent/conversations", "events": system.path + "/events",
            "feedback": system.path + f"/runs/{system.rid}/feedback",
            "followups": followups, "followup-events": followups + "/events"}[target]
    response = request(system, "GET", path + "?share=true")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert shared_app.context.resources.opened == []


@pytest.mark.parametrize("suffix", ["/api/v1/cases/{case}", "/api/v1/cases/{case}/artifacts",
    "/api/v1/artifacts/{artifact}/content?case_id={case}"])
def test_share_cannot_bypass_core_case_or_artifact_ownership(shared_app, suffix):
    path = suffix.format(case=CASE_ID, artifact=ARTIFACT_ID)
    response = request(shared_app, "GET", path + ("&" if "?" in path else "?") + "share=true")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"
    assert shared_app.context.resources.opened == []


@pytest.mark.parametrize("download", [False, True])
def test_deleted_conversation_remains_unreadable_when_shared(shared_app, download):
    system = shared_app
    system.context.store.request_delete(system.cid, owner_key=system.owner)
    path = system.path + (f"/files/{ARTIFACT_ID}/content" if download else "")
    response = request(system, "GET", path + "?share=true")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"
    assert system.context.resources.opened == []
