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
            "eval:read",
            # Vault READ, split off admin 2026-07-29 so a teammate's agent can
            # retrieve a credential without holding a key-minting scope. WRITE and
            # DELETE on the vault stay admin-only.
            "vault:read",
            # Per-dex reserved-prefix write scope (Docdex §4.3): a `docdex:`
            # corpus source is writable only by a key carrying this (or admin).
            "dex:docdex",
            "dex:maildex",
            "admin",
        }
        assert SCOPES == expected

    def test_scope_count(self):
        assert len(SCOPES) == 13


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
        assert "workspace_id" in _ANONYMOUS_IDENTITY
        assert "member_id" in _ANONYMOUS_IDENTITY
        assert "credential_id" in _ANONYMOUS_IDENTITY
        assert "agent_id" not in _ANONYMOUS_IDENTITY
        assert "scopes" in _ANONYMOUS_IDENTITY
        assert "authenticated" in _ANONYMOUS_IDENTITY

    def test_no_wildcard_no_admin_and_no_secret_reading(self):
        """The anonymous scope set withholds admin AND vault:read — never ["*"].

        vault:read is withheld for the same reason admin is: it decrypts secrets,
        and ANONYMOUS_SCOPES is derived from SCOPES, so a new scope is granted to
        unauthenticated callers automatically unless subtracted by name."""
        scopes = set(_ANONYMOUS_IDENTITY["scopes"])
        assert "*" not in scopes
        assert "admin" not in scopes
        assert "vault:read" not in scopes
        assert not any(s.startswith("dex:") for s in scopes)  # member-owned vault writes need identity
        assert scopes == {s for s in SCOPES if not s.startswith("dex:")} - {"admin", "vault:read"}
