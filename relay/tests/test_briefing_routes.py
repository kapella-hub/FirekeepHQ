"""SP1b substrate: relay GET /tasks + GET /bulletin (briefing aggregator source).

Handler-level 200/data tests use fakeredis (mirrors test_scope_routes.py). The
401 enforcement test hand-builds a Starlette app behind the REAL auth middleware
with relay's exact skip_paths (mirrors test_mcp_auth.py) — relay's conftest stubs
fastmcp with a no-op FakeFastMCP so the production http_app cannot be built here.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod
from app.routes import handle_get_tasks, handle_get_bulletin
from app.tasks import create_task
from app.bulletin import post_bulletin

RELAY_SKIP_PATHS = ("/health", "/.well-known/agent.json")


class TestBriefingHandlers:
    @pytest.mark.asyncio
    async def test_get_tasks_returns_created_tasks(self, redis):
        await create_task(redis, "write tests", assignee="alice", assigner="bob", priority="high")
        result = await handle_get_tasks(redis, assignee="alice")
        assert result["count"] == 1
        assert result["tasks"][0]["title"] == "write tests"
        assert result["tasks"][0]["assignee"] == "alice"

    @pytest.mark.asyncio
    async def test_get_tasks_filters_by_status(self, redis):
        await create_task(redis, "t1", assignee="alice")
        result = await handle_get_tasks(redis, assignee="alice", status="completed")
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_tasks_empty(self, redis):
        result = await handle_get_tasks(redis)
        assert result == {"tasks": [], "count": 0}

    @pytest.mark.asyncio
    async def test_get_bulletin_returns_posts(self, redis):
        await post_bulletin(redis, "deployed v2", "alice", ["deploy"], 24)
        result = await handle_get_bulletin(redis, limit=5)
        assert result["count"] == 1
        assert result["posts"][0]["content"] == "deployed v2"

    @pytest.mark.asyncio
    async def test_get_bulletin_empty(self, redis):
        result = await handle_get_bulletin(redis)
        assert result == {"posts": [], "count": 0}


def _auth_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/tasks", mcp_mod._route_get_tasks, methods=["GET"]),
            Route("/bulletin", mcp_mod._route_get_bulletin, methods=["GET"]),
            Route("/health", mcp_mod._health, methods=["GET"]),
        ],
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7"),
            skip_paths=RELAY_SKIP_PATHS,
        ),
    )


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestBriefingRoutesAuth:
    @pytest.mark.asyncio
    async def test_tasks_401_unauth(self):
        async with _client(_auth_app()) as c:
            resp = await c.get("/tasks")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_bulletin_401_unauth(self):
        async with _client(_auth_app()) as c:
            resp = await c.get("/bulletin")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_health_still_open(self):
        async with _client(_auth_app()) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200
