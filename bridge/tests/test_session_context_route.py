"""Tests for POST /sessions/{agent_id}/context (SP2 Phase A Task 4, D-S18)."""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import app.mcp_server as mcp_mod
from app.mcp_server import handle_post_session_context


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value="sess-001")
    r.lpush = AsyncMock()
    r.ltrim = AsyncMock()
    r.llen = AsyncMock(return_value=1)
    return r


class TestHandlePostSessionContext:
    @pytest.mark.asyncio
    async def test_writes_decision_for_active_session(self, mock_redis):
        from app.config import Settings
        from app.session import SessionManager
        mgr = SessionManager(mock_redis, Settings())

        result = await handle_post_session_context(
            mgr, agent_id="agent-x", category="decision",
            content="FirekeepScope screen sc_a1-1 resolved", key="sc_a1",
        )
        assert result["component_count"] == 1
        mock_redis.lpush.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_value_error_without_active_session(self, mock_redis):
        from app.config import Settings
        from app.session import SessionManager
        mock_redis.get = AsyncMock(return_value=None)  # no active session for this agent
        mgr = SessionManager(mock_redis, Settings())

        with pytest.raises(ValueError, match="No active session"):
            await handle_post_session_context(mgr, agent_id="agent-x", category="decision", content="x")

    @pytest.mark.asyncio
    async def test_raises_value_error_for_bad_category(self, mock_redis):
        from app.config import Settings
        from app.session import SessionManager
        mgr = SessionManager(mock_redis, Settings())

        with pytest.raises(ValueError):
            await handle_post_session_context(mgr, agent_id="agent-x", category="not-a-real-category", content="x")


# ---------------------------------------------------------------------------
# Wrapper-level tests: _post_session_context Starlette route, scope-gated by
# require_scope_asgi(request, "session:write") (security review fix,
# write-path IDOR — any session:write holder may target any agent_id, this
# gate is about WHO may call the route at all, not WHICH agent_id they hit).
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_enabled(monkeypatch):
    # Patch the env-derived settings require_scope_asgi actually reads —
    # keys._AUTH_ENABLED is init_auth() state that never exists in the
    # bridge process (the 2026-07-16 scope-gate regression).
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def auth_disabled(monkeypatch):
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=False))


def _make_request(agent_id: str, body: dict, identity: dict | None = None) -> Request:
    """Build a real Starlette Request with a JSON body and path_params, same
    technique as auth/tests/test_asgi_scope_check.py but with a receive
    callable so await request.json() works."""
    body_bytes = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/sessions/{agent_id}/context",
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"agent_id": agent_id},
        "state": {},
    }
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope, receive)


class TestPostSessionContextRouteScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_session_write_scope(self, auth_enabled, mock_redis, monkeypatch):
        from app.config import Settings
        from app.session import SessionManager
        manager = SessionManager(mock_redis, Settings())
        monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))

        request = _make_request(
            "target-agent",
            {"category": "decision", "content": "resolved", "key": "sc_a1"},
            identity={"agent_id": "relay-service", "scopes": ["session:write"], "key_id": "k1"},
        )
        response = await mcp_mod._post_session_context(request)

        assert response.status_code == 200
        mock_redis.lpush.assert_called_once()

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_session_write_scope(self, auth_enabled, monkeypatch):
        fake_get_manager = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_get_manager", fake_get_manager)

        request = _make_request(
            "target-agent",
            {"category": "decision", "content": "resolved"},
            identity={"agent_id": "some-caller", "scopes": ["session:read"], "key_id": "k1"},
        )
        response = await mcp_mod._post_session_context(request)

        assert response.status_code == 403
        fake_get_manager.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_disabled_passes_through(self, auth_disabled, mock_redis, monkeypatch):
        """Local/personal-VPS default (AUTH_ENABLED=false): no identity needed,
        the route still works exactly as before this fix."""
        from app.config import Settings
        from app.session import SessionManager
        manager = SessionManager(mock_redis, Settings())
        monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))

        request = _make_request(
            "target-agent",
            {"category": "decision", "content": "resolved"},
            identity=None,
        )
        response = await mcp_mod._post_session_context(request)

        assert response.status_code == 200
        mock_redis.lpush.assert_called_once()
