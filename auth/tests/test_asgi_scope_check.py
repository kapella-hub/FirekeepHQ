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


@pytest.fixture
def auth_disabled(monkeypatch):
    """Pin disabled-ness EXPLICITLY rather than relying on the shipped default.

    AuthSettings.ENABLED's default is flipping to true (audit blocker 7), and
    AuthSettings also reads .env — a disabled-path test that leaves this
    implicit would silently start exercising the enabled path instead.
    """
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(
        asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=False),
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
            _request({"member_id": "a", "scopes": ["session:write"], "credential_id": "k1"}),
            "admin",
        )
    assert exc_info.value.status_code == 403


class TestRequireScopeAsgi:
    def test_allows_when_scope_present(self, auth_enabled):
        identity = require_scope_asgi(_request({"member_id": "a", "scopes": ["relay:write"], "credential_id": "k1"}), "relay:write")
        assert identity["member_id"] == "a"

    def test_allows_wildcard_scope(self, auth_enabled):
        identity = require_scope_asgi(_request({"member_id": "admin", "scopes": ["*"], "credential_id": "k1"}), "relay:write")
        assert identity["member_id"] == "admin"

    def test_raises_scope_error_when_missing_scope(self, auth_enabled):
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request({"member_id": "a", "scopes": ["relay:read"], "credential_id": "k1"}), "relay:write")
        assert exc_info.value.status_code == 403

    def test_passes_through_anonymous_when_auth_disabled(self, auth_disabled):
        # No identity attached to scope['state'] — mirrors FirekeepKeyAuthMiddleware
        # never having run because auth is disabled.
        identity = require_scope_asgi(_request(None), "relay:write")
        assert identity["member_id"] == "member-owner"
        assert "admin" not in identity["scopes"]
        assert "*" not in identity["scopes"]

    def test_admin_refused_for_anonymous_when_auth_disabled(self, auth_disabled):
        """Audit blocker 7, ASGI twin: the disabled path used to return the
        anonymous identity without consulting `scope`, leaving Bridge's
        admin-only POST /ops/distill-dlq/requeue open on an auth-off box."""
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request(None), "admin")
        assert exc_info.value.status_code == 403
        assert "AUTH_ENABLED" in exc_info.value.detail

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
            require_scope_asgi(_request({"member_id": "a", "credential_id": "k1"}), "relay:write")
