"""SP1b substrate: sentinel GET /environment + GET /events (briefing source).

Handler-level 200/data tests use fakeredis. The 401 enforcement test builds the
REAL http_app (sentinel's conftest does NOT stub fastmcp) behind the real auth
middleware — mirrors test_mcp_auth.py — and pins that /environment is caught by
the auth gate (i.e. NOT exempted by the ('/health',) prefix skip).
"""
from __future__ import annotations

import httpx
import pytest

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod
from app.mcp_server import handle_get_environment, handle_get_events
from app.store import push_event


class TestSentinelBriefingHandlers:
    @pytest.mark.asyncio
    async def test_environment_full_detail_shape(self, redis):
        await push_event(
            redis, "docker", "container.running", "web up",
            {"container": "web", "state": "running", "status": "Up"}, "info", [],
        )
        result = await handle_get_environment(redis)
        assert result["redis"] == "connected"
        # `collectors` reports what is RUNNING. The docker collector is opt-in
        # (NS_DOCKER_COLLECTOR_ENABLED, default false — mounting the Docker socket is
        # host-root-equivalent), so it is absent here rather than present-and-False:
        # the briefing renders any falsey entry as "Collector(s) degraded", and a
        # deliberate opt-out is not a fault. See test_docker_collector_optin.py.
        assert set(result["collectors"]) == {"git", "files"}
        # Full detail — the /health custom_route body omits these three keys:
        assert "containers" in result
        assert "container_count" in result
        assert "healthy" in result
        # Container detail is reconstructed from the Redis event stream, NOT by calling
        # the Docker API, so it survives the collector being off — events pushed while
        # it ran (or by any other producer via sentinel_push_event) still render.
        assert result["containers"]["web"]["state"] == "running"

    @pytest.mark.asyncio
    async def test_events_returns_pushed_events(self, redis):
        await push_event(redis, "ci", "test.failed", "boom", {}, "error", [])
        result = await handle_get_events(redis, severity="error", limit=10)
        assert result["returned"] == 1
        assert result["events"][0]["summary"] == "boom"
        assert result["total_in_stream"] >= 1

    @pytest.mark.asyncio
    async def test_events_severity_filter_excludes(self, redis):
        await push_event(redis, "ci", "test.passed", "ok", {}, "info", [])
        result = await handle_get_events(redis, severity="error", limit=10)
        assert result["returned"] == 0


def _app():
    return mcp_mod.mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7"),
            skip_paths=("/health",),
        ),
        stateless_http=True,
    )


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


class TestSentinelBriefingRoutesAuth:
    @pytest.mark.asyncio
    async def test_environment_401_unauth(self):
        async with _client() as c:
            resp = await c.get("/environment")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_events_401_unauth(self):
        async with _client() as c:
            resp = await c.get("/events")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_environment_not_exempted_by_health_prefix_skip(self):
        """/environment must be gated; /health must stay skip-listed. This is the
        exact reason the route is /environment and NOT /health/full (D5)."""
        async with _client() as c:
            env = await c.get("/environment")
            health = await c.get("/health")
        assert env.status_code == 401       # under the auth gate
        assert health.status_code != 401    # skip-listed
