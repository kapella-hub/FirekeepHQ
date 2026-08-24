"""SP1a §7 consolidation: one validator on the whole app; require_scope refines.

Composes a mini FastAPI app from the REAL auth + vault routers and a stand-in
for an ungated core route (/memory/learn has no require_scope in production),
wrapped by the REAL FirekeepKeyAuthMiddleware — the exact class app.main now
registers in place of the legacy APIKeyMiddleware. (The production app binds
its middleware config at import time from env, so enabled=True composition is
tested on a mini app; the production wiring is pinned in TestMainWiring.)
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth import keys
from auth.api import create_auth_router
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.keys import ENROLLABLE_SCOPES
from vault.api import create_vault_router

# Mirrors the NON_ADMIN_SCOPES constant in deploy/firekeep-admin (teammate keys).
# ENROLLABLE_SCOPES, not SCOPES - {"admin"}: the latter would now include the
# service-only eval:grade scope, which create_key rejects outright.
NON_ADMIN_SCOPES = sorted(ENROLLABLE_SCOPES)

SKIP_PATHS = ("/health", "/version", "/docs", "/redoc", "/openapi.json", "/dashboard")

# Mirrors app.main's production CORS config (allow_origins from Settings.CORS_ORIGINS).
CORS_ORIGIN = "http://localhost:3000"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def auth_env(redis):
    """Enable auth globals (require_scope) and seed non-admin + admin keys."""
    await keys.init_auth(redis_client=redis, enabled=True)
    non_admin = await keys.create_key("teammate", NON_ADMIN_SCOPES)
    admin = await keys.create_key("owner", ["admin"])
    yield {"non_admin": non_admin["api_key"], "admin": admin["api_key"]}
    await keys.init_auth(redis_client=None, enabled=False)


def _mini_cortex(redis) -> FastAPI:
    app = FastAPI()
    app.include_router(create_auth_router())
    app.include_router(create_vault_router())

    @app.post("/memory/learn")  # stand-in: production route has no require_scope
    async def learn_stub() -> dict:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Mirror app.main's registration order: auth first, CORS last. add_middleware
    # PREPENDS, so CORS ends up OUTERMOST — a keyless preflight OPTIONS is
    # answered by CORS and never reaches (and never gets 401'd by) the auth gate.
    app.add_middleware(
        FirekeepKeyAuthMiddleware,
        enabled=True,
        redis_url="redis://unused/7",
        redis_client=redis,
        skip_paths=SKIP_PATHS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[CORS_ORIGIN],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "PATCH"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-Id"],
    )
    return app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestConsolidation:
    @pytest.mark.asyncio
    async def test_non_admin_key_passes_ungated_core_route(self, redis, auth_env):
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.post(
                "/memory/learn", headers={"X-API-Key": auth_env["non_admin"]}
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_key_401_on_core_route(self, redis, auth_env):
        """The gap: /memory/learn was reachable keyless even with auth on."""
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.post("/memory/learn")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_key_can_READ_the_vault(self, redis, auth_env):
        """Changed 2026-07-29 with the vault:read split, and the change is the point.

        This asserted 403 while every vault route required admin. A teammate's
        agent asked to "deploy to my vps" therefore could not read the credential
        it needed, and the only workaround was issuing admin keys -- which also
        grant key minting. Reading a secret you were meant to have is ordinary
        work, so the teammate scope set now carries vault:read.

        Asserting NOT-403 rather than 200: the vault backend is unconfigured in
        this mini app, so a permitted read reaches it and returns 503. Passing the
        GATE is what this test is about; test_non_admin_key_403_on_vault_WRITE
        below covers the half that stayed admin-only."""
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.get(
                "/vault/secrets/db-pass", headers={"X-API-Key": auth_env["non_admin"]}
            )
        assert resp.status_code != 403, (
            "a teammate key must pass the vault READ gate; it carries vault:read"
        )

    @pytest.mark.asyncio
    async def test_non_admin_key_403_on_vault_WRITE(self, redis, auth_env):
        """The asymmetry. Creating or destroying a shared credential is
        administration; a read scope must not confer it."""
        async with _client(_mini_cortex(redis)) as c:
            store = await c.post(
                "/vault/secrets",
                headers={"X-API-Key": auth_env["non_admin"]},
                json={"key": "k", "value": "v"},
            )
            delete = await c.delete(
                "/vault/secrets/db-pass", headers={"X-API-Key": auth_env["non_admin"]}
            )
        assert store.status_code == 403, "a teammate must not be able to STORE a secret"
        assert delete.status_code == 403, "a teammate must not be able to DELETE a secret"

    @pytest.mark.asyncio
    async def test_non_admin_key_403_on_auth_keys(self, redis, auth_env):
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.post(
                "/auth/keys",
                headers={"X-API-Key": auth_env["non_admin"]},
                json={"agent_id": "x", "scopes": ["replay:read"]},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_key_can_mint_keys(self, redis, auth_env):
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.post(
                "/auth/keys",
                headers={"X-API-Key": auth_env["admin"]},
                json={"agent_id": "newbie", "scopes": ["replay:read"]},
            )
        assert resp.status_code == 200
        assert resp.json()["api_key"].startswith("nxs_")

    @pytest.mark.asyncio
    async def test_admin_can_rename_device_metadata_but_blank_is_rejected(
        self, redis, auth_env
    ):
        created = await keys.create_key("device-a", ["memory:read"])
        credential_id = created["credential_id"]
        async with _client(_mini_cortex(redis)) as c:
            renamed = await c.patch(
                f"/auth/keys/{credential_id}",
                headers={"X-API-Key": auth_env["admin"]},
                json={"label": "Alice's laptop"},
            )
            blank = await c.patch(
                f"/auth/keys/{credential_id}",
                headers={"X-API-Key": auth_env["admin"]},
                json={"label": "   "},
            )
        assert renamed.status_code == 200
        assert renamed.json()["label"] == "Alice's laptop"
        assert blank.status_code == 400

    @pytest.mark.asyncio
    async def test_ambiguous_key_id_is_409_and_deletes_nothing(self, redis, auth_env):
        """A well-formed request against ambiguous server state is a 409, not a
        500 (which reads as the caller's fault) and not a 200 (which is the bug
        this whole change removes: deleting one of two and reporting success)."""
        import json as _json

        shared = "a" * 16
        hash_a, hash_b = shared + "1" * 48, shared + "2" * 48
        for h, who in ((hash_a, "alice"), (hash_b, "bob")):
            await redis.hset(f"auth:key:{h}", mapping={
                "agent_id": who,
                "scopes": _json.dumps(["memory:read"]),
                "created_at": "2026-07-30T00:00:00+00:00",
                "key_id": shared,
            })
        await redis.zadd("auth:key_index", {shared: 1.0})

        async with _client(_mini_cortex(redis)) as c:
            resp = await c.delete(
                f"/auth/keys/{shared}",
                headers={"X-API-Key": auth_env["admin"]},
            )

        assert resp.status_code == 409
        assert shared in resp.json()["detail"]
        assert await redis.exists(f"auth:key:{hash_a}") == 1
        assert await redis.exists(f"auth:key:{hash_b}") == 1

    @pytest.mark.asyncio
    async def test_health_skipped(self, redis, auth_env):
        async with _client(_mini_cortex(redis)) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cors_preflight_bypasses_auth_but_post_still_gated(self, redis, auth_env):
        """CORS is outermost: a keyless preflight OPTIONS is answered by CORS
        (200 + allow-origin), NOT 401'd by auth — while real POSTs stay gated.

        Regression guard for the middleware-ordering bug: with auth wrapping
        CORS, the browser preflight (no X-API-Key) got 401'd before CORS could
        attach its headers, blocking every valid-key cross-origin client.
        """
        async with _client(_mini_cortex(redis)) as c:
            # Preflight: no X-API-Key, must be answered by CORS, not blocked by auth.
            preflight = await c.options(
                "/memory/learn",
                headers={
                    "Origin": CORS_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers.get("access-control-allow-origin") == CORS_ORIGIN

            # Real request without a key is still rejected — reorder opened no hole.
            no_key = await c.post("/memory/learn")
            assert no_key.status_code == 401

            # Real request with a valid key still passes through auth.
            with_key = await c.post(
                "/memory/learn", headers={"X-API-Key": auth_env["non_admin"]}
            )
            assert with_key.status_code == 200


class TestMainWiring:
    def test_legacy_middleware_gone_and_validator_registered(self):
        import app.main as main_mod

        names = [m.cls.__name__ for m in main_mod.app.user_middleware]
        assert "APIKeyMiddleware" not in names
        assert "FirekeepKeyAuthMiddleware" in names

    def test_validator_skip_list(self):
        import app.main as main_mod

        mw = next(
            m for m in main_mod.app.user_middleware
            if m.cls.__name__ == "FirekeepKeyAuthMiddleware"
        )
        # Hardcoded literal (not imported from app.main) so this test actually
        # pins production wiring instead of trivially agreeing with itself.
        assert mw.kwargs["skip_paths"] == (
            "/health", "/version", "/docs", "/redoc", "/openapi.json",
        )

    def test_dashboard_is_exact_skip_not_prefix(self):
        """Regression guard for the unauthenticated-dashboard hole
        (2026-07-26): /dashboard must be an EXACT skip, never a prefix —
        a prefix match would silently exempt /dashboard/api/memories,
        which returned real memory content to any unauthenticated caller."""
        import app.main as main_mod

        mw = next(
            m for m in main_mod.app.user_middleware
            if m.cls.__name__ == "FirekeepKeyAuthMiddleware"
        )
        assert mw.kwargs["skip_exact_paths"] == (
            "/dashboard", "/dashboard/", "/enroll", "/enroll/anchor",
            "/members/invites/accept", "/members/invites/anchor",
        )
        assert "/dashboard" not in mw.kwargs["skip_paths"]

    def test_cors_is_outermost_of_auth(self):
        """user_middleware[0] is outermost (Starlette wraps in reverse of the
        list). CORS must sit outside auth so keyless preflights short-circuit at
        CORS instead of being 401'd by the auth gate.
        """
        import app.main as main_mod

        names = [m.cls.__name__ for m in main_mod.app.user_middleware]
        assert names.index("CORSMiddleware") < names.index("FirekeepKeyAuthMiddleware")

    def test_config_api_key_retired(self):
        from app.config import Settings

        assert "API_KEY" not in Settings.model_fields
        assert "LLM_API_KEY" in Settings.model_fields  # unrelated setting stays

    def test_auth_init_no_longer_fails_open(self):
        import inspect

        import app.main as main_mod

        src = inspect.getsource(main_mod)
        assert "Auth init failed (non-critical" not in src
        assert "FATAL-LOUD" in src  # marker comment on the new init block


@pytest.mark.asyncio
async def test_initialized_key_store_remains_usable_with_enforcement_off(redis):
    """The eval:grade route gate's fallback (app/evals/api.py _hint_authorized)
    depends on validate_key still consulting DB 7 when enforcement is disabled —
    cortex initializes the DB-7 auth client regardless of AUTH_ENABLED
    (app/main.py:714-724). A source inspection of main.py is not enough to pin
    that behavior; this exercises it end to end."""
    await keys.init_auth(redis_client=redis, enabled=True)
    created = await keys.create_key("auth-off-probe", ["eval:write"])
    await keys.init_auth(redis_client=redis, enabled=False)
    try:
        identity = await keys.validate_key(created["api_key"])
        assert identity is not None
        assert identity["credential_id"] == created["credential_id"]
        assert keys._AUTH_ENABLED is False
        assert keys._redis is redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
