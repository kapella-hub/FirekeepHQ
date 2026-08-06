"""GET /procedures — what the dashboard reads.

Scope note: unlike the skills router (which declares no dependencies= and
contains no require_scope at all), these routes are gated. Accepting a proposal
is still a PATCH /skills/{id} and therefore still as ungated as it is today —
retrofitting a gate onto a shipped surface belongs in its own change.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.procedures import store

logger = logging.getLogger(__name__)


def create_procedures_router(get_redis, get_vector, settings_fn) -> APIRouter:
    router = APIRouter(tags=["procedures"])

    try:
        from auth.middleware import require_scope
        read_dep = [Depends(require_scope("memory:read"))]
        admin_dep = [Depends(require_scope("admin"))]
    except Exception:  # noqa: BLE001 — auth optional in unit tests
        read_dep, admin_dep = [], []

    @router.get("/procedures", dependencies=read_dep)
    async def list_procedures():
        r = get_redis()
        index = await store.load_index(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)

        rows = []
        for skill_id, entries in by_skill.items():
            stats = await store.get_step_stats(r, skill_id)
            proposals = await store.list_proposals(r, skill_id)
            executions = max(
                [s.get("executions", 0) for s in stats.values()] or [0]
            )
            rows.append({
                "skill_id": skill_id,
                "trigger": entries[0].get("skill_trigger", ""),
                # Coverage is REPORTED, never hidden: a step with no matcher is
                # unobservable, and a coverage number the user cannot see is the
                # same silent cap this repo bans elsewhere.
                "observable_steps": len(entries),
                "executions": executions,
                "steps": stats,
                "proposals": proposals,
            })
        rows.sort(key=lambda x: (-x["executions"], x["skill_id"]))
        return {"procedures": rows, "count": len(rows), "specs_total": len(index)}

    @router.get("/procedures/{skill_id}/executions", dependencies=read_dep)
    async def list_executions(skill_id: str, limit: int = 50):
        r = get_redis()
        all_execs = await store.iter_executions(r)
        mine = [e for e in all_execs if e.get("skill_id") == skill_id]
        mine.sort(key=lambda e: e.get("last_seen_at") or "", reverse=True)
        return {"executions": mine[:limit], "count": len(mine)}

    @router.post("/procedures/proposals/{proposal_id}/dismiss", dependencies=admin_dep)
    async def dismiss(proposal_id: str):
        r = get_redis()
        if not await store.dismiss_proposal(r, proposal_id):
            raise HTTPException(status_code=404, detail="Proposal not found")
        return {"dismissed": proposal_id}

    return router
