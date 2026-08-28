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
from app.migration_gate import require_not_frozen
from app.models import (
    BacklinksResponse,
    ConfirmRequest,
    ConfirmResponse,
    ContestedResolveRequest,
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

    @router.post("/memory/deprecate", dependencies=[Depends(require_not_frozen)])
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

    @router.post("/memory/confirm", dependencies=[Depends(require_not_frozen)])
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

    @router.post("/memory/restore", dependencies=[Depends(require_not_frozen)])
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

    @router.get("/memory/{memory_id}/evidence")
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def memory_evidence(
        request: Request,
        memory_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """The evidence ledger for one memory, assembled from what already flows.

        Knowledge Autopilot round 1: before anything is ever promoted or
        retired automatically, a human must be able to see WHY a memory ranks
        the way it does — every signal that feeds scoring and every lifecycle
        fact, in one read. Nothing here is new bookkeeping; it is the existing
        payload fields, graph lineage, and dream provenance composed into one
        response.

        Admin-scoped like the autopilot inbox, and for the same reason: it
        exposes free-text feedback comments and member/agent provenance, and
        memory ids are handed out by recall — a non-admin key must not be able
        to walk them into other workspaces' evidence.
        """
        memory = await vector.get_memory(memory_id)
        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")

        # get_memory hoists only id/text/status/confirmed_count/
        # contradicted_count/last_confirmed_at/superseded_by and folds every
        # other payload key under "metadata". Everything below that is not in
        # that hoisted set MUST be read from meta — reading it at the top level
        # returns None/0 for every real memory.
        meta = memory.get("metadata") or {}

        graph_history: dict = {"supersedes": [], "superseded_by": None}
        try:
            graph_history = await graph.get_supersession_history(memory_id)
        except Exception:
            logger.warning("Failed to get graph history for %s", memory_id)

        return {
            "memory_id": memory_id,
            "status": memory.get("status", "active"),
            "provenance": {
                "source": meta.get("source"),
                "agent_id": meta.get("agent_id"),
                "member_id": meta.get("member_id"),
                "workspace_id": meta.get("workspace_id"),
                "project": meta.get("project"),
                "created_at": meta.get("created_at"),
                "timestamp": meta.get("timestamp"),
                "dreamed_from": meta.get("dreamed_from"),
            },
            "usage": {
                "access_count": meta.get("access_count", 0),
                "last_recalled_at": meta.get("last_recalled_at"),
            },
            "judgments": {
                "confirmed_count": memory.get("confirmed_count", 0),
                "contradicted_count": memory.get("contradicted_count", 0),
                "last_confirmed_at": memory.get("last_confirmed_at"),
                "feedback_useful_count": meta.get("feedback_useful_count", 0),
                "feedback_not_useful_count": meta.get("feedback_not_useful_count", 0),
                "feedback_last_at": meta.get("feedback_last_at"),
                "feedback_last_comment": meta.get("feedback_last_comment"),
            },
            "outcomes": {
                "owm_efficacy": meta.get("owm_efficacy"),
                "owm_n": meta.get("owm_n"),
                "owm_updated_at": meta.get("owm_updated_at"),
            },
            "disputes": {
                "contested": bool(meta.get("contested")),
                "contested_with": meta.get("contested_with"),
                "contested_at": meta.get("contested_at"),
                "coexist_with": meta.get("coexist_with"),
            },
            "lineage": {
                "superseded_by": memory.get("superseded_by"),
                "supersedes": graph_history.get("supersedes", []),
                "status_reason": meta.get("status_reason"),
            },
            "archive": {
                "archived_at": meta.get("archived_at"),
                "archive_source": meta.get("archive_source"),
                "purge_eligible_at": meta.get("purge_eligible_at"),
            },
        }

    @router.post("/memory/contested/resolve", dependencies=[Depends(require_not_frozen)])
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def resolve_contested(
        request: Request,
        body: ContestedResolveRequest,
        identity: dict = Depends(require_scope("memory:write")),
    ) -> dict[str, Any]:
        """Resolve a contested pair with a human (or explicitly-delegated) verdict.

        The deep-contradiction pass refuses to guess between two unconfirmed
        memories; this is where the guess it declined to make gets made by
        someone accountable. Two verdicts:
        - 'supersede': winner stays active (and is confirmed — the verdict IS
          human evidence), loser is superseded with the contradiction counted
          and a SUPERSEDES edge recorded, same as every other supersede path.
        - 'coexist': both are genuinely true (different contexts); flags clear
          on both and each side gets a durable `coexist_with` marker naming
          the other. The marker is what stops the nightly pass re-contesting
          the identical pair forever — their unchanged texts still sit in the
          similarity band, so without it a coexist verdict would be undone
          within 24 hours.
        """
        winner = await vector.get_memory(body.winner_id)
        loser = await vector.get_memory(body.loser_id)
        if not winner or not loser:
            raise HTTPException(status_code=404, detail="Memory not found")
        # contested_with is NOT a hoisted get_memory field — it lives under
        # "metadata". Reading it at the top level made this endpoint 409 on
        # every genuinely contested pair.
        winner_meta = winner.get("metadata") or {}
        loser_meta = loser.get("metadata") or {}
        if winner_meta.get("contested_with") != body.loser_id and loser_meta.get(
            "contested_with"
        ) != body.winner_id:
            raise HTTPException(
                status_code=409,
                detail="These memories are not contested with each other",
            )

        settings = get_settings()
        if body.action == "supersede":
            # Verdict FIRST, flags cleared LAST: if the supersede write fails,
            # the request 500s with the dispute still recorded — the pair stays
            # in the inbox and the human retries, instead of the verdict
            # silently evaporating with both memories left active.
            await vector.update_status(
                body.loser_id,
                "superseded",
                superseded_by=body.winner_id,
                reason="contested pair resolved by verdict",
                count_as_contradiction=True,
            )
            await vector.confirm_memory(body.winner_id)
            # Same best-effort edge every sibling supersede path records —
            # without it the verdict is invisible to get_supersession_history,
            # which the evidence endpoint's lineage section reads.
            try:
                await graph.create_supersession(
                    newer_id=body.winner_id,
                    older_id=body.loser_id,
                    reason="contested pair resolved by verdict",
                    detected="verdict",
                )
            except Exception:
                logger.warning(
                    "Failed to create supersession edge for %s", body.loser_id
                )
            await vector._client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload={
                    "contested": False,
                    "contested_with": None,
                    "contested_at": None,
                },
                points=[body.winner_id, body.loser_id],
            )
        else:  # coexist — clear the dispute AND leave the durable marker
            for this_id, other_id in (
                (body.winner_id, body.loser_id),
                (body.loser_id, body.winner_id),
            ):
                await vector._client.set_payload(
                    collection_name=settings.QDRANT_COLLECTION,
                    payload={
                        "contested": False,
                        "contested_with": None,
                        "contested_at": None,
                        "coexist_with": other_id,
                    },
                    points=[this_id],
                )
        return {
            "status": "resolved",
            "action": body.action,
            "winner_id": body.winner_id,
            "loser_id": body.loser_id,
        }

    return router
