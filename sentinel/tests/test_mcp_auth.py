"""SP1a enforcement: sentinel MCP surface 401s unauthenticated when enabled."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod

SENTINEL_ROOT = Path(__file__).resolve().parents[1]


def _app():
    return mcp_mod.mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7"),
            skip_paths=("/health",),
        ),
        stateless_http=True,
    )


class TestSentinelEnforcement:
    @pytest.mark.asyncio
    async def test_unauth_tools_call_401(self):
        """sentinel_push_event et al. must not be callable keyless."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "sentinel_push_event",
                        "arguments": {
                            "source": "test", "event_type": "t", "summary": "s",
                        },
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_health_still_open(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.get("/health")
        # /health touches Redis inside the handler and may report degraded —
        # the point here is only that the auth gate does NOT 401 it.
        assert resp.status_code != 401


def test_run_call_wires_auth_middleware():
    src = (SENTINEL_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "middleware=build_auth_middleware(" in main_block
    assert "from auth.asgi import build_auth_middleware" in main_block
