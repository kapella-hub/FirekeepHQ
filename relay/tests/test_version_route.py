"""GET /version: real route registration + real auth-skip behavior.

relay/tests/conftest.py stubs fastmcp with a no-op FakeFastMCP (custom_route
decorators return the bare functions), so the production http_app cannot be
built here -- same constraint documented in test_mcp_auth.py. Instead: relay's
REAL _version handler on a Starlette app behind the REAL middleware from
build_auth_middleware() with relay's exact skip_paths.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod

RELAY_SKIP_PATHS = ("/health", "/version", "/.well-known/agent.json")


def _app(auth_enabled: bool) -> Starlette:
    return Starlette(
        routes=[
            Route("/version", mcp_mod._version, methods=["GET"]),
            Route("/health", mcp_mod._health, methods=["GET"]),
            Route("/dm/{agent_id}", mcp_mod._route_get_dm, methods=["GET"]),
        ],
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=auth_enabled, REDIS_URL="redis://unused/7"),
            skip_paths=RELAY_SKIP_PATHS,
        ),
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestVersionRoute:
    @pytest.mark.asyncio
    async def test_version_returns_provenance_payload(self):
        async with _client(_app(auth_enabled=False)) as c:
            resp = await c.get("/version")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "relay"
        assert set(body.keys()) == {"service", "version", "git_sha", "build_time"}

    @pytest.mark.asyncio
    async def test_version_reachable_without_auth_when_enabled(self):
        """/version must answer even when AUTH_ENABLED=true and no key is
        supplied -- a support call needs this to work before anyone has
        figured out the API key."""
        async with _client(_app(auth_enabled=True)) as c:
            resp = await c.get("/version")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_other_routes_still_401_when_auth_enabled(self):
        """Regression guard: adding /version to skip_paths must not
        accidentally open anything else."""
        async with _client(_app(auth_enabled=True)) as c:
            resp = await c.get("/dm/alice")
        assert resp.status_code == 401


def test_run_call_skip_paths_include_version():
    """The real mcp.run() call (production wiring, not this test's Starlette
    stand-in) must pass /version through skip_paths too."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert '"/version"' in main_block
