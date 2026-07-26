"""Defence in depth: /auth/* and /vault/* are not served when auth is off.

Audit blocker 7, part (c). The scope fix (auth/keys.py ANONYMOUS_SCOPES +
enforcement in require_scope) already denies these routes to an anonymous
caller. This second layer means a REGRESSION in that scope set — the exact
regression that leaked 12 real secrets — cannot on its own re-expose decrypted
secret reads and API-key minting, because the routes are not mounted at all.

The refusal must stay legible: a bare 404 on /vault/secrets reads like a broken
build, so the stand-in router 503s with the setting name and the fix.

These drive the REAL app.main._register_admin_surface_routers (the production
branch), not a reimplementation of it — mounting it on a bare FastAPI app,
which is all that stanza needs (it touches no app.state).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_mod


def _mount(monkeypatch, *, auth_enabled: bool) -> TestClient:
    """Run the production stanza with AuthSettings pinned either way.

    _register_admin_surface_routers does a LOCAL `from auth.config import
    get_auth_settings`, so patching the attribute on auth.config is what the
    call actually resolves. Pinned explicitly in BOTH directions: the shipped
    default is flipping to enabled, so neither premise may be implicit.
    """
    import auth.config as auth_config
    from auth.config import AuthSettings

    monkeypatch.setattr(
        auth_config, "get_auth_settings",
        lambda: AuthSettings(ENABLED=auth_enabled),
    )
    app = FastAPI()
    main_mod._register_admin_surface_routers(app)
    return TestClient(app)


class TestDisabledRefusesLegibly:
    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        return _mount(monkeypatch, auth_enabled=False)

    @pytest.mark.parametrize("path", [
        "/vault/secrets",
        "/vault/secrets/db-password",
        "/auth/keys",
        "/auth/scopes",
    ])
    def test_get_refused_with_503(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 503, path

    def test_key_minting_refused(self, client):
        """POST /auth/keys is the mint-yourself-an-admin-key route."""
        resp = client.post("/auth/keys", json={"agent_id": "x", "scopes": ["admin"]})
        assert resp.status_code == 503

    def test_secret_write_refused(self, client):
        resp = client.post("/vault/secrets", json={"key": "k", "value": "v"})
        assert resp.status_code == 503

    def test_secret_delete_refused(self, client):
        assert client.delete("/vault/secrets/k").status_code == 503

    def test_not_a_bare_404(self, client):
        """The operator-legibility requirement: the body has to say which
        setting turned this off and how to turn it back on."""
        detail = client.get("/vault/secrets").json()["detail"]
        assert "AUTH_ENABLED" in detail
        assert "bootstrap-keys.sh" in detail
        assert "vault" in detail.lower()

    def test_auth_refusal_explains_the_auth_surface_specifically(self, client):
        detail = client.get("/auth/keys").json()["detail"]
        assert "AUTH_ENABLED" in detail
        assert "key" in detail.lower()

    def test_real_routers_are_absent_from_the_route_table(self, client):
        """Not merely shadowed — the real handlers were never mounted."""
        paths = {r.path for r in client.app.routes}
        assert "/vault/secrets/{key}" not in paths
        assert "/auth/keys/{key_id}" not in paths


class TestEnabledStillServes:
    """The gate must not break the supported configuration."""

    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        return _mount(monkeypatch, auth_enabled=True)

    def test_real_routers_are_mounted(self, client):
        paths = {r.path for r in client.app.routes}
        assert "/vault/secrets/{key}" in paths
        assert "/auth/keys/{key_id}" in paths

    def test_requests_reach_require_scope_not_a_503_stand_in(self, client, monkeypatch):
        """With enforcement on and no key presented that is a 401 — proving the
        request got as far as the real dependency."""
        from auth import keys

        monkeypatch.setattr(keys, "_AUTH_ENABLED", True)
        assert client.get("/auth/keys").status_code == 401
        assert client.get("/vault/secrets").status_code == 401
