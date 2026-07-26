"""Tests for auth middleware — key generation, hashing, scope validation."""

import pytest

from auth.middleware import (
    SCOPES,
    _ANONYMOUS_IDENTITY,
    _hash_key,
    generate_api_key,
    require_scope,
)


class TestKeyGeneration:
    def test_format(self):
        key = generate_api_key()
        assert key.startswith("nxs_")
        assert len(key) == 52  # "nxs_" + 48 hex chars

    def test_uniqueness(self):
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100  # All unique

    def test_hash_deterministic(self):
        key = "nxs_test123"
        h1 = _hash_key(key)
        h2 = _hash_key(key)
        assert h1 == h2

    def test_hash_different_keys(self):
        h1 = _hash_key("nxs_aaa")
        h2 = _hash_key("nxs_bbb")
        assert h1 != h2

    def test_hash_is_sha256(self):
        h = _hash_key("test")
        assert len(h) == 64  # SHA-256 hex = 64 chars


class TestScopes:
    def test_all_scopes_defined(self):
        expected = {
            "memory:read", "memory:write",
            "session:read", "session:write",
            "replay:read", "eval:write",
            "relay:read", "relay:write",
            "eval:read", "admin",
        }
        assert SCOPES == expected

    def test_scope_count(self):
        assert len(SCOPES) == 10


@pytest.fixture
def auth_disabled(monkeypatch):
    """Pin require_scope's enable flag EXPLICITLY.

    require_scope reads auth.keys._AUTH_ENABLED, which only init_auth() sets.
    It happens to default False, but a disabled-path test that leans on that
    default would go quiet the day the default moves (AuthSettings.ENABLED is
    already flipping to true for audit blocker 7).
    """
    from auth import keys as _keys
    monkeypatch.setattr(_keys, "_AUTH_ENABLED", False)


class TestRequireScope:
    def test_returns_callable(self):
        dep = require_scope("memory:read")
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_pass_through_when_disabled(self, auth_disabled):
        """AUTH_ENABLED=False: a NON-admin scope still passes through as the
        anonymous identity — a single user with no keys keeps working."""
        from unittest.mock import MagicMock

        dep = require_scope("memory:read")
        mock_request = MagicMock()

        result = await dep(mock_request)
        assert result == _ANONYMOUS_IDENTITY
        assert result["authenticated"] is False

    @pytest.mark.asyncio
    async def test_admin_refused_when_disabled(self, auth_disabled):
        """AUTH_ENABLED=False: 'admin' is refused (audit blocker 7). It used to
        pass — require_scope returned the anonymous identity without ever
        looking at the requested scope, so /vault/* and /auth/keys were open to
        anyone who could reach the port."""
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        dep = require_scope("admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(MagicMock())
        assert exc_info.value.status_code == 403
        # The operator meets this on a default box — it must name the setting.
        assert "AUTH_ENABLED" in exc_info.value.detail


class TestAnonymousIdentity:
    def test_structure(self):
        assert "agent_id" in _ANONYMOUS_IDENTITY
        assert "scopes" in _ANONYMOUS_IDENTITY
        assert "authenticated" in _ANONYMOUS_IDENTITY

    def test_no_wildcard_and_no_admin(self):
        """The anonymous scope set is every scope EXCEPT admin — never ["*"]."""
        scopes = set(_ANONYMOUS_IDENTITY["scopes"])
        assert "*" not in scopes
        assert "admin" not in scopes
        assert scopes == SCOPES - {"admin"}
