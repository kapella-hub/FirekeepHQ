"""FirekeepCortex web dashboard — FastAPI router.

Serves a single-page dashboard for monitoring memories, graph state,
and the dead-letter queue.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db.graph import Neo4jClient
from app.db.vector import VectorClient

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


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
    ):
        """Return recent memories from Qdrant (paginated, optional search)."""
        try:
            memories = await vector.list_memories(
                limit=limit,
                offset=offset,
                query=q,
                namespace=namespace,
            )
            return {"memories": memories, "limit": limit, "offset": offset, "query": q}
        except Exception as exc:
            logger.error("Dashboard memories API error: %s", exc)
            return {"memories": [], "limit": limit, "offset": offset, "query": q, "error": "Internal service error"}

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
    async def api_dlq_retry():
        """Move items from DLQ back to the main queue."""
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
    async def api_dlq_clear():
        """Clear the DLQ."""
        dlq_key = f"{get_settings().REDIS_STREAM_KEY}:dlq"
        try:
            deleted = await redis_client.delete(dlq_key)
            return {"status": "ok", "deleted": deleted}
        except Exception as exc:
            logger.error("Dashboard DLQ clear error: %s", exc)
            return {"status": "error", "error": "Internal service error"}

    return router
