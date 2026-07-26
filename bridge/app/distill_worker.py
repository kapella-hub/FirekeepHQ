"""Queued distillation worker — drains nb:distill:queue with retry/backoff (SP0 D1).

ctx_complete_session enqueues a distillation job instead of distilling inline.
This worker retries each job up to MAX_ATTEMPTS with exponential backoff
(capped at 1h); permanent failures move to the nb:distill:dlq list. The
session-key 7-day TTL is applied ONLY after confirmed success or DLQ move,
so a failing distillation can never silently lose session knowledge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import Settings, get_settings
from app.distiller import Distiller
from app.redis_client import get_redis
from app.session import SessionManager

logger = logging.getLogger(__name__)

QUEUE_KEY = "nb:distill:queue"
DLQ_KEY = "nb:distill:dlq"
MAX_ATTEMPTS = 10
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600
POLL_INTERVAL_SECONDS = 5.0

_distiller: Distiller | None = None


def _get_distiller() -> Distiller:
    global _distiller
    if _distiller is None:
        _distiller = Distiller(get_settings())
    return _distiller


async def close_distiller() -> None:
    """Close the module-level Distiller's HTTP client (lifespan shutdown)."""
    global _distiller
    if _distiller is not None:
        await _distiller.close()
        _distiller = None


async def enqueue_distillation(
    redis: aioredis.Redis,
    session_id: str,
    attempts: int = 0,
    next_attempt_at: float | None = None,
) -> str:
    """Add a distillation job to the queue stream. Returns the stream entry ID."""
    return await redis.xadd(
        QUEUE_KEY,
        {
            "session_id": session_id,
            "attempts": str(attempts),
            "next_attempt_at": str(
                next_attempt_at if next_attempt_at is not None else time.time()
            ),
        },
    )


async def requeue_dlq(
    redis: aioredis.Redis, settings: Settings, limit: int = 1000
) -> dict[str, Any]:
    """Requeue dead-lettered distillation jobs with attempts reset.

    Manual recovery path (POST /ops/distill-dlq/requeue) for jobs that
    exhausted MAX_ATTEMPTS, e.g. while the LLM backend was down. Pops the
    OLDEST records first (rpop; the worker lpush's) and re-enqueues via
    enqueue_distillation — never a hand-rolled xadd, the queue field shape
    stays in one place — with attempts=0; the running distill_worker_loop
    picks them up within ~POLL_INTERVAL_SECONDS.

    Sessions whose keys already hit the 7-day post-DLQ TTL cannot be
    re-distilled: those records are dropped with their FULL content logged
    (counted expired_dropped) rather than restored — restoring them would
    make the DLQ row un-drainable. Every other restore is guarded; a record
    whose write-back AND restore both fail is logged IN FULL at CRITICAL,
    never silently dropped. Iteration is bounded by min(limit, initial
    length) so kept-back malformed records cannot spin the loop.
    """
    mgr = SessionManager(redis, settings)
    requeued = failed = malformed = expired = 0
    initial_len = int(await redis.llen(DLQ_KEY))
    for _ in range(min(limit, initial_len)):
        raw = await redis.rpop(DLQ_KEY)
        if raw is None:
            break
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            record = None
        session_id = record.get("session_id", "") if isinstance(record, dict) else ""
        if not session_id:
            try:
                await redis.lpush(DLQ_KEY, raw)
            except Exception:
                failed += 1
                logger.critical(
                    "Malformed distill DLQ record LOST during requeue "
                    "(restore failed): %s", raw,
                )
                break
            malformed += 1
            logger.error("Malformed distill DLQ record kept in place: %.200s", raw)
            continue
        try:
            data = await mgr.get_session_data(session_id)
        except Exception as exc:
            failed += 1
            try:
                await redis.lpush(DLQ_KEY, raw)
                logger.error(
                    "Distill DLQ requeue session lookup failed — record "
                    "restored, stopping: %s", exc,
                )
            except Exception:
                logger.critical(
                    "Distill DLQ record LOST during requeue "
                    "(lookup and restore both failed): %s", raw,
                )
            break
        if data is None:
            expired += 1
            logger.warning(
                "Distill DLQ record dropped — session %s no longer exists "
                "(post-DLQ 7d TTL elapsed): %s", session_id, raw,
            )
            continue
        try:
            await enqueue_distillation(redis, session_id, attempts=0)
        except Exception as exc:
            failed += 1
            try:
                await redis.lpush(DLQ_KEY, raw)
                logger.error(
                    "Distill DLQ requeue enqueue failed — record restored, "
                    "stopping: %s", exc,
                )
            except Exception:
                logger.critical(
                    "Distill DLQ record LOST during requeue "
                    "(enqueue and restore both failed): %s", raw,
                )
            break
        try:
            await mgr.set_distillation_status(session_id, "queued")
        except Exception as exc:
            logger.warning(
                "Requeued distillation for %s but status update failed: %s",
                session_id, exc,
            )
        requeued += 1
    try:
        remaining = int(await redis.llen(DLQ_KEY))
    except Exception:
        remaining = -1
    return {"status": "completed", "requeued": requeued, "failed": failed,
            "malformed_kept": malformed, "expired_dropped": expired,
            "remaining": remaining}


async def process_queue_once(redis: aioredis.Redis, settings: Settings) -> int:
    """Process all due queue entries once. Returns the number of successes."""
    entries = await redis.xrange(QUEUE_KEY, min="-", max="+", count=100)
    if not entries:
        return 0

    now = time.time()
    mgr = SessionManager(redis, settings)
    succeeded = 0

    for entry_id, fields in entries:
        try:
            if float(fields.get("next_attempt_at", "0")) > now:
                continue  # backoff not elapsed yet
        except (TypeError, ValueError):
            pass

        session_id = fields.get("session_id", "")
        attempts = int(fields.get("attempts", "0") or 0)

        if not session_id:
            await redis.xdel(QUEUE_KEY, entry_id)
            continue

        data = await mgr.get_session_data(session_id)
        if data is None:
            logger.warning(
                "Distill job dropped: session %s no longer exists", session_id
            )
            await redis.xdel(QUEUE_KEY, entry_id)
            continue

        result = await _get_distiller().distill(
            data, outcome=data.get("outcome") or None, session_id=session_id
        )

        # Persist-then-delete: the terminal write (status + TTL, DLQ move, or
        # retry re-enqueue) MUST land before the queue entry is deleted. A
        # crash in the gap leaves a duplicate job (harmless — re-distilling
        # is idempotent enough) instead of a session silently stuck at
        # distillation="queued" forever: never TTL'd and pinned from cleanup
        # by _enforce_max_sessions.
        if result.get("status") == "success":
            await mgr.set_distillation_status(session_id, "success")
            await mgr.expire_session_keys(session_id)
            succeeded += 1
        else:
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                await redis.lpush(
                    DLQ_KEY,
                    json.dumps({
                        "session_id": session_id,
                        "attempts": attempts,
                        "error": result.get("error", "unknown"),
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                    }),
                )
                await mgr.set_distillation_status(session_id, "dlq")
                await mgr.expire_session_keys(session_id)
                logger.error(
                    "Distillation for session %s moved to DLQ after %d attempts: %s",
                    session_id, attempts, result.get("error", "unknown"),
                )
            else:
                backoff = min(BACKOFF_BASE_SECONDS * (2 ** attempts), BACKOFF_CAP_SECONDS)
                await enqueue_distillation(
                    redis, session_id, attempts=attempts, next_attempt_at=now + backoff
                )
                logger.warning(
                    "Distillation for session %s failed (attempt %d/%d), retry in %ds: %s",
                    session_id, attempts, MAX_ATTEMPTS, int(backoff),
                    result.get("error", "unknown"),
                )

        await redis.xdel(QUEUE_KEY, entry_id)

    return succeeded


async def distill_worker_loop(
    poll_interval: float = POLL_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running worker loop. Started from the MCP server lifespan."""
    logger.info("Distill worker started (queue=%s, poll=%ss)", QUEUE_KEY, poll_interval)
    while stop_event is None or not stop_event.is_set():
        try:
            redis = await get_redis()
            await process_queue_once(redis, get_settings())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Distill worker pass failed (will retry): %s", exc)
        await asyncio.sleep(poll_interval)
