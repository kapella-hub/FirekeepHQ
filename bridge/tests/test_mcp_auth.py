"""SP1a enforcement: bridge MCP + REST surfaces 401 unauthenticated when enabled.

GET /sessions leaks teammates' session shadows + file lists to any network
caller today (auth-gap report §3) — this test encodes that exposure closing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

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


# ---------------------------------------------------------------------------
# GET /sessions/{session_id} — scope-gated by require_scope_asgi(request,
# "session:read"), same wrapper-test technique as test_session_context_route.py
# and test_distill_requeue.py. This route returns the full shadow — scratch
# included, which carries the client's workspace_snapshot blob (git branch,
# recent commits, diff stats) — unredacted. Its two siblings
# (/sessions/{agent_id}/context, /ops/distill-dlq/requeue) already gated on
# scope; this one did not.
# ---------------------------------------------------------------------------


def _make_single_session_request(session_id: str, identity: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/sessions/{session_id}",
        "headers": [],
        "path_params": {"session_id": session_id},
        "state": {},
    }
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope)


@pytest.fixture
def auth_enabled(monkeypatch):
    # Patch the env-derived settings require_scope_asgi actually reads —
    # keys._AUTH_ENABLED is init_auth() state that never exists in the
    # bridge process (the 2026-07-16 scope-gate regression).
    import auth.asgi as asgi_module
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def auth_disabled(monkeypatch):
    import auth.asgi as asgi_module
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=False))


class TestGetSingleSessionRouteScopeGate:
    """This route returns the full shadow — scratch included, which carries the
    client's workspace snapshot. Its two siblings gate on scope; it did not."""

    @pytest.mark.asyncio
    async def test_get_single_session_requires_session_read_scope(self, auth_enabled, monkeypatch):
        fake_get_manager = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_get_manager", fake_get_manager)

        request = _make_single_session_request(
            "sess-001",
            identity={"agent_id": "some-caller", "scopes": ["relay:read"], "key_id": "k1"},
        )
        response = await mcp_mod._get_session(request)

        assert response.status_code == 403
        fake_get_manager.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_when_caller_has_session_read_scope(self, auth_enabled, monkeypatch):
        manager = AsyncMock()
        manager.get_session_data = AsyncMock(
            return_value={"goal": "g", "outcome": "", "duration_seconds": None}
        )
        monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))

        request = _make_single_session_request(
            "sess-001",
            identity={"agent_id": "relay-service", "scopes": ["session:read"], "key_id": "k1"},
        )
        response = await mcp_mod._get_session(request)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_auth_disabled_passes_through(self, auth_disabled, monkeypatch):
        """Local/personal-VPS default (AUTH_ENABLED=false): no identity needed,
        the route still works exactly as before this fix."""
        manager = AsyncMock()
        manager.get_session_data = AsyncMock(
            return_value={"goal": "g", "outcome": "", "duration_seconds": None}
        )
        monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))

        request = _make_single_session_request("sess-001", identity=None)
        response = await mcp_mod._get_session(request)

        assert response.status_code == 200
