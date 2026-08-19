"""Audit blocker 7: the anonymous identity must never carry admin authority.

The default install shipped `_ANONYMOUS_IDENTITY = {"scopes": ["*"]}` AND both
scope checkers returned that identity on the auth-disabled path WITHOUT ever
comparing it against the requested scope. Either defect alone opens
`GET /vault/secrets` and `POST /auth/keys` to anyone who can reach the port —
which is how 12 real secrets from the author's VPS reached the public internet.

Two defects, two independent guards, tested separately here:
  1. the scope SET itself (TestAnonymousScopeSet), and
  2. the ENFORCEMENT of it on both disabled paths (TestDisabled*), which holds
     even if (1) regresses because the wildcard is not honoured there.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from auth import keys
from auth.asgi import ScopeError, require_scope_asgi
from auth.keys import ANONYMOUS_SCOPES, SCOPES, scopes_allow
from auth.middleware import require_scope


@pytest.fixture
def auth_disabled(monkeypatch):
    """Pin BOTH enable flags off explicitly.

    require_scope reads keys._AUTH_ENABLED; require_scope_asgi reads
    AuthSettings (the AUTH_ENABLED env var, also loaded from .env). Neither
    disabled-path premise may be left to a default that is being flipped to
    true by the same audit fix.
    """
    import auth.asgi as asgi_module
    from auth.config import AuthSettings

    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    monkeypatch.setattr(
        asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=False),
        raising=False,
    )


def _request(identity: dict | None = None) -> Request:
    scope = {"type": "http", "state": {}}
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope)


# ---------------------------------------------------------------------------
# Guard 1 — the scope set
# ---------------------------------------------------------------------------


#: Scopes that must NEVER be granted to a caller who presented no key. "admin"
#: is the original (key minting + every vault route). "vault:read" joined it on
#: 2026-07-29 when the vault read routes were split off admin: the whole point of
#: that split is that reading a decrypted secret needs A KEY, and the derived
#: ANONYMOUS_SCOPES would otherwise have granted the new scope automatically --
#: reopening precisely the hole this file exists to guard.
# Dex scopes joined the withheld set 2026-08-19: they authorize writing
# MEMBER-OWNED vault secrets (`maildex.<id>` app passwords), and an
# identity-bearing write from a caller who never presented a key is the
# audit-blocker-7 class with a new door.
WITHHELD_FROM_ANONYMOUS = {"admin", "vault:read", "dex:docdex", "dex:maildex"}


class TestAnonymousScopeSet:
    def test_is_every_scope_except_the_withheld_ones(self):
        assert set(ANONYMOUS_SCOPES) == SCOPES - WITHHELD_FROM_ANONYMOUS

    def test_secret_reading_is_never_anonymous(self):
        """The regression that matters. ANONYMOUS_SCOPES is DERIVED from SCOPES,
        and its own comment promises a newly added scope is granted
        automatically. For a scope that decrypts secrets that promise is the
        vulnerability, so the subtraction is asserted explicitly."""
        assert "vault:read" not in ANONYMOUS_SCOPES, (
            "an unauthenticated caller on a default AUTH_ENABLED=false box could "
            "read decrypted secrets -- this is audit blocker 7 reopened"
        )

    def test_never_admin_and_never_wildcard(self):
        assert "admin" not in ANONYMOUS_SCOPES
        assert "*" not in ANONYMOUS_SCOPES

    def test_identity_uses_the_derived_set(self):
        assert set(keys._ANONYMOUS_IDENTITY["scopes"]) == set(ANONYMOUS_SCOPES)
        assert keys._ANONYMOUS_IDENTITY["authenticated"] is False

    def test_is_not_empty(self):
        """Deliberately NOT []: with auth off a single user must still be able
        to use memory, sessions, replay and evals without minting a key."""
        assert "memory:read" in ANONYMOUS_SCOPES
        assert "session:write" in ANONYMOUS_SCOPES
        assert len(ANONYMOUS_SCOPES) == len(SCOPES) - len(WITHHELD_FROM_ANONYMOUS)

    def test_derived_not_hardcoded(self):
        """A scope added to SCOPES later must be granted automatically, so the set
        cannot silently drift into withholding new ordinary scopes -- EXCEPT the
        explicitly withheld ones, which are subtracted by name."""
        assert ANONYMOUS_SCOPES == tuple(sorted(SCOPES - {"*"} - WITHHELD_FROM_ANONYMOUS))


class TestScopesAllow:
    def test_wildcard_grants_on_the_authenticated_path(self):
        """bootstrap-keys.sh mints the owner + dashboard keys with ["*"]."""
        assert scopes_allow(["*"], "admin") is True

    def test_wildcard_ignored_when_disallowed(self):
        """Belt and braces: if the anonymous scope set ever regressed to ["*"],
        the disabled path must still refuse admin."""
        assert scopes_allow(["*"], "admin", allow_wildcard=False) is False

    def test_plain_membership(self):
        assert scopes_allow(["memory:read"], "memory:read") is True
        assert scopes_allow(["memory:read"], "admin") is False

    def test_empty_scopes_deny(self):
        assert scopes_allow([], "memory:read") is False


# ---------------------------------------------------------------------------
# Guard 2 — enforcement on the auth-disabled path (FastAPI twin)
# ---------------------------------------------------------------------------


class TestDisabledPathFastAPI:
    @pytest.mark.asyncio
    async def test_admin_refused(self, auth_disabled):
        with pytest.raises(HTTPException) as exc_info:
            await require_scope("admin")(_request())
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_normal_scope_allowed(self, auth_disabled):
        identity = await require_scope("memory:read")(_request())
        assert identity["member_id"] == "member-owner"

    @pytest.mark.asyncio
    async def test_refusal_explains_itself(self, auth_disabled):
        """Operators meet this 403 on a default box (the dashboard's DLQ
        Requeue button, a vault_store MCP call). It must name the setting and
        the fix, not just say "forbidden"."""
        with pytest.raises(HTTPException) as exc_info:
            await require_scope("admin")(_request())
        detail = exc_info.value.detail
        assert "AUTH_ENABLED" in detail
        assert "bootstrap-keys.sh" in detail

    @pytest.mark.asyncio
    async def test_wildcard_regression_still_refused(self, auth_disabled, monkeypatch):
        """The two guards are independent: re-introduce the shipped defect
        (scopes back to ["*"]) and admin is STILL refused."""
        monkeypatch.setitem(keys._ANONYMOUS_IDENTITY, "scopes", ["*"])
        with pytest.raises(HTTPException) as exc_info:
            await require_scope("admin")(_request())
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Guard 2 — enforcement on the auth-disabled path (Starlette/FastMCP twin)
# ---------------------------------------------------------------------------


class TestDisabledPathAsgi:
    def test_admin_refused(self, auth_disabled):
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request(), "admin")
        assert exc_info.value.status_code == 403

    def test_normal_scope_allowed(self, auth_disabled):
        identity = require_scope_asgi(_request(), "relay:write")
        assert identity["member_id"] == "member-owner"

    def test_wildcard_regression_still_refused(self, auth_disabled, monkeypatch):
        monkeypatch.setitem(keys._ANONYMOUS_IDENTITY, "scopes", ["*"])
        with pytest.raises(ScopeError) as exc_info:
            require_scope_asgi(_request(), "admin")
        assert exc_info.value.status_code == 403
