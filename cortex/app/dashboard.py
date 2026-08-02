"""FirekeepCortex web dashboard — FastAPI router.

Serves a single-page dashboard for monitoring memories, graph state,
and the dead-letter queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from auth.middleware import require_scope
from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.workers import gc as gc_worker

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Maintenance audit entries written before the archive-first rewrite carry only
# `evicted_at` -- the pass they came from could only hard-delete. They are
# surfaced as legacy purges rather than dropped: an operator reading the trail
# needs to see that those records are gone, not merely unexplained.
_LEGACY_AUDIT_ACTION = "legacy_purge"


def create_dashboard_router(
    graph: Neo4jClient,
    vector: VectorClient,
    redis_client: Any,
) -> APIRouter:
    """Create the dashboard router with injected database clients."""
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_page():
        """Serve the dashboard HTML page."""
        html_path = _STATIC_DIR / "dashboard.html"
        if not html_path.exists():
            return HTMLResponse(
                content="<h1>Dashboard HTML not found</h1>",
                status_code=404,
            )
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @router.get("/api/memories")
    async def api_memories(
        q: str | None = Query(default=None, description="Search query"),
        namespace: str | None = Query(default=None, description="Filter by domain/namespace"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        view: str = Query(default="all", description="Lifecycle slice: all | available | archived"),
    ):
        """Return recent memories from Qdrant (paginated, optional search).

        ``view=archived`` backs the Memory -> Archived recovery tab; the vector
        client rejects an unknown view rather than silently listing everything.
        """
        try:
            memories = await vector.list_memories(
                limit=limit,
                offset=offset,
                query=q,
                namespace=namespace,
                view=view,
            )
            return {
                "memories": memories,
                "limit": limit,
                "offset": offset,
                "query": q,
                "view": view,
            }
        except Exception as exc:
            logger.error("Dashboard memories API error: %s", exc)
            return {
                "memories": [], "limit": limit, "offset": offset, "query": q,
                "view": view, "error": "Internal service error",
            }

    @router.get("/api/graph")
    async def api_graph(
        concept: str | None = Query(default=None, description="Concept to center the view on"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """Return graph nodes/edges for visualization."""
        try:
            snapshot = await graph.get_graph_snapshot(concept=concept, limit=limit)
            return snapshot
        except Exception as exc:
            logger.error("Dashboard graph API error: %s", exc)
            return {"nodes": [], "edges": [], "error": "Internal service error"}

    @router.get("/api/stats")
    async def api_stats():
        """Return memory count, node/edge counts, DLQ depth, domains list."""
        stats: dict[str, Any] = {}

        # Memory count from Qdrant
        try:
            stats["memory_count"] = await vector.memory_count() or 0
        except Exception:
            stats["memory_count"] = 0

        # Node/edge counts from Neo4j
        try:
            counts = await graph.get_node_edge_counts()
            stats["node_count"] = counts["nodes"]
            stats["edge_count"] = counts["edges"]
        except Exception:
            stats["node_count"] = 0
            stats["edge_count"] = 0

        # DLQ depth from Redis
        try:
            dlq_key = f"{get_settings().REDIS_STREAM_KEY}:dlq"
            stats["dlq_depth"] = await redis_client.llen(dlq_key)
        except Exception:
            stats["dlq_depth"] = 0

        # Domains from Neo4j
        try:
            stats["domains"] = await graph.get_domains()
        except Exception:
            stats["domains"] = []

        return stats

    @router.get("/api/dlq")
    async def api_dlq(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        """Return DLQ items from Redis (paginated)."""
        dlq_key = f"{get_settings().REDIS_STREAM_KEY}:dlq"
        try:
            total = await redis_client.llen(dlq_key)
            raw_items = await redis_client.lrange(dlq_key, offset, offset + limit - 1)
            items = []
            for raw in raw_items:
                try:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    items.append(json.loads(raw))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    items.append({"raw": str(raw)})
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        except Exception as exc:
            logger.error("Dashboard DLQ API error: %s", exc)
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "error": "Internal service error"}

    @router.post("/api/dlq/retry")
    async def api_dlq_retry(
        identity: dict = Depends(require_scope("admin")),
    ):
        """Move items from DLQ back to the main queue.

        Admin-scoped, matching its byte-equivalent twin
        POST /ops/dlq/retry-events (ops.py:228) — same rpop/lpush over the same
        two keys. Until 2026-07-26 this one carried no scope at all, so with
        AUTH_ENABLED=false, where FirekeepKeyAuthMiddleware does not run, it was
        reachable by anyone on the port while the twin was refused. Gating one
        route and not its double is the specific way that fix fails.
        """
        dlq_key = f"{get_settings().REDIS_STREAM_KEY}:dlq"
        stream_key = get_settings().REDIS_STREAM_KEY
        try:
            count = 0
            while True:
                item = await redis_client.rpop(dlq_key)
                if item is None:
                    break
                await redis_client.lpush(stream_key, item)
                count += 1
            return {"status": "ok", "retried": count}
        except Exception as exc:
            logger.error("Dashboard DLQ retry error: %s", exc)
            return {"status": "error", "error": "Internal service error"}

    @router.delete("/api/dlq/clear")
    async def api_dlq_clear(
        identity: dict = Depends(require_scope("admin")),
    ):
        """Clear the DLQ.

        Admin-scoped, and the more urgent of the pair: this one is DESTRUCTIVE
        and had no equivalent in ops.py to be compared against, so nothing
        flagged it. An unauthenticated DELETE dropped every dead-lettered event
        with no undo.
        """
        dlq_key = f"{get_settings().REDIS_STREAM_KEY}:dlq"
        try:
            deleted = await redis_client.delete(dlq_key)
            return {"status": "ok", "deleted": deleted}
        except Exception as exc:
            logger.error("Dashboard DLQ clear error: %s", exc)
            return {"status": "error", "error": "Internal service error"}

    @router.get("/api/memory-gc")
    async def api_memory_gc(
        limit: int = Query(default=20, ge=1, le=100),
        identity: dict = Depends(require_scope("memory:read")),
    ):
        """Archive/purge policy plus the recent memory-maintenance audit trail.

        Read-only, and deliberately `memory:read` rather than admin: this is the
        page a human checks *before* deciding whether anything needs restoring.
        """
        settings = get_settings()
        payload: dict[str, Any] = {
            "enabled": bool(getattr(settings, "GC_ENABLED", False)),
            "dry_run": bool(getattr(settings, "GC_DRY_RUN", False)),
            "purge_enabled": bool(getattr(settings, "GC_PURGE_ENABLED", False)),
            "archive_grace_days": getattr(
                settings, "GC_ARCHIVE_GRACE_DAYS", gc_worker.DEFAULT_ARCHIVE_GRACE_DAYS
            ),
            "events": [],
        }

        try:
            raw_events = await redis_client.lrange(
                gc_worker.GC_EVICTION_LOG_KEY, 0, limit - 1
            )
        except Exception as exc:
            logger.error("Dashboard memory-gc audit read error: %s", exc)
            payload["error"] = "Internal service error"
            return payload

        events: list[dict[str, Any]] = []
        for raw in raw_events or []:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                events.append({"action": "unparsed", "raw": str(raw)})
                continue
            if not isinstance(entry, dict):
                events.append({"action": "unparsed", "raw": str(raw)})
                continue
            if entry.get("action"):
                entry.setdefault("legacy", False)
            else:
                # Pre-archive-first entry: `evicted_at` only, and that pass
                # could only delete. Say so rather than inventing an action.
                entry["action"] = _LEGACY_AUDIT_ACTION
                entry["legacy"] = True
                entry.setdefault("occurred_at", entry.get("evicted_at"))
            events.append(entry)

        payload["events"] = events
        return payload

    @router.post("/api/memory-gc/preview")
    async def api_memory_gc_preview(
        limit: int = Query(default=50, ge=1, le=500),
        identity: dict = Depends(require_scope("memory:read")),
    ):
        """No-write preview of what the next maintenance pass would do.

        Resolved off the module at call time (never imported by name) so the
        preview always runs the same evaluation the scheduled task does, and so
        the blocking Qdrant scan stays off the event loop.
        """
        try:
            return await asyncio.to_thread(
                gc_worker.preview_memories, get_settings(), limit
            )
        except Exception as exc:
            logger.error("Dashboard memory-gc preview error: %s", exc)
            return {"status": "error", "error": "Internal service error"}

    return router
