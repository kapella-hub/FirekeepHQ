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


class TestAuthOnRouterFailureIsLegible:
    """A router that fails to construct must not leave a bare 404.

    The auth-OFF branch explains itself with a 503 naming the setting and the
    fix. The auth-ON branch used to log a warning and move on, leaving every
    /vault/* path answering 404 — which reads as a typo, a wrong port or an old
    build, and sends the operator looking anywhere but at the actual fault.
    Since auth-on is now the default, that was the branch nearly everyone runs.
    """

    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        """Auth ENABLED, but both router factories raise.

        Patches the modules the stanza imports locally (auth.api / vault.api),
        which is what its `from ... import` actually resolves — the same reason
        _mount patches the attribute on auth.config rather than the imported name.
        """
        import auth.api
        import auth.config as auth_config
        import vault.api
        from auth.config import AuthSettings

        def _boom():
            raise RuntimeError("simulated construction failure")

        monkeypatch.setattr(auth.api, "create_auth_router", _boom)
        monkeypatch.setattr(vault.api, "create_vault_router", _boom)
        monkeypatch.setattr(
            auth_config, "get_auth_settings", lambda: AuthSettings(ENABLED=True)
        )
        app = FastAPI()
        main_mod._register_admin_surface_routers(app)
        return TestClient(app)

    def test_vault_failure_503s_with_a_reason_not_404(self, client):
        resp = client.get("/vault/secrets")
        assert resp.status_code == 503, resp.status_code
        detail = resp.json()["detail"]
        assert "failed to start" in detail
        assert "simulated construction failure" in detail
        # ...and must not be mistaken for the deliberate auth-off withholding,
        # which is a configuration choice rather than a fault.
        assert "AUTH_ENABLED=false" not in detail

    def test_auth_failure_503s_with_a_reason_not_404(self, client):
        resp = client.get("/auth/keys")
        assert resp.status_code == 503, resp.status_code
        assert "failed to start" in resp.json()["detail"]

    def test_both_surfaces_are_still_mounted(self, client):
        """A dead vault must not be a dead API.

        The stand-in exists so the failure is legible, not so the process dies:
        memory, sessions and coordination are unaffected by a broken vault.
        """
        app = client.app
        paths = {r.path for r in app.routes}
        assert any(p.startswith("/vault") for p in paths), paths
        assert any(p.startswith("/auth") for p in paths), paths
