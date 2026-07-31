"""Regression coverage for the unauthenticated-dashboard hole (2026-07-26).

GET /dashboard/api/memories (and every other /dashboard/api/* route) used to
bypass FirekeepKeyAuthMiddleware entirely because /dashboard was on the
PREFIX skip list — verified against a running instance to return real
memory content (4,066 records) to an unauthenticated caller on the public
internet. The fix moves /dashboard to an EXACT skip (app/main.py's
AUTH_SKIP_EXACT_PATHS) so only the bare HTML shell stays keyless.

Wires the REAL create_dashboard_router + the REAL FirekeepKeyAuthMiddleware
(mirrors cortex/tests/test_auth_consolidation.py's mini-app pattern) so
these are genuine end-to-end ASGI requests through the actual middleware
and the actual router, not an assertion about config values.
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.dashboard import create_dashboard_router
from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware

# Mirrors app.main.AUTH_SKIP_PREFIXES / AUTH_SKIP_EXACT_PATHS as LITERALS
# (not imported) — this test pins the two lists independently of whatever
# app/main.py happens to contain, so a future edit to one can't silently
# make both say the same (possibly wrong) thing.
SKIP_PREFIXES = ("/health", "/version", "/docs", "/redoc", "/openapi.json")
SKIP_EXACT = ("/dashboard", "/dashboard/", "/enroll", "/enroll/anchor")


class _StubVector:
    async def list_memories(self, **kwargs):
        return [{"id": "mem-1", "content": "real memory content"}]

    async def memory_count(self):
        return 4066


class _StubGraph:
    async def get_graph_snapshot(self, **kwargs):
        return {"nodes": [], "edges": []}

    async def get_node_edge_counts(self):
        return {"nodes": 0, "edges": 0}

    async def get_domains(self):
        return []


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def auth_env(redis):
    """A TEAMMATE key: authenticated, deliberately NOT admin.

    The scope set matters. Several routes under /dashboard/api are admin-only,
    so this fixture is what proves a valid key is not a free pass.
    """
    await keys.init_auth(redis_client=redis, enabled=True)
    key = await keys.create_key("teammate", ["memory:read"])
    yield key["api_key"]
    await keys.init_auth(redis_client=None, enabled=False)


@pytest_asyncio.fixture
async def admin_key(redis, auth_env):
    """An admin key on the same store — depends on auth_env so init order holds."""
    key = await keys.create_key("owner", ["admin"])
    return key["api_key"]


def _app(redis) -> FastAPI:
    app = FastAPI()
    app.include_router(create_dashboard_router(_StubGraph(), _StubVector(), redis))
    app.add_middleware(
        FirekeepKeyAuthMiddleware,
        enabled=True,
        redis_url="redis://unused/7",
        redis_client=redis,
        skip_paths=SKIP_PREFIXES,
        skip_exact_paths=SKIP_EXACT,
    )
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestDashboardShellStaysKeyless:
    @pytest.mark.asyncio
    async def test_dashboard_root_no_key_200(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard")
        # FastAPI redirects the bare prefix to the registered "/" (307), or
        # serves it directly -- either way it must NOT be 401.
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_dashboard_root_slash_no_key_200(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestDashboardApiRequiresKey:
    """Every /dashboard/api/* route: keyless -> 401, keyed -> 200 (or the
    route's own 2xx/DELETE semantics), covering all seven routes the real
    router exposes."""

    @pytest.mark.asyncio
    async def test_memories_keyless_401(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/memories")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memories_keyed_200_and_returns_data(self, redis, auth_env):
        """Proves the nginx-injection story: a valid X-API-Key reaches the
        SAME route that leaked data unauthenticated -- so the unified
        dashboard SPA, which always sends this header via nginx, keeps
        working end to end."""
        async with _client(_app(redis)) as c:
            resp = await c.get(
                "/dashboard/api/memories", headers={"X-API-Key": auth_env}
            )
        assert resp.status_code == 200
        assert resp.json()["memories"] == [{"id": "mem-1", "content": "real memory content"}]

    @pytest.mark.asyncio
    async def test_graph_keyless_401(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/graph")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_graph_keyed_200(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/graph", headers={"X-API-Key": auth_env})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_stats_keyless_401(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/stats")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_stats_keyed_200(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/stats", headers={"X-API-Key": auth_env})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dlq_list_keyless_401(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/dlq")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dlq_list_keyed_200(self, redis, auth_env):
        async with _client(_app(redis)) as c:
            resp = await c.get("/dashboard/api/dlq", headers={"X-API-Key": auth_env})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dlq_retry_keyless_401(self, redis, auth_env):
        """REVERSED 2026-07-26: this route used to be a documented key-free
        exception "for the embedded SPA" (see app/ops.py's note on the
        adjacent /ops/dlq/retry-events route). That rationale evaporated
        once every other /dashboard/api/* route got gated -- a key-free
        Retry button next to a DLQ tab that can no longer load protects
        nothing. It is now gated like its siblings."""
        async with _client(_app(redis)) as c:
            resp = await c.post("/dashboard/api/dlq/retry")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dlq_retry_teammate_key_403(self, redis, auth_env):
        """A valid key is not a free pass — requeueing the DLQ is admin-only.

        This asserted 200 until 2026-07-26: the route carried no scope at all,
        so any authenticated caller could replay the dead-letter queue while its
        byte-equivalent twin POST /ops/dlq/retry-events required admin.
        """
        async with _client(_app(redis)) as c:
            resp = await c.post(
                "/dashboard/api/dlq/retry", headers={"X-API-Key": auth_env}
            )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_dlq_retry_admin_key_200(self, redis, admin_key):
        """...and the gate is not simply refusing everyone."""
        async with _client(_app(redis)) as c:
            resp = await c.post(
                "/dashboard/api/dlq/retry", headers={"X-API-Key": admin_key}
            )
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_dlq_clear_keyless_401(self, redis, auth_env):
        """The destructive route -- must never be reachable keyless."""
        async with _client(_app(redis)) as c:
            resp = await c.delete("/dashboard/api/dlq/clear")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dlq_clear_teammate_key_403(self, redis, auth_env):
        """The destructive route: authenticated is not sufficient, admin is required.

        Clearing the DLQ drops every dead-lettered event with no undo, and this
        route had no scope check at all — nor any twin in ops.py to be compared
        against, which is why nothing flagged it.
        """
        async with _client(_app(redis)) as c:
            resp = await c.delete(
                "/dashboard/api/dlq/clear", headers={"X-API-Key": auth_env}
            )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_dlq_clear_admin_key_200(self, redis, admin_key):
        async with _client(_app(redis)) as c:
            resp = await c.delete(
                "/dashboard/api/dlq/clear", headers={"X-API-Key": admin_key}
            )
        assert resp.status_code == 200, resp.text


class TestNestedPathIsNotSwallowedByExactMatch:
    @pytest.mark.asyncio
    async def test_deep_nested_path_still_gated(self, redis, auth_env):
        """/dashboard/api/dlq/retry is 3 segments deep -- confirms the exact
        skip list isn't accidentally doing prefix-ish matching some other
        way (e.g. via a startswith fallback)."""
        async with _client(_app(redis)) as c:
            resp = await c.post("/dashboard/api/dlq/retry")
        assert resp.status_code == 401
