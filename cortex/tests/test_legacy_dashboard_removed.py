"""Cortex's legacy self-served dashboard is gone, and must stay gone.

Until 2026-10-01 cortex-api served a second, older dashboard SPA of its own at
GET /dashboard/ (app/static/dashboard.html), kept keyless by two exact entries
on the auth skip list. It had no key mechanism, so under AUTH_ENABLED=true its
data tabs simply failed; the unified SPA on :8040 superseded it
(docs/THREAT-MODEL.md §5.2). The shell route, the HTML asset and both skip-list
entries were removed together.

The /dashboard/api/* JSON routes are NOT part of that removal: the :8040 SPA
calls them through nginx with DASHBOARD_API_KEY. These tests pin both halves.

Note the middleware runs before routing: with auth enabled, a keyless request
to a now-nonexistent path is 401 (it is no longer exempt), and only a keyed
request reaches the router and gets the 404.
"""

from __future__ import annotations

from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.dashboard as dashboard_mod
from app.dashboard import create_dashboard_router
from app.main import AUTH_SKIP_EXACT_PATHS, AUTH_SKIP_PREFIXES
from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware

SHELL_PATHS = ("/dashboard", "/dashboard/")


class _StubVector:
    async def list_memories(self, **kwargs):
        return []

    async def memory_count(self):
        return 0


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
async def api_key(redis):
    await keys.init_auth(redis_client=redis, enabled=True)
    key = await keys.create_key("teammate", ["memory:read"])
    yield key["api_key"]
    await keys.init_auth(redis_client=None, enabled=False)


def _app(redis, *, auth_enabled: bool) -> FastAPI:
    """The real dashboard router behind the real middleware, wired with the
    production skip lists imported from app.main."""
    app = FastAPI()
    app.include_router(create_dashboard_router(_StubGraph(), _StubVector(), redis))
    app.add_middleware(
        FirekeepKeyAuthMiddleware,
        enabled=auth_enabled,
        redis_url="redis://unused/7",
        redis_client=redis,
        skip_paths=AUTH_SKIP_PREFIXES,
        skip_exact_paths=AUTH_SKIP_EXACT_PATHS,
    )
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )


class TestSkipListNoLongerExemptsDashboard:
    def test_no_dashboard_path_on_either_skip_list(self):
        every_skip = AUTH_SKIP_PREFIXES + AUTH_SKIP_EXACT_PATHS
        assert not any(p.startswith("/dashboard") for p in every_skip), every_skip

    def test_health_and_version_still_skipped(self):
        assert "/health" in AUTH_SKIP_PREFIXES
        assert "/version" in AUTH_SKIP_PREFIXES


class TestShellRouteAndAssetAreGone:
    def test_router_registers_no_html_shell(self):
        router = create_dashboard_router(_StubGraph(), _StubVector(), None)
        paths = {route.path for route in router.routes}
        assert not paths & set(SHELL_PATHS), paths
        # Every surviving route is under the JSON data API.
        assert all(p.startswith("/dashboard/api/") for p in paths), paths

    def test_static_html_asset_is_deleted(self):
        static_dir = Path(dashboard_mod.__file__).parent / "static"
        assert not (static_dir / "dashboard.html").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", SHELL_PATHS)
    async def test_shell_404_with_auth_disabled(self, redis, path):
        async with _client(_app(redis, auth_enabled=False)) as c:
            resp = await c.get(path)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", SHELL_PATHS)
    async def test_shell_keyless_is_401_not_exempt(self, redis, api_key, path):
        async with _client(_app(redis, auth_enabled=True)) as c:
            resp = await c.get(path)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", SHELL_PATHS)
    async def test_shell_keyed_is_404(self, redis, api_key, path):
        async with _client(_app(redis, auth_enabled=True)) as c:
            resp = await c.get(path, headers={"X-API-Key": api_key})
        assert resp.status_code == 404


class TestDataApiTheUnifiedSpaUsesSurvives:
    """The :8040 SPA (dashboard/index.html) calls these four paths."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/dashboard/api/memories"),
            ("GET", "/dashboard/api/graph"),
            ("GET", "/dashboard/api/memory-gc"),
            ("POST", "/dashboard/api/memory-gc/preview"),
        ],
    )
    async def test_keyed_request_is_routed(self, redis, api_key, method, path, monkeypatch):
        monkeypatch.setattr(
            dashboard_mod.gc_worker, "preview_memories", lambda settings, limit: {"items": []}
        )
        async with _client(_app(redis, auth_enabled=True)) as c:
            keyless = await c.request(method, path)
            keyed = await c.request(method, path, headers={"X-API-Key": api_key})
        assert keyless.status_code == 401
        assert keyed.status_code == 200, keyed.text
