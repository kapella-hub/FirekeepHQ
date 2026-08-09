"""GET /autopilot/inbox and GET /autopilot/digest — the operator's two reads.

Both are ADMIN-scoped. This is an operator surface, not an agent one: the inbox
joins five subsystems' review queues, including session ids and error strings
from the eval DLQ, and the digest describes the whole store's activity. Neither
is something an agent key holding `memory:read` has any reason to see.

The auth dependencies are built in a `try/except` (the `procedures/api.py`
idiom) because `auth` is optional in unit tests — and, as the procedures suite
learned, that idiom will silently serve every route ungated if the import ever
breaks. `test_autopilot_api.py::test_both_routes_are_admin_scoped` is what makes
that impossible to ship unnoticed.

Clients arrive as CLOSURES over `app.state`, not as captured values, matching
the procedures router's wiring: this factory runs inside the lifespan, and a
client replaced later (reconnect, re-init) must be picked up rather than pinned
at registration time. The replay Redis is a SEPARATE getter from the main one —
the eval DLQ lives on `app.state.replay_redis` and nowhere else, and passing the
main client would make the DLQ section silently always-empty rather than fail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from app.autopilot import digest as digest_mod
from app.autopilot import inbox as inbox_mod

logger = logging.getLogger(__name__)


def create_autopilot_router(get_redis, get_replay_redis, get_vector, settings_fn) -> APIRouter:
    router = APIRouter(prefix="/autopilot", tags=["autopilot"])

    try:
        from auth.middleware import require_scope
        admin_dep = [Depends(require_scope("admin"))]
    except Exception:  # noqa: BLE001 — auth optional in unit tests
        admin_dep = []

    async def _section(name: str, coro):
        """Run one section, degrade it in place if it fails.

        One broken store must not cost the operator the other four queues —
        `run_doctor`'s philosophy, and the whole reason this surface is worth
        having: the day Qdrant is unhappy is a day the eval DLQ is especially
        worth reading.
        """
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.exception("Autopilot inbox section %s failed", name)
            return {"count": 0, "error": str(exc)[:200]}

    @router.get("/inbox", dependencies=admin_dep)
    async def inbox():
        vector = get_vector()
        redis_client = get_redis()
        replay_redis = get_replay_redis()
        settings = settings_fn()

        items = {
            "draft_skills": await _section(
                "draft_skills", inbox_mod.draft_skills(vector, settings)),
            "stale_skills": await _section(
                "stale_skills", inbox_mod.stale_skills(vector, settings)),
            "rereview_skills": await _section(
                "rereview_skills", inbox_mod.rereview_skills(vector, settings)),
            "procedure_proposals": await _section(
                "procedure_proposals", inbox_mod.procedure_proposals(redis_client, settings)),
            "contested_memories": await _section(
                "contested_memories", inbox_mod.contested_memories(vector, settings)),
            "eval_dlq": await _section("eval_dlq", inbox_mod.eval_dlq(replay_redis)),
        }

        # A total that quietly omits a section that FAILED is the confident
        # wrong signal this repo bans: "3 things to do" reads identically
        # whether the other three queues are empty or unreadable. So the total
        # is the sum of what was actually counted, and `degraded` names every
        # queue that contributed nothing because it could not be read.
        degraded = sorted(k for k, v in items.items() if v.get("error"))
        total = sum(int(v.get("count", 0) or 0) for v in items.values())

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": items,
            "total_actionable": total,
        }
        if degraded:
            payload["degraded"] = degraded
        return payload

    @router.get("/digest", dependencies=admin_dep)
    async def digest(days: int = Query(default=7, ge=digest_mod.MIN_DAYS,
                                       le=digest_mod.MAX_DAYS)):
        return await digest_mod.build_digest(
            get_vector(), get_redis(), settings_fn(), days=days,
        )

    return router
