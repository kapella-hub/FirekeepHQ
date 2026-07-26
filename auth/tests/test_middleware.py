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


class TestRequireScope:
    def test_returns_callable(self):
        dep = require_scope("memory:read")
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_pass_through_when_disabled(self):
        """When AUTH_ENABLED=False, require_scope returns anonymous identity."""
        from unittest.mock import MagicMock

        dep = require_scope("admin")
        mock_request = MagicMock()

        result = await dep(mock_request)
        assert result == _ANONYMOUS_IDENTITY
        assert result["authenticated"] is False
        assert "*" in result["scopes"]


class TestAnonymousIdentity:
    def test_structure(self):
        assert "agent_id" in _ANONYMOUS_IDENTITY
        assert "scopes" in _ANONYMOUS_IDENTITY
        assert "authenticated" in _ANONYMOUS_IDENTITY

    def test_wildcard_scope(self):
        assert "*" in _ANONYMOUS_IDENTITY["scopes"]
