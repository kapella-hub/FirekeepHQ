"""GET /version: real route registration + real auth-skip behavior.

Stronger than a source-text assertion — this imports the actual FastMCP app
(via mcp.http_app(), a public API) and drives it over ASGI, mirroring the
existing test_mcp_auth.py pattern for /health.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod

BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def _app(auth_enabled: bool):
    return mcp_mod.mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=auth_enabled, REDIS_URL="redis://unused/7"),
            skip_paths=("/health", "/version"),
        ),
        stateless_http=True,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_version_route_is_registered():
    """The route exists in the actual Starlette route table FastMCP builds,
    not just as a source-text substring."""
    app = mcp_mod.mcp.http_app()
    routes = {r.name: r for r in app.routes if getattr(r, "name", None) == "version"}
    assert "version" in routes
    assert routes["version"].path == "/version"
    assert "GET" in routes["version"].methods


class TestVersionRoute:
    @pytest.mark.asyncio
    async def test_version_returns_provenance_payload(self):
        async with _client(_app(auth_enabled=False)) as c:
            resp = await c.get("/version")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "bridge"
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
            resp = await c.get("/sessions")
        assert resp.status_code == 401


def test_run_call_skip_paths_include_version():
    """The real mcp.run() call (production wiring, not this test's rebuilt
    app) must pass /version through skip_paths too."""
    src = (BRIDGE_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert '"/version"' in main_block
