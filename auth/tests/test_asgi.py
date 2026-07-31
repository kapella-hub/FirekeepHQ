"""ASGI unit tests for FirekeepKeyAuthMiddleware (SP1a §7 unit block).

Conventions: fakeredis.aioredis + httpx.ASGITransport, matching the repo's
fakeredis/ASGI test style (see relay/tests/test_routes.py).
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware, build_auth_middleware
from auth.config import AuthSettings


async def _echo_identity(request):
    identity = request.scope.get("state", {}).get("identity")
    return JSONResponse({"ok": True, "identity": identity})


def _make_app(**mw_kwargs) -> Starlette:
    return Starlette(
        routes=[
            Route("/protected", _echo_identity),
            Route("/health", _echo_identity),
            Route("/.well-known/agent.json", _echo_identity),
            Route("/dashboard", _echo_identity),
            Route("/dashboard/", _echo_identity),
            Route("/dashboard/api/memories", _echo_identity),
        ],
        middleware=[Middleware(FirekeepKeyAuthMiddleware, **mw_kwargs)],
    )


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def seeded_key(redis):
    """Create a real key in fakeredis using the production layout."""
    await keys.init_auth(redis_client=redis, enabled=True)
    created = await keys.create_key("morgan", ["replay:read", "eval:read"])
    await keys.init_auth(redis_client=None, enabled=False)
    return created


class _BrokenRedis:
    async def hgetall(self, *_a, **_k):
        raise ConnectionError("redis DB 7 down")


class TestEnabled:
    @pytest.mark.asyncio
    async def test_valid_key_passes_and_attaches_identity(self, redis, seeded_key):
        app = _make_app(enabled=True, redis_url="redis://unused/7", redis_client=redis)
        async with _client(app) as c:
            resp = await c.get(
                "/protected", headers={"X-API-Key": seeded_key["api_key"]}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["identity"]["workspace_id"] == "workspace-local"
        assert body["identity"]["member_id"] == "member-owner"
        assert "agent_id" not in body["identity"]
        assert body["identity"]["scopes"] == ["replay:read", "eval:read"]
        assert body["identity"]["credential_id"] == seeded_key["key_id"]

    @pytest.mark.asyncio
    async def test_missing_key_401(self, redis):
        app = _make_app(enabled=True, redis_url="redis://unused/7", redis_client=redis)
        async with _client(app) as c:
            resp = await c.get("/protected")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Missing X-API-Key header"}

    @pytest.mark.asyncio
    async def test_invalid_key_401(self, redis):
        app = _make_app(enabled=True, redis_url="redis://unused/7", redis_client=redis)
        async with _client(app) as c:
            resp = await c.get("/protected", headers={"X-API-Key": "nxs_" + "0" * 48})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unknown API key"}

    @pytest.mark.asyncio
    async def test_expired_key_401(self, redis):
        api_key = "nxs_" + "ab" * 24
        h = keys._hash_key(api_key)
        await redis.hset(
            f"auth:key:{h}",
            mapping={
                "agent_id": "old",
                "scopes": json.dumps(["*"]),
                "created_at": "2020-01-01T00:00:00+00:00",
                "key_id": h[:16],
                "expires_at": "2020-06-01T00:00:00+00:00",
            },
        )
        app = _make_app(enabled=True, redis_url="redis://unused/7", redis_client=redis)
        async with _client(app) as c:
            resp = await c.get("/protected", headers={"X-API-Key": api_key})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_skip_paths_bypass(self, redis):
        app = _make_app(
            enabled=True,
            redis_url="redis://unused/7",
            redis_client=redis,
            skip_paths=("/health", "/.well-known/agent.json"),
        )
        async with _client(app) as c:
            health = await c.get("/health")
            card = await c.get("/.well-known/agent.json")
            protected = await c.get("/protected")
        assert health.status_code == 200
        assert card.status_code == 200
        assert protected.status_code == 401

    @pytest.mark.asyncio
    async def test_skip_exact_paths_bypass_but_nested_paths_stay_gated(self, redis):
        """skip_exact_paths matches ONLY the literal path — unlike skip_paths'
        prefix match, "/dashboard" here must NOT exempt "/dashboard/api/x"."""
        app = _make_app(
            enabled=True,
            redis_url="redis://unused/7",
            redis_client=redis,
            skip_paths=("/health",),
            skip_exact_paths=("/dashboard", "/dashboard/"),
        )
        async with _client(app) as c:
            shell = await c.get("/dashboard")
            shell_slash = await c.get("/dashboard/")
            nested = await c.get("/dashboard/api/memories")
        assert shell.status_code == 200
        assert shell_slash.status_code == 200
        assert nested.status_code == 401

    @pytest.mark.asyncio
    async def test_redis_down_fails_closed_503(self):
        """Reliability Principle: enabled + store down => 503, never fall open."""
        app = _make_app(
            enabled=True, redis_url="redis://unused/7", redis_client=_BrokenRedis()
        )
        async with _client(app) as c:
            resp = await c.get("/protected", headers={"X-API-Key": "nxs_" + "1" * 48})
        assert resp.status_code == 503
        assert "failing closed" in resp.json()["detail"]


class TestDisabled:
    @pytest.mark.asyncio
    async def test_disabled_passthrough_never_touches_redis(self):
        # redis_client deliberately broken: disabled mode must never touch it.
        app = _make_app(
            enabled=False, redis_url="redis://unused/7", redis_client=_BrokenRedis()
        )
        async with _client(app) as c:
            resp = await c.get("/protected")
        assert resp.status_code == 200
        assert resp.json()["identity"] is None


class TestBuildAuthMiddleware:
    def test_disabled_returns_empty_list(self):
        assert build_auth_middleware(AuthSettings(ENABLED=False)) == []

    def test_enabled_returns_configured_middleware(self):
        mws = build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://redis:6379/7"),
            skip_paths=("/health", "/.well-known/agent.json"),
        )
        assert len(mws) == 1
        mw = mws[0]
        assert mw.cls is FirekeepKeyAuthMiddleware
        assert mw.kwargs == {
            "enabled": True,
            "redis_url": "redis://redis:6379/7",
            "skip_paths": ("/health", "/.well-known/agent.json"),
            "skip_exact_paths": (),
        }

    def test_enabled_passes_through_skip_exact_paths(self):
        mws = build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://redis:6379/7"),
            skip_paths=("/health",),
            skip_exact_paths=("/dashboard", "/dashboard/"),
        )
        assert mws[0].kwargs["skip_exact_paths"] == ("/dashboard", "/dashboard/")
