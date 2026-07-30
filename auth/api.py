"""Auth key management REST API — mounted on Cortex."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.keys import AmbiguousKeyIdError
from auth.middleware import (
    create_key,
    list_keys,
    require_scope,
    revoke_key,
    SCOPES,
)

logger = logging.getLogger(__name__)


class CreateKeyRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=200)
    scopes: list[str] = Field(..., min_length=1)
    expires_days: int | None = Field(default=None, ge=1, le=365)


class RevokeKeyResponse(BaseModel):
    status: str
    key_id: str


def create_auth_router() -> APIRouter:
    """Create the auth management router. All endpoints require admin scope."""

    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/keys")
    async def create_api_key(
        req: CreateKeyRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Create a new API key with specified scopes.

        Returns the plaintext key — this is the only time it's visible.
        Store it securely.
        """
        try:
            result = await create_key(
                agent_id=req.agent_id,
                scopes=req.scopes,
                expires_days=req.expires_days,
            )
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    @router.get("/keys")
    async def list_api_keys(
        identity: dict = Depends(require_scope("admin")),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """List all API keys (metadata only, never plaintext)."""
        keys = await list_keys(limit=limit)
        return {"keys": keys, "count": len(keys)}

    @router.delete("/keys/{key_id}")
    async def revoke_api_key(
        key_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> RevokeKeyResponse:
        """Revoke an API key by its short ID."""
        try:
            success = await revoke_key(key_id)
        except AmbiguousKeyIdError as exc:
            # 409, not 500: the request is well-formed and the server state is
            # the problem. Nothing was deleted.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Key {key_id} matches {len(exc.matches)} stored records "
                    f"({', '.join(exc.matches)}). Nothing was revoked — resolve "
                    "the ambiguity in Redis before retrying."
                ),
            ) from exc
        if not success:
            raise HTTPException(status_code=404, detail=f"Key {key_id} not found")
        return RevokeKeyResponse(status="revoked", key_id=key_id)

    @router.get("/scopes")
    async def list_scopes(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """List all available scopes."""
        return {"scopes": sorted(SCOPES)}

    return router
