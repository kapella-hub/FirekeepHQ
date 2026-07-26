"""SP1a enforcement: relay REST surfaces 401 unauthenticated when enabled.

relay/tests/conftest.py stubs fastmcp with a no-op FakeFastMCP (custom_route
decorators return the bare functions), so the production http_app cannot be
built here. Instead: relay's REAL route handlers on a Starlette app behind
the REAL middleware list from build_auth_middleware() with relay's exact
skip_paths. The FastMCP-wrapping proof lives in auth/tests/
test_asgi_fastmcp.py; the run() wiring line is pinned source-level below.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod

RELAY_ROOT = Path(__file__).resolve().parents[1]
RELAY_SKIP_PATHS = ("/health", "/.well-known/agent.json")


async def _fake_mcp_endpoint(request):
    return JSONResponse({"mcp": True})


def _app() -> Starlette:
    return Starlette(
        routes=[
            Route("/mcp", _fake_mcp_endpoint, methods=["POST"]),
            Route("/dm/{agent_id}", mcp_mod._route_get_dm, methods=["GET"]),
            Route(
                "/presence/{agent_id}", mcp_mod._route_delete_presence,
                methods=["DELETE"],
            ),
            Route(
                "/.well-known/agent.json", mcp_mod.a2a_agent_card, methods=["GET"],
            ),
            Route("/health", mcp_mod._health, methods=["GET"]),
        ],
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7"),
            skip_paths=RELAY_SKIP_PATHS,
        ),
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestRelayEnforcement:
    @pytest.mark.asyncio
    async def test_unauth_dm_read_401(self):
        """Reading anyone's DMs was open to the network (auth-gap §3)."""
        async with _client(_app()) as c:
            resp = await c.get("/dm/alice")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unauth_presence_delete_401(self):
        """Deregistering another agent's presence was open (auth-gap §3)."""
        async with _client(_app()) as c:
            resp = await c.delete("/presence/alice")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unauth_mcp_endpoint_401(self):
        async with _client(_app()) as c:
            resp = await c.post("/mcp", json={})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_health_still_open(self):
        async with _client(_app()) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_agent_card_discovery_still_open(self):
        """A2A discovery is pre-auth by definition (spec §4.1)."""
        async with _client(_app()) as c:
            resp = await c.get("/.well-known/agent.json")
        assert resp.status_code == 200
        assert "capabilities" in resp.json() or "name" in resp.json()


def test_run_call_wires_auth_middleware():
    src = (RELAY_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "middleware=build_auth_middleware(" in main_block
    assert '"/.well-known/agent.json"' in main_block  # relay's extra skip path
