"""GET /dreams — status endpoint for the Dreaming pass (round 1, additive-only).

Mirrors collectors/api.py's shape (SP3 precedent). One difference: DreamState
(app/dreams/state.py) is intentionally SYNCHRONOUS — it's built for the Celery
task's own sync `redis.Redis` client (see task.py's `_build_clients` and its
docstring), so handing it this app's async `get_redis` client would raise
`AttributeError: 'coroutine' object has no attribute 'items'` — redis-py's
async methods return unawaited coroutines, which are always truthy, so
DreamState.get_run()'s `or {}` guard never fires and the very next `.items()`
call crashes. This endpoint therefore reads the same `dreams:run` hash
directly via the async client instead of going through DreamState.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.dreams.state import COUNTER_KEY, RUN_KEY


def _s(v: Any) -> Any:
    return v.decode("utf-8") if isinstance(v, bytes) else v


def _int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def create_dreams_router() -> APIRouter:
    router = APIRouter(prefix="/dreams", tags=["dreams"])
    from app.main import get_redis

    @router.get("")
    async def get_dreams_status(redis_client=Depends(get_redis)):
        settings = get_settings()
        raw = await redis_client.hgetall(RUN_KEY)
        run = {_s(k): _s(v) for k, v in (raw or {}).items()}
        # `errors` is the ONLY one of the three counters that never reaches
        # the dreams:run hash: task.py writes clusters_done/profiles_done
        # through record_run() on the success paths, but the error path calls
        # bump_counter("errors") and then record_run(status="error", ...)
        # WITHOUT an errors field — so `run.get("errors")` was structurally
        # always absent and this endpoint reported 0 no matter how many ticks
        # had failed (final-review I4). Read the counter key itself rather
        # than mirroring it into the hash: hset merges and reset_progress
        # clears counters but not hash fields, so a mirrored value would
        # linger across runs long after the errors it described were gone.
        errors_raw = await redis_client.get(COUNTER_KEY.format(name="errors"))
        return {
            "enabled": bool(settings.DREAM_ENABLED),
            "last_run": run.get("last_run"),
            "clusters_done": _int(run.get("clusters_done")),
            "profiles_done": _int(run.get("profiles_done")),
            # Without this, a run that wrote 6 dreams and a run that wrote 0
            # were indistinguishable here — both reported
            # {"clusters_done":3,"profiles_done":2,"errors":0,"health":"ok"},
            # which is exactly how a live 2-of-3-clusters-produced-nothing run
            # passed for healthy. Read from the RUN HASH, not from
            # `dreams:counter:insights_written`, and deliberately unlike
            # `errors` above: the counter is per-run and reset_progress clears
            # it at completion, so reading the counter would report 0 for the
            # run that just finished — the moment an operator most wants the
            # number. task.py mirrors the cumulative counter into the hash on
            # every tick that does work, so the hash carries the current run's
            # running total and then the completed run's final one. (The
            # argument against mirroring `errors` doesn't apply: nothing ever
            # writes `errors` into the hash, so a mirror there would be a value
            # that only ever goes stale.)
            "insights_written": _int(run.get("insights_written")),
            "errors": _int(_s(errors_raw)),
            "health": run.get("health", "unknown"),
        }

    return router
