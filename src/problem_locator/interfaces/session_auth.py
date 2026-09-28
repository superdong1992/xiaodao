"""Resolve website identities from server-owned Redis sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_AGENT_PREFIX = "/api/v1/agent"
_CORE_PREFIXES = ("/api/v1/cases", "/api/v1/artifacts", "/api/v1/attachments")


@dataclass(frozen=True, slots=True)
class WebsiteAuthConfig:
    mode: Literal["redis", "trusted_header"] = "redis"
    redis_host: str = ""
    redis_port: int = 6379
    redis_db: int = 0
    redis_username: str | None = None
    redis_password: str | None = field(default=None, repr=False)
    redis_ssl: bool = False
    cookie_name: str = "sessionid"
    owner_namespace: str = "xiaodao-website"


@dataclass(frozen=True, slots=True)
class WebsiteIdentity:
    userid: str
    owner_key: str


class WebsiteAuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def response(self) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "data": None,
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "details": [],
                    "retryable": self.retryable,
                },
            },
            status_code=self.status_code,
        )


def _auth_required() -> WebsiteAuthError:
    return WebsiteAuthError("AUTH_REQUIRED", "登录状态已失效，请重新登录。", 401)


def _auth_unavailable() -> WebsiteAuthError:
    return WebsiteAuthError("AUTH_UNAVAILABLE", "登录验证暂时不可用，请稍后重试。", 503, retryable=True)


class RedisSessionAuthenticator:
    def __init__(self, config: WebsiteAuthConfig, *, client: Redis | None = None):
        self.config = config
        self._client = client
        if self._client is None and config.mode == "redis" and config.redis_host:
            self._client = Redis(
                host=config.redis_host, port=config.redis_port, db=config.redis_db,
                username=config.redis_username, password=config.redis_password,
                ssl=config.redis_ssl, socket_connect_timeout=2, socket_timeout=2,
            )

    async def authenticate(self, request: Request) -> WebsiteIdentity:
        session_id = request.cookies.get(self.config.cookie_name)
        if not session_id:
            raise _auth_required()
        if self._client is None:
            raise _auth_unavailable()
        try:
            value = await self._client.get(f"airobot2-session:{session_id}")
        except RedisError:
            raise _auth_unavailable() from None
        try:
            userid = json.loads(value or "{}")["user"]["userid"]
        except (ValueError, KeyError, TypeError):
            raise _auth_required() from None
        if not isinstance(userid, str) or not userid.strip():
            raise _auth_required()
        owner_input = json.dumps(
            [self.config.owner_namespace, userid], ensure_ascii=False, separators=(",", ":"),
        )
        owner_key = hashlib.sha256(owner_input.encode("utf-8")).hexdigest()
        return WebsiteIdentity(userid=userid, owner_key=owner_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _under_path(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class WebsiteSessionAuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: RedisSessionAuthenticator):
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.authenticator.config.mode == "trusted_header":
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        scope["website_owner_key"] = None
        scope["website_userid"] = None
        request = Request(scope, receive=receive)
        path = scope.get("path", "")
        needs_auth = _under_path(path, _AGENT_PREFIX) or (
            any(_under_path(path, prefix) for prefix in _CORE_PREFIXES)
            and self.authenticator.config.cookie_name in request.cookies
        )
        if needs_auth and request.method != "OPTIONS":
            try:
                identity = await self.authenticator.authenticate(request)
            except WebsiteAuthError as exc:
                await exc.response()(scope, receive, send)
                return
            scope["website_owner_key"] = identity.owner_key
            scope["website_userid"] = identity.userid
        await self.app(scope, receive, send)
