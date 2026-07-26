"""Replay Engine REST API — FastAPI router mounted on Cortex.

Provides endpoints for querying trace events, inspecting context snapshots,
and running the narrowing algorithm for root cause analysis.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.middleware import require_scope

from replay.models import (
    NarrowingResponse,
    SessionSummaryResponse,
    TimelineResponse,
    TraceEventResponse,
)
from replay.reader import (
    get_context_at,
    get_event,
    get_session_summary,
    get_session_timeline,
)
from replay.narrowing import narrow

logger = logging.getLogger(__name__)


def create_replay_router(get_replay_redis) -> APIRouter:
    """Create the replay API router.

    Args:
        get_replay_redis: FastAPI dependency that returns an async Redis client
                         connected to DB 6 (replay).
    """
    router = APIRouter(prefix="/replay", tags=["replay"])

    @router.get("/sessions/{session_id}/events", response_model=TimelineResponse)
    async def replay_session_events(
        session_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("replay:read")),
        event_type: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """Get the event timeline for a session."""
        return await get_session_timeline(
            r, session_id, event_type=event_type, limit=limit, offset=offset,
        )

    @router.get("/events/{event_id}", response_model=TraceEventResponse)
    async def replay_get_event(
        event_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("replay:read")),
    ) -> dict[str, Any]:
        """Get a single trace event by ID."""
        event = await get_event(r, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    @router.get("/sessions/{session_id}/context-at/{event_id}")
    async def replay_context_at(
        session_id: str,
        event_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("replay:read")),
    ) -> dict[str, Any]:
        """Reconstruct the context snapshot at a specific event."""
        return await get_context_at(r, session_id, event_id)

    @router.get("/sessions/{session_id}/summary", response_model=SessionSummaryResponse)
    async def replay_session_summary(
        session_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("replay:read")),
    ) -> dict[str, Any]:
        """Get summary statistics for a session's trace."""
        return await get_session_summary(r, session_id)

    @router.post("/sessions/{session_id}/narrow", response_model=NarrowingResponse)
    async def replay_narrow(
        session_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("replay:read")),
        failure_event_id: str = Query(...),
        max_depth: int = Query(default=10, ge=1, le=50),
        max_results: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Run narrowing algorithm from a failure event.

        Walks backward through trace links, scoring ancestors by
        link confidence and proximity decay. Returns ranked suspect events.
        """
        return await narrow(
            r, session_id, failure_event_id,
            max_depth=max_depth, max_results=max_results,
        )

    return router
