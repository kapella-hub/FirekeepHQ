"""A teammate must be able to READ a secret without holding the keys to the kingdom.

The bug
-------
Every vault route required ``admin``. The teammate scope set minted by
``deploy/firekeep-admin`` deliberately carries no ``admin``. So an agent asked to
"deploy to my vps" could not read the credential it needed — reproduced live with
a properly minted key::

    GET /vault/secrets -> 403
    {"detail":"Insufficient scope: requires 'admin', key has ['memory:read', ...]"}

There was no ``vault:*`` scope in the vocabulary at all, so the only workaround was
issuing teammates an ``admin`` key — which also grants API-key minting and every
other secret. That is strictly more exposure than a read scope.

The fix, and the trap inside it
-------------------------------
READ (``GET /secrets``, ``GET /secrets/{key}``) accepts ``vault:read`` OR
``admin``. WRITE and DELETE stay ``admin``-only: retrieving a secret you were
meant to have is ordinary work, creating or destroying one is administration, and
the blast radii are not comparable.

The trap: ``ANONYMOUS_SCOPES`` is DERIVED as ``SCOPES - {"admin", "*"}``, and its
own comment promises that a scope added later "is granted automatically". For a
scope that decrypts secrets, that promise is the vulnerability — it would have
handed secret-reading to any unauthenticated caller on a default
``AUTH_ENABLED=false`` box, which is exactly the exposure that put 12 real secrets
from the author's VPS on the public internet (audit blocker 7). So ``vault:read``
is subtracted by name, and that subtraction is asserted here rather than trusted.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from auth import keys as _keys
from auth.keys import ANONYMOUS_SCOPES, SCOPES
from auth.middleware import require_any_scope


class TestTheScopeExists:
    def test_vault_read_is_in_the_vocabulary(self):
        assert "vault:read" in SCOPES, (
            "without this scope the only way to read a secret is an admin key, "
            "which also mints API keys"
        )

    def test_teammate_keys_are_granted_it(self):
        """deploy/firekeep-admin is the only issuer of teammate keys. If the scope
        exists but is not minted, every teammate still 403s.

        Parses the NON_ADMIN_SCOPES ASSIGNMENT, not the file. Written first as
        `assert "vault:read" in text`, which mutation testing exposed as a
        non-check: the comment above the assignment explains why vault:read is
        there and also contains the string, so deleting it from the actual scope
        list left the test passing.
        """
        import json
        import re
        from pathlib import Path

        admin = Path(__file__).resolve().parents[2] / "deploy" / "firekeep-admin"
        text = admin.read_text(encoding="utf-8")
        m = re.search(r"^NON_ADMIN_SCOPES='(\[.*?\])'", text, re.M)
        assert m, "NON_ADMIN_SCOPES assignment not found in deploy/firekeep-admin"
        minted = json.loads(m.group(1))
        assert "vault:read" in minted, (
            f"firekeep-admin does not mint vault:read, so teammate keys still cannot "
            f"read a secret the agent was asked to use. Minted: {minted}"
        )
        assert "admin" not in minted, "teammate keys must never carry admin"


class TestSecretReadingIsNeverAnonymous:
    """The security boundary. These must hold even though ANONYMOUS_SCOPES is
    derived from SCOPES and would otherwise grant a new scope automatically."""

    def test_anonymous_does_not_get_vault_read(self):
        assert "vault:read" not in ANONYMOUS_SCOPES, (
            "an unauthenticated caller on a default box could read decrypted "
            "secrets — audit blocker 7 reopened"
        )

    def test_anonymous_does_not_get_admin(self):
        assert "admin" not in ANONYMOUS_SCOPES

    @pytest.mark.asyncio
    async def test_the_real_gate_refuses_an_anonymous_read(self, monkeypatch):
        """Asserting the SET is not enough: the pre-fix bug was that the disabled
        path never consulted the set. This drives the real dependency."""
        monkeypatch.setattr(_keys, "_AUTH_ENABLED", False)
        from vault.api import create_vault_router

        app = FastAPI()
        app.include_router(create_vault_router())  # the REAL gates
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/vault/secrets", "/vault/secrets/anything"):
                r = await client.get(path)
                assert r.status_code == 403, f"GET {path} -> {r.status_code}"


class TestRequireAnyScope:
    """The new dependency factory, in isolation."""

    def _dep(self, *scopes):
        return require_any_scope(*scopes)

    def test_rejects_an_empty_scope_list(self):
        with pytest.raises(ValueError):
            require_any_scope()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key_scopes,expected",
        [
            (["vault:read"], True),                    # the teammate case
            (["admin"], True),                         # the owner case
            (["*"], True),                             # bootstrap/dashboard keys
            (["memory:read", "vault:read"], True),     # realistic teammate set
            (["memory:read"], False),                  # no vault access
            ([], False),
        ],
    )
    async def test_any_one_scope_satisfies_it(self, monkeypatch, key_scopes, expected):
        monkeypatch.setattr(_keys, "_AUTH_ENABLED", True)

        async def _fake_validate(_key):
            return {"agent_id": "t", "scopes": key_scopes, "authenticated": True}

        import auth.middleware as mw
        monkeypatch.setattr(mw, "validate_key", _fake_validate)

        dep = require_any_scope("vault:read", "admin")
        from starlette.requests import Request
        req = Request({"type": "http", "headers": [(b"x-api-key", b"k")], "state": {}})

        if expected:
            ident = await dep(req)
            assert ident["scopes"] == key_scopes
        else:
            with pytest.raises(HTTPException) as exc:
                await dep(req)
            assert exc.value.status_code == 403
            # The message must name the LEAST-privileged option that would work,
            # or an operator fixes a 403 by granting admin.
            assert "vault:read" in str(exc.value.detail)


class TestWriteStaysAdminOnly:
    """The asymmetry is still the point — narrowed, not dropped (2026-08-19).

    Ordinary keys keep the admin-only write posture: a read scope that also
    permitted writes would let any teammate silently replace a shared
    credential. What changed for Maildex: keys under a KNOWN dex prefix are
    member secrets, writable under `dex:<id>` with in-body ownership checks —
    the full behavioral contract lives in test_dex_member_keys.py. This pin
    asserts the SHAPE: both write routes gate on require_any_scope over admin
    plus exactly the dex scopes, and the in-body admin/dex branching exists.
    """

    def test_write_routes_gate_on_admin_or_dex_scopes(self):
        import inspect

        from vault import api as vault_api
        src = inspect.getsource(vault_api.create_vault_router)
        for route in ('@router.post("/secrets")', '@router.delete("/secrets/{key}")'):
            i = src.find(route)
            assert i != -1, f"route {route} not found"
            window = src[i:i + 700]
            assert 'require_any_scope("admin"' in window, (
                f"{route} no longer starts its gate at admin")
            assert 'f"dex:{d}"' in window and "KNOWN_DEX_IDS" in window, (
                f"{route} gate is not derived from the dex-id table")
            assert "_is_admin(identity)" in window, (
                f"{route} lost its in-body admin/dex branching")
