"""Tests for the Starlette-level scope-check helper (SP2 Phase A Task 3).

require_scope() in auth/middleware.py is a FastAPI Depends and cannot run on
FastMCP's @mcp.custom_route (plain Starlette) handlers — this covers those.
"""

import pytest
from starlette.requests import Request

from auth.asgi import require_scope_asgi, ScopeError


def _request(identity: dict | None) -> Request:
    scope = {"type": "http", "state": {}}
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope)


@pytest.fixture
def auth_enabled(monkeypatch):
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(
        asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True),
        raising=False,
    )


def test_enforces_scopes_from_env_settings_without_init_auth(monkeypatch):
    """Production regression (2026-07-16 review): the FastMCP services
    (bridge, relay, sentinel) never call init_auth(), so keys._AUTH_ENABLED
    stayed False in those processes even under AUTH_ENABLED=true — every
    require_scope_asgi gate passed anonymously with wildcard scopes.
    Enabled-ness must derive from the same AuthSettings env truth that
    build_auth_middleware reads, not from init_auth's process-local state."""
    import auth.asgi as asgi_module
    import auth.keys as keys_module
    from auth.config import AuthSettings

    assert keys_module._AUTH_ENABLED is False  # init_auth never ran here
    monkeypatch.setattr(
        asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True),
        raising=False,
    )
    with pytest.raises(ScopeError) as exc_info:
        require_scope_asgi(
            _request({"agent_id": "a", "scopes": ["session:write"], "key_id": "k1"}),
            "admin",
        )
    assert exc_info.value.status_code == 403


class TestRequireScopeAsgi:
    def test_allows_when_scope_present(self, auth_enabled):
        identity = require_scope_asgi(_request({"agent_id": "a", "scopes": ["relay:write"], "key_id": "k1"}), "relay:write")
        assert identity["agent_id"] == "a"

    def test_allows_wildcard_scope(self, auth_enabled):
        identity = require_scope_asgi(_request({"agent_id": "admin", "scopes": ["*"], "key_id": "k1"}), "relay:write")
        assert identity["agent_id"] == "admin"

    def test_raises_scope_error_when_missing_scope(self, auth_enabled):
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request({"agent_id": "a", "scopes": ["relay:read"], "key_id": "k1"}), "relay:write")
        assert exc_info.value.status_code == 403

    def test_passes_through_anonymous_when_auth_disabled(self):
        # No identity attached to scope['state'] — mirrors FirekeepKeyAuthMiddleware
        # never having run because auth is disabled.
        identity = require_scope_asgi(_request(None), "relay:write")
        assert identity["agent_id"] == "anonymous"
        assert "*" in identity["scopes"]

    def test_raises_when_auth_enabled_but_no_identity_attached(self, auth_enabled):
        # Regression test: auth IS enabled but no identity was attached
        # (e.g. route under FirekeepKeyAuthMiddleware's skip_paths, or the
        # middleware isn't wired into this app). Must fail closed with 401,
        # not silently return anonymous wildcard identity.
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request(None), "relay:write")
        assert exc_info.value.status_code == 401

    def test_denies_when_scopes_key_entirely_absent(self, auth_enabled):
        # identity dict with no "scopes" key at all — identity.get("scopes", [])
        # should default to [] and deny.
        with pytest.raises(ScopeError):
            require_scope_asgi(_request({"agent_id": "a", "key_id": "k1"}), "relay:write")
