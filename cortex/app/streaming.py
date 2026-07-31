"""SSE streaming router for FirekeepCortex recall endpoint.

Streams recall results progressively so agents can start processing
before full retrieval completes.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery
from auth.principal import request_principal

logger = logging.getLogger(__name__)


def create_streaming_router(
    rag_engine: RAGEngine,
    graph: Neo4jClient,
    vector: VectorClient,
) -> APIRouter:
    """Create and return the streaming recall router."""
    router = APIRouter(tags=["streaming"])

    @router.post("/memory/recall/stream")
    async def recall_stream(request: Request, query: ContextQuery) -> StreamingResponse:
        """Stream recall results as Server-Sent Events."""
        principal = request_principal(request)

        async def event_generator():
            async for event in rag_engine.recall_streaming(
                query, workspace_id=principal["workspace_id"]
            ):
                event_type = event["type"]
                data = json.dumps(event["data"], default=str)

                if event_type == "source":
                    yield f"event: sources\ndata: {data}\n\n"
                elif event_type == "context":
                    yield f"event: context\ndata: {data}\n\n"
                elif event_type == "done":
                    yield f"event: done\ndata: {data}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
