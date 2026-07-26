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
    SCOPES,
    _ANONYMOUS_IDENTITY,
    _KEY_INDEX,
    _KEY_PREFIX,
    _hash_key,
    create_key,
    generate_api_key,
    init_auth,
    list_keys,
    revoke_key,
    validate_key,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_scope(scope: str):
    """Create a FastAPI dependency that checks for a required scope.

    When AUTH_ENABLED=False: returns anonymous identity (pass-through).
    When AUTH_ENABLED=True: validates X-API-Key header and checks scopes.

    NOTE: reads the enable flag via the auth.keys module attribute (not an
    imported value) so init_auth() calls are observed at request time.

    Usage:
        @router.get("/endpoint")
        async def my_endpoint(identity: dict = Depends(require_scope("replay:read"))):
            agent_id = identity["agent_id"]
    """
    async def _check_scope(request: Request) -> dict[str, Any]:
        if not _keys._AUTH_ENABLED:
            return _keys._ANONYMOUS_IDENTITY

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
                detail="Invalid or expired API key",
            )

        # Check scope
        key_scopes = identity.get("scopes", [])
        if "*" not in key_scopes and scope not in key_scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scope: requires '{scope}', key has {key_scopes}",
            )

        return identity

    return _check_scope
