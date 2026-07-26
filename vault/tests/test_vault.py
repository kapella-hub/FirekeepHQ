"""Tests for vault module — config, key validation, encryption, store operations, REST API."""

import asyncio

import fakeredis.aioredis
import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from vault.config import VaultSettings, generate_vault_key
from vault.store import (
    _validate_key_name,
    delete_secret,
    init_vault,
    list_secrets,
    retrieve_secret,
    store_secret,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _test_key() -> str:
    return Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# TestVaultConfig
# ---------------------------------------------------------------------------


class TestVaultConfig:
    def test_default_settings(self):
        s = VaultSettings(ENABLED=True, KEY="", REDIS_URL="redis://localhost:6379/7")
        assert s.ENABLED is True
        assert s.KEY == ""
        assert "/7" in s.REDIS_URL

    def test_generate_vault_key(self):
        key = generate_vault_key()
        # Must be usable as a Fernet key without raising
        f = Fernet(key.encode())
        assert f is not None


# ---------------------------------------------------------------------------
# TestKeyNameValidation
# ---------------------------------------------------------------------------


class TestKeyNameValidation:
    def test_valid_names(self):
        for name in ["my-secret", "api_key.prod", "VPS_IP", "a", "a" * 200]:
            _validate_key_name(name)  # should not raise

    def test_invalid_empty(self):
        with pytest.raises(ValueError):
            _validate_key_name("")

    def test_invalid_too_long(self):
        with pytest.raises(ValueError):
            _validate_key_name("a" * 201)

    def test_invalid_chars(self):
        for name in ["my secret", "key/path", "key;drop"]:
            with pytest.raises(ValueError):
                _validate_key_name(name)


# ---------------------------------------------------------------------------
# TestEncryption
# ---------------------------------------------------------------------------


class TestEncryption:
    def test_roundtrip(self):
        key = _test_key()
        f = Fernet(key.encode())
        plaintext = "super-secret-value-123"
        ct = f.encrypt(plaintext.encode())
        assert f.decrypt(ct).decode() == plaintext

    def test_different_ciphertexts(self):
        key = _test_key()
        f = Fernet(key.encode())
        plaintext = b"same-value"
        ct1 = f.encrypt(plaintext)
        ct2 = f.encrypt(plaintext)
        assert ct1 != ct2  # Fernet uses random IV

    def test_invalid_key_init(self):
        r = _make_redis()
        with pytest.raises(ValueError):
            init_vault(r, "not-a-valid-fernet-key")


# ---------------------------------------------------------------------------
# TestStoreOperations
# ---------------------------------------------------------------------------


class TestStoreOperations:
    @pytest.fixture(autouse=True)
    def setup_vault(self):
        """Initialize vault with fakeredis + fresh Fernet key for each test."""
        import vault.store as _mod

        self.redis = _make_redis()
        self.key = _test_key()
        init_vault(self.redis, self.key)
        yield
        # Reset module-level state
        _mod._redis = None
        _mod._fernet = None

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self):
        meta = await store_secret("db-pass", "s3cret!", description="DB password", category="db")
        assert meta["key"] == "db-pass"
        assert meta["description"] == "DB password"
        assert meta["category"] == "db"
        assert "created_at" in meta
        assert "updated_at" in meta

        result = await retrieve_secret("db-pass")
        assert result is not None
        assert result["value"] == "s3cret!"
        assert result["key"] == "db-pass"
        assert result["description"] == "DB password"
        assert result["category"] == "db"

    @pytest.mark.asyncio
    async def test_retrieve_nonexistent(self):
        result = await retrieve_secret("no-such-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_secrets(self):
        await store_secret("key-a", "val-a")
        await store_secret("key-b", "val-b")
        await store_secret("key-c", "val-c")

        secrets = await list_secrets()
        assert len(secrets) == 3
        keys = {s["key"] for s in secrets}
        assert keys == {"key-a", "key-b", "key-c"}
        # Metadata only — no 'value' field
        for s in secrets:
            assert "value" not in s

    @pytest.mark.asyncio
    async def test_list_by_category(self):
        await store_secret("prod-db", "v1", category="database")
        await store_secret("prod-api", "v2", category="api")
        await store_secret("staging-db", "v3", category="database")

        db_secrets = await list_secrets(category="database")
        assert len(db_secrets) == 2
        assert all(s["category"] == "database" for s in db_secrets)

        api_secrets = await list_secrets(category="api")
        assert len(api_secrets) == 1
        assert api_secrets[0]["key"] == "prod-api"

    @pytest.mark.asyncio
    async def test_delete_secret(self):
        await store_secret("to-delete", "temporary")
        assert await delete_secret("to-delete") is True
        assert await retrieve_secret("to-delete") is None

    @pytest.mark.asyncio
    async def test_upsert(self):
        meta1 = await store_secret("upsert-key", "first-value")
        first_updated = meta1["updated_at"]

        # Small delay to ensure updated_at differs
        await asyncio.sleep(0.01)

        meta2 = await store_secret("upsert-key", "second-value")
        assert meta2["updated_at"] != first_updated

        result = await retrieve_secret("upsert-key")
        assert result is not None
        assert result["value"] == "second-value"

    @pytest.mark.asyncio
    async def test_store_not_initialized(self):
        import vault.store as _mod

        _mod._redis = None
        _mod._fernet = None

        with pytest.raises(RuntimeError):
            await store_secret("key", "value")


# ---------------------------------------------------------------------------
# TestVaultRouter
# ---------------------------------------------------------------------------


class TestVaultRouter:
    @pytest.fixture(autouse=True)
    def setup_app(self):
        """Build a test FastAPI app with vault router backed by fakeredis."""
        import vault.store as _mod
        from fastapi import FastAPI
        from vault.api import create_vault_router

        self.redis = _make_redis()
        self.key = _test_key()
        init_vault(self.redis, self.key)

        app = FastAPI()
        # Build the router with the admin gate stubbed out, so these tests
        # exercise vault CRUD rather than authorization.
        #
        # This used to read "Bypass auth — require_scope returns anonymous
        # identity", and it needed no stub because that was literally true: on
        # the AUTH_ENABLED=false path require_scope returned the anonymous
        # identity WITHOUT comparing scopes, so every admin route answered 200
        # to anyone. That is audit blocker 7 — it is how GET /vault/secrets
        # served 12 real secrets off a public VPS. The comment was describing
        # the vulnerability as a testing convenience.
        #
        # Patch `vault.api.require_scope`, not `auth.middleware.require_scope`:
        # vault/api.py:11 imported the name at module load, so it holds its own
        # reference. The patch must be live while the router is CONSTRUCTED,
        # because the dependency is captured at decoration time.
        # The gate itself is covered by test_vault_routes_refuse_anonymous below.
        with patch("vault.api.require_scope",
                   lambda scope: (lambda: {"agent_id": "test-admin",
                                           "scopes": ["*"], "authenticated": True})):
            router = create_vault_router()
        app.include_router(router)
        self.app = app
        yield
        _mod._redis = None
        _mod._fernet = None

    @pytest.mark.asyncio
    async def test_store_and_get(self):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/vault/secrets", json={
                "key": "my-api-key",
                "value": "sk-12345",
                "description": "API key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["key"] == "my-api-key"
            assert "value" not in data  # metadata only

            resp = await client.get("/vault/secrets/my-api-key")
            assert resp.status_code == 200
            data = resp.json()
            assert data["value"] == "sk-12345"
            assert data["key"] == "my-api-key"

    @pytest.mark.asyncio
    async def test_list_secrets(self):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for name in ["sec-a", "sec-b", "sec-c"]:
                await client.post("/vault/secrets", json={"key": name, "value": "v"})

            resp = await client.get("/vault/secrets")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 3
            assert len(data["secrets"]) == 3

    @pytest.mark.asyncio
    async def test_delete(self):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/vault/secrets", json={"key": "del-me", "value": "bye"})

            resp = await client.delete("/vault/secrets/del-me")
            assert resp.status_code == 200

            resp = await client.get("/vault/secrets/del-me")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_not_found(self):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/vault/secrets/nonexistent")
            assert resp.status_code == 404


class TestVaultAdminGate:
    """The gate TestVaultRouter stubs out must actually refuse.

    Regression guard for audit blocker 7. Asserting only that
    keys.ANONYMOUS_SCOPES excludes "admin" would NOT catch a regression here:
    the pre-fix code never consulted that list on the disabled path.
    """

    @pytest.mark.asyncio
    async def test_vault_routes_refuse_anonymous(self):
        from fastapi import FastAPI
        from vault.api import create_vault_router

        from auth import keys as _keys

        assert _keys._AUTH_ENABLED is False, "this test is about the auth-DISABLED path"

        app = FastAPI()
        app.include_router(create_vault_router())  # the REAL gate
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for method, path in (
                ("get", "/vault/secrets"),
                ("get", "/vault/secrets/anything"),
                ("post", "/vault/secrets"),
                ("delete", "/vault/secrets/anything"),
            ):
                kwargs = {"json": {"key": "k", "value": "v"}} if method == "post" else {}
                resp = await getattr(client, method)(path, **kwargs)
                assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"
