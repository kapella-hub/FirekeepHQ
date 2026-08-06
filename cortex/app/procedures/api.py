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
        coverage = await store.load_coverage(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)

        # Rows come from the COVERAGE summary, not from the matcher index. The
        # index holds file_glob specs only, so deriving rows from it made a
        # procedure whose steps are all `unobservable` produce no row at all and
        # `specs_total: 0` — and the dashboard then renders the cold-start
        # message ("no procedure has step specs yet") at a human who has just
        # compiled them. H2 requires the opposite: say "0 of 7 observable". The
        # index is the fallback for a deployment that has not rebuilt since the
        # coverage key was introduced.
        summary = dict(coverage)
        for skill_id, entries in by_skill.items():
            summary.setdefault(skill_id, {
                "trigger": entries[0].get("skill_trigger", ""),
                "spec_count": len(entries),
                "observable": len(entries),
            })

        # Live, from the execution records, NOT from the nightly stats blob.
        # `GET /procedures/{id}/executions` reads the records, so the two
        # endpoints answered "has this ever run?" differently for up to a full
        # PROCEDURE_SCHEDULE_HOURS — and the rollup was the one that was wrong.
        exec_counts: dict[str, int] = {}
        for rec in await store.iter_executions(r):
            sid = rec.get("skill_id")
            if sid:
                exec_counts[sid] = exec_counts.get(sid, 0) + 1

        rows = []
        for skill_id, cov in summary.items():
            stats = await store.get_step_stats(r, skill_id)
            proposals = await store.list_proposals(r, skill_id)
            rows.append({
                "skill_id": skill_id,
                "trigger": cov.get("trigger", ""),
                # Coverage is REPORTED, never hidden: a step with no matcher is
                # unobservable, and a coverage number the user cannot see is the
                # same silent cap this repo bans elsewhere.
                "spec_count": int(cov.get("spec_count", 0)),
                "observable_steps": int(cov.get("observable", 0)),
                "executions": exec_counts.get(skill_id, 0),
                "steps": stats,
                "proposals": proposals,
            })
        rows.sort(key=lambda x: (-x["executions"], x["skill_id"]))
        return {
            "procedures": rows,
            "count": len(rows),
            "specs_total": sum(row["spec_count"] for row in rows),
            # What the pass did, and — the point of it — WHICH closed state
            # Tier B is in. Without this a deployment where the gate is shut for
            # lack of an outcome signal is byte-identical to one where it is
            # open and found nothing, with no last_run to say whether the pass
            # has ever executed (the /dreams precedent).
            "run": await store.get_run(r),
            # Spec §4 Stage 2: recognised work dropped for an unjoinable
            # session "is counted and surfaced, not hidden".
            "unjoinable_edits": await store.get_unjoinable(r),
        }

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
