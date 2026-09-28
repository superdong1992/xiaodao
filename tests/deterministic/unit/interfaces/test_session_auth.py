from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager

import httpx
import pytest
from starlette.requests import Request
from redis.exceptions import ConnectionError as RedisConnectionError

from problem_locator.interfaces import session_auth
from problem_locator.interfaces.session_auth import (
    RedisSessionAuthenticator,
    WebsiteAuthConfig,
    WebsiteAuthError,
)
from problem_locator.agent.models import AgentStoreError
from problem_locator.interfaces.http_app import create_http_app
from tests.deterministic.unit.interfaces.fakes import FakeApplicationService, FakeQuery, FakeStateAdmin
from tests.deterministic.unit.interfaces.helpers import readiness


_CASE_ID = "30000000-0000-0000-0000-000000000001"
_ATTACHMENT_ID = "20000000-0000-0000-0000-000000000001"
_CONVERSATION_ID = "10000000-0000-0000-0000-000000000001"


class FakeRedis:
    def __init__(self, values=None, *, error=None):
        self.values = values or {}
        self.error = error
        self.keys = []
        self.closed = 0

    async def get(self, key):
        self.keys.append(key)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.values.get(key)

    async def aclose(self):
        self.closed += 1


def session_value(userid="00123", **extra):
    return json.dumps({"user": {"userid": userid}, "cookie": {"userid": "forged"}, **extra})


def request_for(*, method="GET", headers=()):
    return Request({
        "type": "http", "method": method, "path": "/api/v1/agent/conversations",
        "headers": [(key.lower().encode(), value.encode("latin-1")) for key, value in headers],
    })


def authenticator_for(values=None, **config):
    client = FakeRedis(values)
    return RedisSessionAuthenticator(WebsiteAuthConfig(redis_host="redis.internal", **config), client=client), client


def authenticate(authenticator, *, method="GET", headers=(("cookie", "sessionid=session-a"),)):
    return asyncio.run(authenticator.authenticate(request_for(method=method, headers=headers)))


def expected_owner(userid, namespace="xiaodao-website"):
    return hashlib.sha256(json.dumps([namespace, userid], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_redis_userid_alone_defines_identity_and_preserves_leading_zeroes():
    auth, client = authenticator_for({
        "airobot2-session:session-a": session_value(),
        "airobot2-session:session-b": session_value(),
        "airobot2-session:session-c": session_value("123"),
    })
    first = authenticate(auth, headers=(("cookie", "userid=evil; sessionid=session-a"), ("x-agent-owner-key", "b" * 64)))
    renewed = authenticate(auth, headers=(("cookie", "sessionid=session-b"),))
    other = authenticate(auth, headers=(("cookie", "sessionid=session-c"),))
    assert first.userid == "00123"
    assert first.owner_key == renewed.owner_key == expected_owner("00123")
    assert other.owner_key != first.owner_key
    assert client.keys == [f"airobot2-session:session-{letter}" for letter in "abc"]


def test_owner_namespace_and_unicode_match_json_array_hash_contract():
    auth, _ = authenticator_for({"airobot2-session:session-a": session_value("工号001")}, owner_namespace="小刀")
    assert authenticate(auth).owner_key == expected_owner("工号001", "小刀")


def test_configured_cookie_name_and_quoted_session_are_supported():
    auth, client = authenticator_for({"airobot2-session:session-a": session_value()}, cookie_name="custom_session")
    assert authenticate(auth, headers=(("cookie", 'custom_session="session-a"'),)).userid == "00123"
    assert client.keys == ["airobot2-session:session-a"]


def test_login_is_rechecked_and_expired_session_is_not_cached():
    auth, client = authenticator_for({"airobot2-session:session-a": session_value()})
    assert authenticate(auth).userid == "00123"
    client.values.clear()
    with pytest.raises(WebsiteAuthError, match="登录状态已失效"):
        authenticate(auth)
    assert client.keys == ["airobot2-session:session-a"] * 2


def test_unconfigured_redis_returns_unavailable_and_config_repr_hides_password():
    config = WebsiteAuthConfig(redis_password="do-not-print")
    assert "do-not-print" not in repr(config)
    auth = RedisSessionAuthenticator(config)
    with pytest.raises(WebsiteAuthError) as exc:
        authenticate(auth)
    assert (exc.value.code, exc.value.status_code) == ("AUTH_UNAVAILABLE", 503)


class GuardedWebsiteAgent:
    def __init__(self):
        self.created_owners = []
        self.checked_owners = []
        self.active_leases = 0

    def create_conversation(self, *, request_id, owner_key):
        self.created_owners.append(owner_key)
        return {"conversation_id": _CONVERSATION_ID, "request_id": request_id, "schema_version": 1}

    def authorize_case(self, case_id, owner_key=None):
        self.checked_owners.append(owner_key)
        if owner_key != expected_owner("00123"):
            raise AgentStoreError("AGENT_CONVERSATION_NOT_FOUND", "会话不存在或已删除。", 404)
        return _CONVERSATION_ID

    def authorize_attachment(self, attachment_id, owner_key=None):
        return self.authorize_case(_CASE_ID, owner_key)

    @contextmanager
    def operation_lease(self, conversation_id, *, owner_key=None):
        assert owner_key == expected_owner("00123")
        self.active_leases += 1
        try:
            yield
        finally:
            self.active_leases -= 1


def website_app(monkeypatch, *, values=None, query=None, **config):
    redis = FakeRedis(values)
    monkeypatch.setattr(session_auth, "Redis", lambda **_: redis)
    commands, agent = FakeApplicationService(), GuardedWebsiteAgent()
    query = query or FakeQuery()
    app = create_http_app(
        command_port=commands, query_port=query, state_admin=FakeStateAdmin(readiness=readiness()),
        public_base_url="http://local", agent_service=agent,
        website_auth=WebsiteAuthConfig(redis_host="redis.internal", **config),
    )
    return app, redis, commands, query, agent


def test_real_http_app_passes_redis_identity_to_agent_route_and_rejects_expired_session(monkeypatch):
    app, redis, _, _, agent = website_app(monkeypatch, values={
        "airobot2-session:first": session_value("00123"),
        "airobot2-session:renewed": session_value("00123"),
        "airobot2-session:other": session_value("00456"),
    })

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            for session in ("first", "renewed", "other"):
                response = await client.post("/api/v1/agent/conversations", json={"request_id": session}, headers={
                    "cookie": f"sessionid={session}", "origin": "https://site.internal", "sec-fetch-site": "cross-site",
                })
                assert response.status_code == 200
                assert response.json()["data"]["conversation_id"] == _CONVERSATION_ID
            redis.values.clear()
            response = await client.post("/api/v1/agent/conversations", json={"request_id": "expired"}, headers={
                "cookie": "sessionid=first", "X-Agent-Owner-Key": expected_owner("00123"),
            })
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_REQUIRED"
            assert response.headers.get("X-Problem-Locator-Correlation-ID")

    asyncio.run(scenario())
    assert agent.created_owners == [expected_owner("00123"), expected_owner("00123"), expected_owner("00456")]


def test_real_http_core_guard_accepts_authenticated_owner_for_artifact_stream(monkeypatch):
    from tests.deterministic.unit.interfaces.test_agent_file_access import FileQuery

    query = FileQuery()
    app, _, _, _, agent = website_app(monkeypatch, query=query, values={"airobot2-session:mine": session_value("00123")})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get(
                f"/api/v1/artifacts/{_ATTACHMENT_ID}/content?case_id={_CASE_ID}",
                headers={"cookie": "sessionid=mine", "X-Agent-Owner-Key": "b" * 64},
            )
            assert response.status_code == 200
            assert response.content == b"immutable bytes"

    asyncio.run(scenario())
    assert agent.checked_owners == [expected_owner("00123")]
    assert agent.active_leases == 0
    assert query.calls == [(_CASE_ID, _ATTACHMENT_ID)]
    assert query.streams[0].closed


def test_real_http_openapi_declares_cookie_security_and_removes_trusted_owner_header(monkeypatch):
    app, _, _, _, _ = website_app(monkeypatch, cookie_name="custom_session")
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["WebsiteSession"]["in"] == "cookie"
    assert schema["components"]["securitySchemes"]["WebsiteSession"]["name"] == "custom_session"
    operations = [
        operation for path, methods in schema["paths"].items() if path.startswith("/api/v1/agent/")
        for operation in methods.values() if isinstance(operation, dict) and "responses" in operation
    ]
    assert operations
    for operation in operations:
        assert operation["security"] == [{"WebsiteSession": []}]
        assert {"401", "503"} <= operation["responses"].keys()
        assert all(parameter["name"] != "X-Agent-Owner-Key" for parameter in operation.get("parameters", []))
    assert app.openapi() == schema


@pytest.mark.parametrize("fail_in_lifespan", [False, True])
def test_real_http_lifespan_closes_redis_pool_on_normal_and_exception_exit(monkeypatch, fail_in_lifespan):
    app, redis, _, _, _ = website_app(monkeypatch, values={"airobot2-session:mine": session_value()})

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                response = await client.post("/api/v1/agent/conversations", json={"request_id": "request-1"},
                                             headers={"cookie": "sessionid=mine"})
                assert response.status_code == 200
            assert redis.closed == 0
            if fail_in_lifespan:
                raise RuntimeError("test lifespan cleanup")

    if fail_in_lifespan:
        with pytest.raises(Exception) as exc:
            asyncio.run(scenario())
        error = exc.value
        while isinstance(error, BaseExceptionGroup):
            assert len(error.exceptions) == 1
            error = error.exceptions[0]
        assert type(error) is RuntimeError
        assert str(error) == "test lifespan cleanup"
    else:
        asyncio.run(scenario())
    assert redis.closed == 1


@pytest.mark.parametrize("value", [None, "not json", "{}", session_value("")])
def test_missing_or_invalid_session_returns_401(value):
    auth, _ = authenticator_for({"airobot2-session:session-a": value})
    with pytest.raises(WebsiteAuthError) as exc:
        authenticate(auth)
    assert (exc.value.code, exc.value.status_code) == ("AUTH_REQUIRED", 401)


def test_missing_cookie_returns_401_without_redis_lookup():
    auth, redis = authenticator_for()
    with pytest.raises(WebsiteAuthError) as exc:
        authenticate(auth, headers=())
    assert exc.value.status_code == 401
    assert redis.keys == []


def test_redis_connection_error_returns_503():
    auth = RedisSessionAuthenticator(WebsiteAuthConfig(), client=FakeRedis(error=RedisConnectionError("connection failed")))
    with pytest.raises(WebsiteAuthError) as exc:
        authenticate(auth)
    assert (exc.value.code, exc.value.status_code) == ("AUTH_UNAVAILABLE", 503)


def test_redis_client_receives_connection_settings_and_two_second_timeout(monkeypatch):
    calls = []
    def make_redis(**kwargs):
        calls.append(kwargs)
        return FakeRedis()
    monkeypatch.setattr(session_auth, "Redis", make_redis)
    RedisSessionAuthenticator(WebsiteAuthConfig(
        redis_host="10.0.0.8", redis_port=6380, redis_db=2,
        redis_username="reader", redis_password="secret", redis_ssl=True,
    ))
    assert calls == [{
        "host": "10.0.0.8", "port": 6380, "db": 2, "username": "reader",
        "password": "secret", "ssl": True, "socket_connect_timeout": 2, "socket_timeout": 2,
    }]


def test_trusted_header_mode_keeps_existing_route(monkeypatch):
    app, redis, _, _, agent = website_app(monkeypatch, mode="trusted_header")
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.post("/api/v1/agent/conversations", json={"request_id": "create-1"},
                                         headers={"X-Agent-Owner-Key": "a" * 64})
            assert response.status_code == 200
    asyncio.run(scenario())
    assert agent.created_owners == ["a" * 64]
    assert redis.keys == []


def test_unrelated_core_cookie_keeps_existing_owner_guard(monkeypatch):
    app, redis, _, query, agent = website_app(monkeypatch)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get(f"/api/v1/cases/{_CASE_ID}", headers={"cookie": "theme=dark"})
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "AGENT_CONVERSATION_NOT_FOUND"
    asyncio.run(scenario())
    assert agent.checked_owners == [None]
    assert query.calls == []
    assert redis.keys == []
