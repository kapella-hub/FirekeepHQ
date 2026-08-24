"""Eval REST API — FastAPI router mounted on Cortex."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.middleware import require_any_scope, require_scope

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
        trigger: str = Query(
            default="manual",
            pattern=r"^[a-z_]{1,32}$",
            description="Who asked: 'manual' (a human) or 'session_complete' (Bridge).",
        ),
        task_result: str | None = Query(
            default=None,
            pattern=r"^(success|partial|failure)$",
            description="Structured task grade from the completing caller "
                        "(spec D8: survives a lost session_end emit; honored "
                        "only with the eval:grade scope).",
        ),
        identity: dict = Depends(require_any_scope("eval:write", "admin")),
    ) -> dict[str, Any]:
        """Trigger eval computation for a session.

        SCOPE: ``eval:write`` OR ``admin``. This was ``admin`` ALONE, and Bridge —
        which posts here on every ``ctx_complete_session`` carrying
        ``FIREKEEP_INTERNAL_KEY`` (scopes ``memory:write``, ``session:read``,
        ``eval:read``, ``eval:write``) — got HTTP 403 on EVERY session
        completion, treated 4xx as permanent, and never retried. Measured on
        the live deployment: all 19 stored evals had ``trigger="manual"`` and
        the newest was 12 days old against 54 completed sessions in the recent
        window. That silently starves everything downstream of eval outcomes —
        OWM's ranking signal, quality trends and regression detection, and the
        pattern A/B tip-effectiveness join.

        ``eval:write`` is not a widening: the scope already exists, is already
        granted to the internal key, and computing an eval for one session is
        exactly what it names. ``admin`` gates decrypted vault reads and key
        minting; nothing about this endpoint belongs in that class.

        ``admin`` is kept ALONGSIDE it rather than replaced by it, and that is
        not belt-and-braces — it is required. ``keys.scopes_allow`` treats only
        ``"*"`` as a superset, NOT ``"admin"``, so swapping the gate outright
        would have handed a 403 to every key holding a literal ``["admin"]``
        and no wildcard: the operator's own key, and the one that could call
        this endpoint before the change. Widening a gate must not narrow it for
        somebody else. ``require_any_scope`` (added for the vault read routes)
        is what expresses OR — FastAPI ANDs stacked dependencies, so two
        ``require_scope`` calls would demand both.

        ``trigger`` is a parameter rather than the hardcoded ``"manual"`` it
        used to be, so a stored eval records who actually asked for it and the
        "all evals are manual" signal above stays diagnosable.
        """
        result = await compute_session_eval(r, session_id, trigger=trigger,
                                            task_result_hint=task_result)
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
