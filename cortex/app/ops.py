"""Operational visibility endpoints for workers and queues.

Consolidates the previously-inline ``/ops/workers`` and ``/ops/queues`` handlers
from ``main.py`` into a router. Response shapes are kept byte-for-byte compatible
with the dashboard consumers in ``dashboard/index.html`` (``loadOps()``):

- ``/ops/workers`` -> ``{"workers": [{"name", "active_tasks", ...}], "count"}``
- ``/ops/queues``  -> ``{"queues": {"celery": int, "event_stream": int, "event_dlq": int, "memory_backfill": int, "memory_backfill_dlq": int, "distill_dlq": int}}``

The dashboard reads ``worker.name`` / ``worker.active_tasks`` and iterates
``Object.keys(data.queues)`` treating ``queues`` as a name->depth dict, so those
contracts must not change.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from auth.middleware import require_scope

from app.config import get_settings
from app.workers.backfill import requeue_dlq
from app.workers.sleep_cycle import celery_app

logger = logging.getLogger(__name__)


def _inspect_workers(inspect) -> list[dict[str, Any]]:
    """Build a normalized worker list from Celery inspect results.

    Includes ``active_tasks`` / ``active_task_names`` aliases so the dashboard
    (which reads ``worker.active_tasks``) renders without a remap step.
    """
    stats = inspect.stats() or {}
    active = inspect.active() or {}
    reserved = inspect.reserved() or {}
    scheduled = inspect.scheduled() or {}
    registered = inspect.registered() or {}

    worker_names = sorted(set(stats) | set(active) | set(reserved) | set(scheduled) | set(registered))
    workers = []
    for name in worker_names:
        worker_stats = stats.get(name) or {}
        active_list = active.get(name) or []
        workers.append({
            "name": name,
            "status": "online",
            "pool": ((worker_stats.get("pool") or {}) or {}).get("implementation"),
            "processes": len(((worker_stats.get("pool") or {}) or {}).get("processes") or []),
            "active": len(active_list),
            "active_tasks": len(active_list),  # dashboard alias
            "active_task_names": [t.get("name", "?") for t in active_list if isinstance(t, dict)],
            "reserved": len(reserved.get(name) or []),
            "scheduled": len(scheduled.get(name) or []),
            "registered_tasks": len(registered.get(name) or []),
            "total_tasks": sum((worker_stats.get("total") or {}).values()),
        })
    return workers


async def _get_queue_depths(redis_client, queue_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read queue depths from a Redis broker connection."""
    queues = []
    for queue_name in queue_names:
        try:
            depth = await redis_client.llen(queue_name)
        except Exception as exc:
            logger.warning("Queue depth lookup failed for %s: %s", queue_name, exc)
            depth = 0
        queues.append({"name": queue_name, "depth": depth})
    return queues


async def collect_queue_depths() -> dict[str, int]:
    """Read all dashboard/briefing queue depths as a flat name->depth dict.

    Extracted from the /ops/queues handler so the briefing dlq section can
    reuse the exact same multi-DB reads (Celery broker + data DB backfill +
    bridge DB 3 distill) without an HTTP hop.
    """
    settings = get_settings()
    event_key = settings.REDIS_STREAM_KEY

    # Only the Celery broker queue lives on CELERY_BROKER_URL. The event
    # stream + DLQ live on the data DB (REDIS_URL) — producer (main.py) and
    # consumer (sleep_cycle) both use it; reading them from the broker DB
    # returned 0 forever and hid the event_dlq row everywhere downstream.
    redis_client = aioredis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    try:
        depths = await _get_queue_depths(redis_client, ("celery",))
    finally:
        await redis_client.aclose()

    backfill_depth = 0
    backfill_dlq_depth = 0
    data_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        depths += await _get_queue_depths(
            data_client, (event_key, f"{event_key}:dlq"),
        )
        try:
            backfill_depth = await data_client.xlen("memory:backfill")
        except Exception as exc:
            logger.warning("Backfill stream depth lookup failed: %s", exc)
        try:
            backfill_dlq_depth = await data_client.llen("memory:backfill:dlq")
        except Exception as exc:
            logger.warning("Backfill DLQ depth lookup failed: %s", exc)
    finally:
        await data_client.aclose()

    distill_dlq_depth = 0
    bridge_db_url = settings.REDIS_URL.rsplit("/", 1)[0] + "/3"
    bridge_client = aioredis.from_url(bridge_db_url, decode_responses=True)
    try:
        try:
            distill_dlq_depth = await bridge_client.llen("nb:distill:dlq")
        except Exception as exc:
            logger.warning("Distill DLQ depth lookup failed: %s", exc)
    finally:
        await bridge_client.aclose()

    by_name = {d["name"]: d["depth"] for d in depths}
    return {
        "celery": by_name.get("celery", 0),
        "event_stream": by_name.get(event_key, 0),
        "event_dlq": by_name.get(f"{event_key}:dlq", 0),
        "memory_backfill": backfill_depth,
        "memory_backfill_dlq": backfill_dlq_depth,
        "distill_dlq": distill_dlq_depth,
    }


async def retry_event_dlq(redis_client=None, limit: int = 1000) -> dict[str, Any]:
    """Move dead-lettered sleep-cycle event batches back onto the event queue.

    rpop oldest-first from ``{REDIS_STREAM_KEY}:dlq`` → lpush onto
    ``REDIS_STREAM_KEY`` (the consumer rpops, so order stays FIFO). Items are
    opaque strings — no parse step, no malformed branch. Guarded restore: a
    popped item whose queue write fails is lpush'd back; if even that fails
    it is logged IN FULL at CRITICAL, never silently dropped. Both keys live
    on the data DB (REDIS_URL), same as producer and consumer.
    """
    settings = get_settings()
    event_key = settings.REDIS_STREAM_KEY
    dlq_key = f"{event_key}:dlq"
    close_after = redis_client is None
    if redis_client is None:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    requeued = failed = 0
    try:
        initial_len = int(await redis_client.llen(dlq_key))
        for _ in range(min(limit, initial_len)):
            raw = await redis_client.rpop(dlq_key)
            if raw is None:
                break
            try:
                await redis_client.lpush(event_key, raw)
                requeued += 1
            except Exception as exc:
                failed += 1
                try:
                    await redis_client.lpush(dlq_key, raw)
                except Exception:
                    logger.critical(
                        "Event DLQ record LOST during retry "
                        "(queue write and restore both failed): %s", raw,
                    )
                logger.error("Event DLQ retry queue write failed — stopping: %s", exc)
                break
        remaining = int(await redis_client.llen(dlq_key))
        return {
            "status": "completed",
            "requeued": requeued,
            "failed": failed,
            "remaining": remaining,
        }
    finally:
        if close_after:
            await redis_client.aclose()


def create_ops_router() -> APIRouter:
    """Create the operations router (dashboard-compatible response shapes)."""
    router = APIRouter(prefix="/ops", tags=["ops"])

    @router.get("/workers")
    async def get_workers() -> dict[str, Any]:
        """Celery worker status. The inspect broadcasts are blocking (~10s total),
        so run them in a threadpool — otherwise they stall the event loop and every
        other concurrent /ops and /admin request behind them."""
        def _collect() -> list[dict[str, Any]]:
            return _inspect_workers(celery_app.control.inspect(timeout=2.0))
        try:
            workers = await run_in_threadpool(_collect)
        except Exception as exc:
            logger.warning("Celery inspect failed: %s", exc)
            return {"workers": [], "count": 0, "error": str(exc)}
        return {"workers": workers, "count": len(workers)}

    @router.get("/queues")
    async def get_queues() -> dict[str, Any]:
        """Redis queue depths for Celery broker, event stream, event DLQ,
        and the SP0 memory-backfill stream + dead-letter queue."""
        return {"queues": await collect_queue_depths()}

    @router.post("/dlq/requeue")
    async def post_dlq_requeue(
        limit: int = Query(default=1000, ge=1, le=10_000),
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Requeue memory-backfill DLQ entries for re-embedding.

        Covers memory_backfill_dlq only: entries dead-lettered while the
        embedding backend was down have no automatic path back onto the
        stream (event_dlq: POST /ops/dlq/retry-events below; distill_dlq:
        Bridge POST /ops/distill-dlq/requeue). Admin-scoped like the policy
        toggle.
        """
        result = await requeue_dlq(limit=limit)
        return {"queue": "memory_backfill_dlq", **result}

    @router.post("/dlq/retry-events")
    async def post_event_dlq_retry(
        limit: int = Query(default=1000, ge=1, le=10_000),
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Retry dead-lettered sleep-cycle event batches (admin-scoped sibling
        of POST /dashboard/api/dlq/retry).

        REVERSED 2026-07-26: /dashboard/api/dlq/retry used to be documented
        as a deliberate key-free exception "for the embedded SPA". That
        rationale no longer holds — closing the unauthenticated-dashboard
        hole (app/main.py's AUTH_SKIP_EXACT_PATHS) also gates
        GET /dashboard/api/{memories,graph,stats,dlq}, so the embedded
        cortex-native SPA (app/static/dashboard.html) can no longer populate
        its DLQ tab at all; a key-free Retry button next to a tab that can't
        load protects nothing and stayed a real unauthenticated
        mutating endpoint. It is now auth-gated like every other route
        under /dashboard/api/*."""
        result = await retry_event_dlq(limit=limit)
        return {"queue": "event_dlq", **result}

    return router
