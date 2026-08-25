"""SSE streaming router for FirekeepCortex recall endpoint.

Streams recall results progressively so agents can start processing
before full retrieval completes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated

import redis.asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery
from auth.principal import request_principal

logger = logging.getLogger(__name__)


async def _get_redis(request: Request) -> redis.asyncio.Redis:
    """Local mirror of `app.main.get_redis` (both just read
    `request.app.state.redis_client`).

    Not imported from `app.main`: `app.main` imports `create_streaming_router`
    from this module at module-load time, so an `app.main` import at THIS
    module's top level would be circular. It also has to resolve to a real
    callable at router-registration time (when `Depends(_get_redis)` is
    evaluated below), which happens whenever `create_streaming_router()` is
    called — including from unit tests that build the router directly without
    ever loading `app.main`. A tiny local duplicate sidesteps both problems.
    """
    return request.app.state.redis_client


async def _emit_stream_receipt(
    redis_client: redis.asyncio.Redis,
    sid: str,
    aid: str,
    query: ContextQuery,
    accessed_ids: list[str],
    result_count: int,
) -> None:
    """Best-effort parity with the non-streaming recall receipt (main.py
    `memory_recall`, ~line 1291-1342). Bumps `memory:access_counts` +
    `memory:last_recalled` for every recalled id, bumps the untagged-call
    counter, and emits one `memory_read` replay event. Never raises — a
    telemetry receipt must not break or delay the stream.

    `top_score` is deliberately omitted: it is `RecallResponse.score`, which
    is a post-normalization max() pinned at 1.0 whenever any result survives
    (see `main.py::_raw_top_score`'s docstring) — a constant, not a signal,
    and no consumer reads it.
    """
    try:
        # Deferred import: `app.main` imports `create_streaming_router` from
        # this module at load time, so importing back from `app.main` at
        # module scope would be a circular import. By request time `app.main`
        # is fully loaded.
        from app.main import _bump_untagged_counter, _replay_emit

        if accessed_ids:
            now_iso = datetime.now(timezone.utc).isoformat()
            pipe = redis_client.pipeline()
            for mem_id in accessed_ids[:50]:
                pipe.hincrby("memory:access_counts", mem_id, 1)
                # last-recall timestamp — feeds the skill staleness sweep,
                # same as the non-streaming path.
                pipe.hset("memory:last_recalled", mem_id, now_iso)
            await pipe.execute()

        await _bump_untagged_counter(redis_client, sid)
        await _replay_emit(
            "memory_read",
            session_id=sid,
            agent_id=aid,
            payload={
                "query": query.task[:200],
                "top_k": query.top_k,
                # None for deliberate calls; "prompt-hook" for pushed recall.
                "trigger": query.trigger,
                # ALL source frames (vector + graph), matching the
                # non-streaming handler's `len(result.sources)` — graph
                # sources carry no metadata["id"] so they are absent from
                # `accessed_ids`/`memory_ids` but must still be counted here,
                # or SSE recalls with graph hits would under-report.
                "result_count": result_count,
                "namespace": query.namespace,
                # OWM: the ids RETURNED, so a nightly pass can join which
                # sessions saw which memories to how those sessions ended.
                "memory_ids": accessed_ids[:50],
            },
        )
    except Exception as exc:  # noqa: BLE001 — a receipt must never break the stream
        logger.warning("stream recall receipt failed: %s", exc)


def create_streaming_router(
    rag_engine: RAGEngine,
    graph: Neo4jClient,
    vector: VectorClient,
) -> APIRouter:
    """Create and return the streaming recall router."""
    router = APIRouter(tags=["streaming"])

    @router.post("/memory/recall/stream")
    async def recall_stream(
        request: Request,
        query: ContextQuery,
        redis_client: Annotated[redis.asyncio.Redis, Depends(_get_redis)],
    ) -> StreamingResponse:
        """Stream recall results as Server-Sent Events."""
        principal = request_principal(request)
        sid = request.headers.get("X-Session-Id", "unknown")
        aid = request.headers.get("X-Agent-Id", "unknown")

        async def event_generator():
            # SP0 B2 / D1 parity: the same accumulation the non-streaming path
            # uses (`main.py`: `s.metadata.get("id")`, truthy-filtered), built
            # up as source frames go by instead of over `result.sources`.
            accessed_ids: list[str] = []
            source_count = 0
            try:
                async for event in rag_engine.recall_streaming(
                    query,
                    workspace_id=principal["workspace_id"],
                    member_id=principal["member_id"],
                ):
                    event_type = event["type"]
                    data = json.dumps(event["data"], default=str)

                    if event_type == "source":
                        source_count += 1
                        mid = (event["data"].get("metadata") or {}).get("id")
                        if mid:
                            accessed_ids.append(mid)
                        yield f"event: sources\ndata: {data}\n\n"
                    elif event_type == "context":
                        yield f"event: context\ndata: {data}\n\n"
                    elif event_type == "done":
                        yield f"event: done\ndata: {data}\n\n"
            finally:
                # Runs on normal completion AND on client disconnect —
                # closing the SSE blind spot means the receipt must fire
                # either way, and it must never raise into the response.
                await _emit_stream_receipt(
                    redis_client, sid, aid, query, accessed_ids, source_count
                )

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
