"""Firekeep Auth — FastAPI scope enforcement.

Key primitives (generation, storage, validation, SCOPES) live in auth/keys.py
(fastapi-free, shared with the pure-ASGI validator in auth/asgi.py). This
module re-exports them for backward compatibility — external callers
(auth/api.py, vault/api.py, replay/api.py, cortex/app/main.py) import from
here — and adds the FastAPI-only require_scope dependency on top.

Enforcement is gated behind AUTH_ENABLED (default: False). When disabled,
all requests pass through with an anonymous identity.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from auth import keys as _keys
from auth.keys import (  # noqa: F401 — re-exports (same objects as auth.keys)
    ANONYMOUS_SCOPES,
    ENROLLABLE_SCOPES,
    SCOPES,
    AmbiguousKeyIdError,
    _ANONYMOUS_IDENTITY,
    _KEY_INDEX,
    _KEY_PREFIX,
    _hash_key,
    anonymous_denied_detail,
    create_key,
    generate_api_key,
    init_auth,
    invalid_credential_detail,
    list_keys,
    rename_device,
    revoke_key,
    scopes_allow,
    validate_key,
    validate_key_by_hash,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_scope(scope: str):
    """Create a FastAPI dependency that checks for a required scope.

    When AUTH_ENABLED=False: the caller is the anonymous identity, which is
    scope-checked like any other — non-admin scopes pass, "admin" is refused.
    When AUTH_ENABLED=True: validates X-API-Key header and checks scopes.

    NOTE: reads the enable flag via the auth.keys module attribute (not an
    imported value) so init_auth() calls are observed at request time.

    Usage:
        @router.get("/endpoint")
        async def my_endpoint(identity: dict = Depends(require_scope("replay:read"))):
            member_id = identity["member_id"]
    """
    async def _check_scope(request: Request) -> dict[str, Any]:
        if not _keys._AUTH_ENABLED:
            # Pass-through, but NOT a free pass. Until audit blocker 7 this
            # returned the anonymous identity unconditionally, WITHOUT looking
            # at `scope` at all — so require_scope("admin") on /vault/secrets
            # and /auth/keys was satisfied by anyone who could reach the port,
            # whatever the anonymous scope list said. Narrowing that list is
            # only half the fix; the check has to actually run.
            anon = _keys._ANONYMOUS_IDENTITY
            if not _keys.scopes_allow(anon["scopes"], scope, allow_wildcard=False):
                raise HTTPException(
                    status_code=403,
                    detail=_keys.anonymous_denied_detail(scope),
                )
            return anon

        # Extract API key from header
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing X-API-Key header",
            )

        # Validate key
        identity = await validate_key(api_key)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail=await invalid_credential_detail(api_key),
            )

        # Check scope. Wildcard IS honoured here: bootstrap-keys.sh mints the
        # owner's admin key and the dashboard key with ["*"].
        key_scopes = identity.get("scopes", [])
        if not _keys.scopes_allow(key_scopes, scope):
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scope: requires '{scope}', key has {key_scopes}",
            )

        return identity

    return _check_scope


def require_any_scope(*scopes: str):
    """Like require_scope, but satisfied by ANY ONE of `scopes`.

    Added for the vault read routes, which accept "vault:read" OR "admin". A
    chain of two require_scope dependencies cannot express that -- FastAPI ANDs
    dependencies, so it would demand both.

    Deliberately mirrors require_scope's auth-disabled branch rather than
    short-circuiting it. That branch is load-bearing: until audit blocker 7 the
    disabled path returned the anonymous identity WITHOUT consulting the required
    scope at all, which satisfied require_scope("admin") on the vault for anyone
    who could reach the port. A new gate that skipped the check would reopen
    exactly that hole, so this one runs it too -- and because "vault:read" is
    subtracted from ANONYMOUS_SCOPES, an anonymous caller is still refused here
    even with enforcement off.
    """
    if not scopes:
        raise ValueError("require_any_scope needs at least one scope")

    async def _check_any(request: Request) -> dict[str, Any]:
        if not _keys._AUTH_ENABLED:
            anon = _keys._ANONYMOUS_IDENTITY
            if not any(
                _keys.scopes_allow(anon["scopes"], s, allow_wildcard=False) for s in scopes
            ):
                # Report the LEAST-privileged option: it is the one an operator
                # should grant. Naming "admin" would invite over-granting.
                raise HTTPException(
                    status_code=403,
                    detail=_keys.anonymous_denied_detail(scopes[0]),
                )
            return anon

        api_key = request.headers.get("X-API-Key")
        if not api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header")

        identity = await validate_key(api_key)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail=await invalid_credential_detail(api_key),
            )

        key_scopes = identity.get("scopes", [])
        if not any(_keys.scopes_allow(key_scopes, s) for s in scopes):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Insufficient scope: requires one of {list(scopes)}, "
                    f"key has {key_scopes}"
                ),
            )

        return identity

    return _check_any

