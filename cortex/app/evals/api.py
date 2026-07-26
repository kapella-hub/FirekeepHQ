"""Eval REST API — FastAPI router mounted on Cortex."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.middleware import require_scope

from app.evals.compute import compute_session_eval
from app.evals.store import get_eval, get_eval_summary

logger = logging.getLogger(__name__)


def create_evals_router(get_replay_redis) -> APIRouter:
    """Create the evals API router.

    Args:
        get_replay_redis: FastAPI dependency returning async Redis for DB 6.
    """
    router = APIRouter(prefix="/evals", tags=["evals"])

    @router.get("/sessions/{session_id}")
    async def eval_for_session(
        session_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
    ) -> dict[str, Any]:
        """Get eval result for a specific session."""
        result = await get_eval(r, session_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"No eval found for session {session_id}")
        return result.model_dump(mode="json")

    @router.get("/summary")
    async def eval_summary(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Get aggregate eval metrics across recent sessions."""
        summary = await get_eval_summary(r, limit=limit)
        return summary.model_dump(mode="json")

    @router.post("/sessions/{session_id}/compute")
    async def compute_eval(
        session_id: str,
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Manually trigger eval computation for a session."""
        result = await compute_session_eval(r, session_id, trigger="manual")
        if not result:
            raise HTTPException(status_code=404, detail="No replay events for this session or computation failed")
        return result.model_dump(mode="json")

    @router.get("/trends")
    async def get_trends(
        r=Depends(get_replay_redis),
        identity: dict = Depends(require_scope("eval:read")),
        window: int = Query(default=10, ge=3, le=50),
    ) -> dict[str, Any]:
        """Get quality trend indicators comparing recent vs previous sessions."""
        from app.self_diagnosis import compute_trends

        return await compute_trends(r, window_size=window)

    return router
