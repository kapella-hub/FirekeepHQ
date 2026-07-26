"""SP1a §7 MANDATORY test: confused-deputy closure on the cortex-mcp proxy.

A non-admin teammate calling the vault_retrieve MCP tool must get 403 —
proving the proxy presents the CALLER's key (not a server-held key) to the
REST layer's require_scope("admin"). This is the test that would have caught
the naive give-the-proxy-an-admin-key fix.

Also proves the internal service key (memory:write/session:read/eval:read/eval:write)
is not over-privileged: it passes ungated core routes but 403s on vault and
key minting.
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI

import app.mcp_server as mcp_mod
from app.mcp_server import _CallerKeyAuth, vault_retrieve
from auth import keys

NON_ADMIN_SCOPES = sorted(keys.SCOPES - {"admin"})
# bootstrap-keys.sh mints the internal key with EXACTLY this set (spec §4.2).
INTERNAL_KEY_SCOPES = ["memory:write", "session:read", "eval:read", "eval:write"]


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def auth_env(redis):
    await keys.init_auth(redis_client=redis, enabled=True)
    teammate = await keys.create_key("teammate", NON_ADMIN_SCOPES)
    owner = await keys.create_key("owner", ["admin"])
    internal = await keys.create_key("internal-service", INTERNAL_KEY_SCOPES)
    yield {
        "teammate": teammate["api_key"],
        "admin": owner["api_key"],
        "internal": internal["api_key"],
    }
    await keys.init_auth(redis_client=None, enabled=False)


@pytest.fixture
def stub_cortex_api(redis):
    """Stub cortex-api: the REAL vault router (require_scope('admin') on all
    four endpoints) + an initialized vault store."""
    from vault import store as vault_store
    from vault.api import create_vault_router

    api = FastAPI()
    api.include_router(create_vault_router())
    vault_store.init_vault(redis, Fernet.generate_key().decode())
    return api


@pytest.fixture
def proxy_client(stub_cortex_api):
    """Wire the cortex-mcp shared client at the stub REST API with the
    production _CallerKeyAuth attached — exactly what _get_client builds."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stub_cortex_api),
        base_url="http://cortex-api",
        auth=_CallerKeyAuth(),
    )
    mcp_mod._client = client
    yield client
    mcp_mod._client = None


def _fake_headers(key: str | None):
    def _get_http_headers(*_a, **_k):
        return {"x-api-key": key} if key else {}

    return _get_http_headers


class TestConfusedDeputyClosure:
    @pytest.mark.asyncio
    async def test_non_admin_caller_gets_403_from_vault_retrieve(
        self, monkeypatch, proxy_client, auth_env, redis
    ):
        """THE load-bearing SP1a test (spec §7)."""
        monkeypatch.setattr(
            mcp_mod, "get_http_headers", _fake_headers(auth_env["teammate"])
        )
        result = await vault_retrieve(key="prod-db-password")
        assert "403" in result  # _format_error: "Error: API returned 403."

    @pytest.mark.asyncio
    async def test_admin_caller_key_reaches_vault(
        self, monkeypatch, proxy_client, auth_env, redis
    ):
        from vault.store import store_secret

        await store_secret(key="prod-db-password", value="hunter2")
        monkeypatch.setattr(
            mcp_mod, "get_http_headers", _fake_headers(auth_env["admin"])
        )
        result = await vault_retrieve(key="prod-db-password")
        assert "hunter2" in result

    @pytest.mark.asyncio
    async def test_proxy_holds_no_static_key(
        self, monkeypatch, proxy_client, auth_env
    ):
        """No caller header + no FIREKEEP_INTERNAL_KEY => the proxied request
        carries no X-API-Key at all: deputy eliminated, not re-pointed."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(mcp_mod, "get_http_headers", _fake_headers(None))
        mock_settings = MagicMock()
        mock_settings.FIREKEEP_INTERNAL_KEY = None
        monkeypatch.setattr(mcp_mod, "get_settings", lambda: mock_settings)
        result = await vault_retrieve(key="prod-db-password")
        # Keyless against require_scope("admin") with auth enabled -> 401.
        assert "Authentication failed" in result


class TestCallerKeyAuth:
    def test_forwards_caller_key(self, monkeypatch):
        monkeypatch.setattr(
            mcp_mod, "get_http_headers", _fake_headers("nxs_caller")
        )
        request = httpx.Request("GET", "http://cortex-api/vault/secrets/x")
        sent = next(_CallerKeyAuth().auth_flow(request))
        assert sent.headers["X-API-Key"] == "nxs_caller"

    def test_falls_back_to_internal_key(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(mcp_mod, "get_http_headers", _fake_headers(None))
        ms = MagicMock()
        ms.FIREKEEP_INTERNAL_KEY = "nxs_internal"
        monkeypatch.setattr(mcp_mod, "get_settings", lambda: ms)
        request = httpx.Request("POST", "http://cortex-api/memory/learn")
        sent = next(_CallerKeyAuth().auth_flow(request))
        assert sent.headers["X-API-Key"] == "nxs_internal"

    def test_no_key_when_neither_present(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(mcp_mod, "get_http_headers", _fake_headers(None))
        ms = MagicMock()
        ms.FIREKEEP_INTERNAL_KEY = None
        monkeypatch.setattr(mcp_mod, "get_settings", lambda: ms)
        request = httpx.Request("GET", "http://cortex-api/health")
        sent = next(_CallerKeyAuth().auth_flow(request))
        assert "x-api-key" not in sent.headers


class TestInternalKeyNotOverprivileged:
    """Spec §7 internal-key path: memory:write-scoped key passes ungated core
    routes but cannot read vault or mint keys (direct REST, as the bridge
    distiller calls it — no proxy involved)."""

    def _mini_app(self, redis) -> FastAPI:
        from auth.api import create_auth_router
        from auth.asgi import FirekeepKeyAuthMiddleware
        from vault.api import create_vault_router

        app = FastAPI()
        app.include_router(create_auth_router())
        app.include_router(create_vault_router())

        @app.post("/memory/learn")  # stand-in: production route has no require_scope
        async def learn_stub() -> dict:
            return {"status": "ok"}

        app.add_middleware(
            FirekeepKeyAuthMiddleware,
            enabled=True,
            redis_url="redis://unused/7",
            redis_client=redis,
        )
        return app

    def _client(self, app):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    @pytest.mark.asyncio
    async def test_internal_key_passes_memory_learn(self, redis, auth_env):
        async with self._client(self._mini_app(redis)) as c:
            resp = await c.post(
                "/memory/learn", headers={"X-API-Key": auth_env["internal"]}
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_internal_key_403_on_vault(self, redis, auth_env):
        async with self._client(self._mini_app(redis)) as c:
            resp = await c.get(
                "/vault/secrets/prod-db-password",
                headers={"X-API-Key": auth_env["internal"]},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_internal_key_403_on_auth_keys(self, redis, auth_env):
        async with self._client(self._mini_app(redis)) as c:
            resp = await c.post(
                "/auth/keys",
                headers={"X-API-Key": auth_env["internal"]},
                json={"agent_id": "sneaky", "scopes": ["admin"]},
            )
        assert resp.status_code == 403
