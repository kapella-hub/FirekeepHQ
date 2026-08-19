"""Dex-prefixed vault keys are MEMBER secrets (Maildex e2e finding, 2026-08-19).

The admin-only write posture is correct for ops secrets and fatal for
connector dexes: `maildex add` verified the mailbox, printed success, and
stored nothing, because the member key could not write. These tests drive the
route BODIES with injected identities (the scope-gate dependency itself is
covered by test_vault_read_scope's real-gate suite): a dex-scoped member can
store/read/delete their own `maildex.<id>` secret; a teammate with vault:read
cannot read, overwrite, list, or delete it; ordinary keys keep the admin-only
write posture unchanged.
"""
import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import vault.api as vault_api
from vault.store import init_vault

ALICE = {"member_id": "m-alice", "scopes": ["vault:read", "dex:maildex"], "authenticated": True}
BOB = {"member_id": "m-bob", "scopes": ["vault:read", "dex:maildex"], "authenticated": True}
ADMIN = {"member_id": "m-admin", "scopes": ["admin"], "authenticated": True}
NARROW = {"member_id": "m-narrow", "scopes": ["vault:read"], "authenticated": True}


@pytest.fixture
def app(monkeypatch):
    init_vault(fakeredis.aioredis.FakeRedis(decode_responses=True), Fernet.generate_key().decode())
    application = FastAPI()
    application.state.identity = ADMIN

    def fake_gate(*_scopes):
        async def dep() -> dict:
            return application.state.identity
        return dep

    monkeypatch.setattr(vault_api, "require_any_scope", fake_gate)
    monkeypatch.setattr(vault_api, "require_scope", lambda *_s: fake_gate())
    application.include_router(vault_api.create_vault_router())
    return application


@pytest.fixture
def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), app


async def _store(c, key, value="s3cret"):
    return await c.post("/vault/secrets", json={"key": key, "value": value})


class TestMemberDexKeys:
    @pytest.mark.asyncio
    async def test_dex_scoped_member_stores_and_reads_their_own(self, client):
        c, app = client
        app.state.identity = ALICE
        async with c:
            assert (await _store(c, "maildex.acct1")).status_code == 200
            r = await c.get("/vault/secrets/maildex.acct1")
            assert r.status_code == 200 and r.json()["value"] == "s3cret"

    @pytest.mark.asyncio
    async def test_member_cannot_store_an_ordinary_key(self, client):
        c, app = client
        app.state.identity = ALICE
        async with c:
            r = await _store(c, "prod-database-password")
            assert r.status_code == 403 and "dex prefix" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_unknown_dex_prefix_is_an_ordinary_key(self, client):
        c, app = client
        app.state.identity = ALICE
        async with c:
            assert (await _store(c, "chatdex.acct1")).status_code == 403

    @pytest.mark.asyncio
    async def test_teammate_cannot_read_overwrite_delete_or_list_it(self, client):
        c, app = client
        async with c:
            app.state.identity = ALICE
            assert (await _store(c, "maildex.acct1")).status_code == 200
            app.state.identity = BOB
            assert (await c.get("/vault/secrets/maildex.acct1")).status_code == 404
            r = await _store(c, "maildex.acct1", value="evil")
            assert r.status_code == 403 and "another member" in r.json()["detail"]
            assert (await c.delete("/vault/secrets/maildex.acct1")).status_code == 404
            listed = (await c.get("/vault/secrets")).json()["secrets"]
            assert all(s["key"] != "maildex.acct1" for s in listed)
            # And Alice's own view still has it, value intact.
            app.state.identity = ALICE
            assert (await c.get("/vault/secrets/maildex.acct1")).json()["value"] == "s3cret"

    @pytest.mark.asyncio
    async def test_owner_can_overwrite_and_delete_their_own(self, client):
        c, app = client
        app.state.identity = ALICE
        async with c:
            await _store(c, "maildex.acct1")
            assert (await _store(c, "maildex.acct1", value="rotated")).status_code == 200
            assert (await c.delete("/vault/secrets/maildex.acct1")).status_code == 200
            assert (await c.get("/vault/secrets/maildex.acct1")).status_code == 404

    @pytest.mark.asyncio
    async def test_scope_without_the_named_dex_is_refused(self, client):
        c, app = client
        app.state.identity = NARROW
        async with c:
            assert (await _store(c, "maildex.acct1")).status_code == 403

    @pytest.mark.asyncio
    async def test_admin_sees_and_manages_everything(self, client):
        c, app = client
        async with c:
            app.state.identity = ALICE
            await _store(c, "maildex.acct1")
            app.state.identity = ADMIN
            assert (await c.get("/vault/secrets/maildex.acct1")).status_code == 200
            listed = (await c.get("/vault/secrets")).json()["secrets"]
            assert any(s["key"] == "maildex.acct1" for s in listed)
            assert (await c.delete("/vault/secrets/maildex.acct1")).status_code == 200
