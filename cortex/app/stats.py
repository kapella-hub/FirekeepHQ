"""Stats router for FirekeepCortex — memory statistics endpoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter

from app.models import MemoryStats

if TYPE_CHECKING:
    from app.db.graph import Neo4jClient
    from app.db.vector import VectorClient

logger = logging.getLogger(__name__)


def create_stats_router(graph: Neo4jClient, vector: VectorClient, redis_client) -> APIRouter:
    """Create the stats router with injected dependencies."""
    router = APIRouter(tags=["stats"])

    @router.get("/memory/stats", response_model=MemoryStats)
    async def memory_stats() -> MemoryStats:
        """Return aggregated memory statistics from all stores."""
        import asyncio

        async def _get_dlq_depth() -> int:
            try:
                return await redis_client.llen("firekeep:event_stream:dlq")
            except Exception:
                return 0

        graph_stats, vector_stats, dlq_depth = await asyncio.gather(
            graph.get_stats(),
            vector.get_stats(),
            _get_dlq_depth(),
            return_exceptions=True,
        )

        if isinstance(graph_stats, BaseException):
            graph_stats = {"node_count": 0, "edge_count": 0, "domains": [], "top_tags": []}
        if isinstance(vector_stats, BaseException):
            vector_stats = {"total": 0, "oldest_memory": None, "newest_memory": None, "namespace_counts": {}}
        if isinstance(dlq_depth, BaseException):
            dlq_depth = 0

        return MemoryStats(
            total_memories=vector_stats.get("total", 0),
            graph_nodes=graph_stats.get("node_count", 0),
            graph_edges=graph_stats.get("edge_count", 0),
            domains=graph_stats.get("domains", []),
            top_tags=graph_stats.get("top_tags", []),
            dlq_depth=dlq_depth,
            oldest_memory=vector_stats.get("oldest_memory"),
            newest_memory=vector_stats.get("newest_memory"),
            namespace_counts=vector_stats.get("namespace_counts", {}),
        )

    return router
