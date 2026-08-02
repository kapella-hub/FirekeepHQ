"""Knowledge lifecycle management router for FirekeepCortex.

Provides endpoints for deprecating, confirming, and tracing the history
of memories stored in the vector and graph databases.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
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
    RestoreRequest,
    RestoreResponse,
)
from auth.middleware import require_scope

logger = logging.getLogger(__name__)

# Same audit list the maintenance task writes (app.workers.gc.GC_EVICTION_LOG_KEY).
# Restoring is the inverse of archiving, so it belongs in the same trail the
# dashboard reads -- an archive with no matching restore entry would read as
# still-archived to anyone auditing the log.
GC_EVICTION_LOG_KEY = "gc:eviction:log"


def create_lifecycle_router(
    graph: Neo4jClient,
    vector: VectorClient,
    redis_client: Any | None = None,
) -> APIRouter:
    """Factory that returns an APIRouter wired to the given graph/vector clients.

    ``redis_client`` is optional: without it the restore endpoint still restores,
    it just records no audit entry. Restoring is a recovery action, so a Redis
    outage must not be the thing that stops a human getting their memory back.
    """
    router = APIRouter(tags=["lifecycle"])
    limiter = Limiter(key_func=get_remote_address)

    async def _append_audit(entries: list[dict[str, Any]]) -> None:
        """Best-effort append to the shared maintenance audit trail."""
        if not entries or redis_client is None:
            return
        try:
            pipe = redis_client.pipeline()
            for entry in entries:
                pipe.lpush(GC_EVICTION_LOG_KEY, json.dumps(entry))
            pipe.ltrim(GC_EVICTION_LOG_KEY, 0, 999)
            await pipe.execute()
        except Exception:
            logger.exception(
                "Restored %d memories but FAILED to write %s audit entries: %s",
                len(entries), GC_EVICTION_LOG_KEY, [e.get("id") for e in entries],
            )

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
                    reason=body.reason,
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

    @router.post("/memory/restore")
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def restore_memories(
        request: Request,
        body: RestoreRequest,
        identity: dict = Depends(require_scope("memory:write")),
    ) -> RestoreResponse:
        """Bring archived memories back to their pre-archive status.

        The inverse of the archive tier: the vector store returns each memory to
        the status it held before archiving (or "active" for legacy archives with
        no recorded origin) and clears the archive provenance, so a memory that
        is later re-archived starts a fresh recovery window rather than
        inheriting an already-elapsed ``purge_eligible_at``.
        """
        restored_ids: list[str] = []
        for memory_id in body.memory_ids:
            try:
                if await vector.restore_memory(memory_id):
                    restored_ids.append(memory_id)
            except Exception:
                logger.warning("Failed to restore memory %s", memory_id)

        occurred_at = datetime.now(timezone.utc).isoformat()
        await _append_audit([
            {
                "id": memory_id,
                "action": "restored",
                "occurred_at": occurred_at,
                "agent_id": identity.get("agent_id") if identity else None,
            }
            for memory_id in restored_ids
        ])

        return RestoreResponse(status="restored", restored=len(restored_ids))

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
