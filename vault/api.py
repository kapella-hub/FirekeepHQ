"""Vault REST API — mounted on Cortex."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.middleware import require_any_scope, require_scope
from vault.store import delete_secret, list_secrets, retrieve_secret, store_secret

logger = logging.getLogger(__name__)


class StoreSecretRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=50000)
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None


class SecretMetadata(BaseModel):
    key: str
    description: str | None
    category: str | None
    tags: list[str]
    created_at: str | None
    updated_at: str | None
    created_by: str | None


class SecretResponse(SecretMetadata):
    value: str


def create_vault_router() -> APIRouter:
    """Create the vault management router.

    SCOPES ARE SPLIT, deliberately asymmetric:
      * READ  (GET /secrets, GET /secrets/{key}) -> "vault:read" OR "admin"
      * WRITE (POST /secrets, DELETE /secrets/{key}) -> "admin" only

    Every route used to require "admin", and the teammate scope set minted by
    deploy/firekeep-admin carries no admin scope. So a teammate's agent asked to
    "deploy to my vps" could not read the credential it needed -- reproduced with
    a properly minted key: 403 "Insufficient scope: requires 'admin'". The only
    workaround was issuing admin keys, which also grants key-minting and every
    other secret. A read scope is strictly less exposure than that workaround.

    Retrieving a secret you were meant to have is ordinary work. Creating or
    destroying one is administration. The blast radii are not comparable, so the
    scopes are not either.
    """

    router = APIRouter(prefix="/vault", tags=["vault"])

    # admin only: creating a secret is administration, not use.
    @router.post("/secrets")
    async def store_vault_secret(
        req: StoreSecretRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> SecretMetadata:
        """Store an encrypted secret. Returns metadata only (no value)."""
        try:
            result = await store_secret(
                key=req.key,
                value=req.value,
                description=req.description,
                category=req.category,
                tags=req.tags,
                created_by=identity.get("agent_id", "admin"),
            )
            return SecretMetadata(**result)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    @router.get("/secrets/{key}")
    async def get_vault_secret(
        key: str,
        identity: dict = Depends(require_any_scope("vault:read", "admin")),
    ) -> SecretResponse:
        """Retrieve a decrypted secret by key."""
        try:
            result = await retrieve_secret(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

        if result is None:
            raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")
        return SecretResponse(**result)

    # admin only: destroying a credential is irreversible for its holders.
    @router.delete("/secrets/{key}")
    async def delete_vault_secret(
        key: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Delete a secret by key."""
        try:
            success = await delete_secret(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

        if not success:
            raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")
        return {"status": "deleted", "key": key}

    @router.get("/secrets")
    async def list_vault_secrets(
        identity: dict = Depends(require_any_scope("vault:read", "admin")),
        category: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """List all secrets with metadata (no values)."""
        try:
            secrets = await list_secrets(category=category, limit=limit)
            return {"secrets": secrets, "count": len(secrets)}
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    return router
