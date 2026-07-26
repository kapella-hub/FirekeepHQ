"""Knowledge lifecycle management router for FirekeepCortex.

Provides endpoints for deprecating, confirming, and tracing the history
of memories stored in the vector and graph databases.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.models import (
    BacklinksResponse,
    ConfirmRequest,
    ConfirmResponse,
    DeprecateRequest,
    DeprecateResponse,
    MemoryHistoryResponse,
)

logger = logging.getLogger(__name__)


def create_lifecycle_router(graph: Neo4jClient, vector: VectorClient) -> APIRouter:
    """Factory that returns an APIRouter wired to the given graph/vector clients."""
    router = APIRouter(tags=["lifecycle"])
    limiter = Limiter(key_func=get_remote_address)

    @router.post("/memory/deprecate")
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def deprecate_memories(request: Request, body: DeprecateRequest) -> DeprecateResponse:
        """Change memory status to deprecated, superseded, or archived."""
        updated = 0
        for memory_id in body.memory_ids:
            try:
                await vector.update_status(
                    memory_id=memory_id,
                    status=body.status,
                    superseded_by=body.superseded_by,
                )
                # If superseding, create graph edge
                if body.status == "superseded" and body.superseded_by:
                    try:
                        await graph.create_supersession(
                            newer_id=body.superseded_by,
                            older_id=memory_id,
                            reason=body.reason,
                            detected="manual",
                        )
                    except Exception:
                        logger.warning("Failed to create supersession edge for %s", memory_id)
                updated += 1
            except Exception:
                logger.warning("Failed to update status for memory %s", memory_id)
        return DeprecateResponse(status="updated", updated=updated)

    @router.post("/memory/confirm")
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def confirm_memories(request: Request, body: ConfirmRequest) -> ConfirmResponse:
        """Confirm memories are still valid -- resets decay, bumps confidence."""
        confirmed = 0
        for memory_id in body.memory_ids:
            try:
                success = await vector.confirm_memory(memory_id)
                if success:
                    confirmed += 1
            except Exception:
                logger.warning("Failed to confirm memory %s", memory_id)
        return ConfirmResponse(status="confirmed", confirmed=confirmed)

    @router.get("/memory/{memory_id}/history")
    async def memory_history(memory_id: str) -> MemoryHistoryResponse:
        """Get the supersession chain for a memory."""
        # Get memory from vector store
        memory = await vector.get_memory(memory_id)
        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")

        # Try to get graph history (best-effort)
        graph_history: dict = {"supersedes": [], "superseded_by": None}
        try:
            graph_history = await graph.get_supersession_history(memory_id)
        except Exception:
            logger.warning("Failed to get graph history for %s", memory_id)

        return MemoryHistoryResponse(
            memory_id=memory_id,
            status=memory.get("status", "active"),
            superseded_by={"id": memory["superseded_by"]} if memory.get("superseded_by") else None,
            supersedes=graph_history["supersedes"],
            confirmed_count=memory.get("confirmed_count", 0),
            contradicted_count=memory.get("contradicted_count", 0),
            last_confirmed_at=memory.get("last_confirmed_at"),
        )

    @router.get("/memory/{memory_id}/backlinks")
    async def memory_backlinks(memory_id: str) -> BacklinksResponse:
        """Get all automatically discovered backlinks for a memory."""
        links = await graph.get_backlinks(memory_id)
        return BacklinksResponse(
            memory_id=memory_id,
            backlinks=links,
            total=len(links),
        )

    return router
