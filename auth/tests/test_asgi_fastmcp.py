"""FastMCP 3.1 integration: middleware passed to http_app wraps BOTH the /mcp
endpoint and @mcp.custom_route REST routes (SP1a §4.1).

This is the single real-fastmcp proof backing the four services' run()
wiring (their own suites stub fastmcp or cannot execute __main__). Runs in
the shared-modules CI job (cortex/requirements.txt provides fastmcp 3.1.1).
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware

fastmcp = pytest.importorskip("fastmcp")


def _build_service(redis_client):
    mcp = fastmcp.FastMCP("AuthIntegration")

    @mcp.tool()
    async def echo(text: str) -> str:
        return text

    @mcp.custom_route("/sessions", methods=["GET"], name="sessions")
    async def _sessions(request: Request) -> JSONResponse:
        return JSONResponse({"sessions": []})

    @mcp.custom_route("/health", methods=["GET"], name="health")
    async def _health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp.http_app(
        middleware=[
            Middleware(
                FirekeepKeyAuthMiddleware,
                enabled=True,
                redis_url="redis://unused/7",
                skip_paths=("/health",),
                redis_client=redis_client,
            )
        ],
        stateless_http=True,
    )


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def valid_key(redis):
    await keys.init_auth(redis_client=redis, enabled=True)
    created = await keys.create_key("teammate", ["replay:read"])
    await keys.init_auth(redis_client=None, enabled=False)
    return created["api_key"]


_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "echo", "arguments": {"text": "hi"}},
}


class TestFastMCPWrapping:
    @pytest.mark.asyncio
    async def test_unauth_tools_call_401(self, redis):
        app = _build_service(redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post("/mcp", json=_TOOLS_CALL, headers=_MCP_HEADERS)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Missing X-API-Key header"}

    @pytest.mark.asyncio
    async def test_unauth_custom_route_401(self, redis):
        app = _build_service(redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/sessions")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_health_skipped(self, redis):
        app = _build_service(redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_key_reaches_custom_route(self, redis, valid_key):
        app = _build_service(redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/sessions", headers={"X-API-Key": valid_key})
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}
