"""SP1a enforcement: bridge MCP + REST surfaces 401 unauthenticated when enabled.

GET /sessions leaks teammates' session shadows + file lists to any network
caller today (auth-gap report §3) — this test encodes that exposure closing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod

BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def _app():
    return mcp_mod.mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7"),
            skip_paths=("/health",),
        ),
        stateless_http=True,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestBridgeEnforcement:
    @pytest.mark.asyncio
    async def test_unauth_mcp_endpoint_401(self):
        async with _client(_app()) as c:
            resp = await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unauth_sessions_route_401(self):
        async with _client(_app()) as c:
            resp = await c.get("/sessions")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_health_still_open(self):
        async with _client(_app()) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200


def test_run_call_wires_auth_middleware():
    src = (BRIDGE_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "middleware=build_auth_middleware(" in main_block
    assert "from auth.asgi import build_auth_middleware" in main_block
