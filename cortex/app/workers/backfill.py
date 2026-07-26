"""Backfill queue for vector-less memories (SP0 fix A2, defect #2).

When /memory/learn's vector write fails after all embedding retries, the
memory is enqueued on a Redis stream (DB 0, key "memory:backfill"). A Celery
beat task drains the queue every 60 seconds, re-attempting embed + upsert
with per-entry exponential backoff capped at 1 hour. Entries exceeding
BACKFILL_MAX_ATTEMPTS move to the dead-letter list "memory:backfill:dlq",
surfaced by /health and the memory_health MCP tool.

Principle: a boundary may retry, or it may fail loudly — never silently.
No memory is ever silently vector-less: it is either backfilled or visible
in the DLQ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio

from app.config import get_settings
from app.workers.sleep_cycle import celery_app

logger = logging.getLogger(__name__)

BACKFILL_STREAM_KEY = "memory:backfill"
BACKFILL_DLQ_KEY = "memory:backfill:dlq"

_DRAIN_BATCH_SIZE = 100
_BACKOFF_BASE_SECONDS = 60.0
_BACKOFF_CAP_SECONDS = 3600.0


async def enqueue_backfill(
    memory_id: str,
    text: str,
    payload: dict,
    redis_client=None,
) -> None:
    """Enqueue a vector-less memory for background embedding backfill.

    Args:
        memory_id: Deterministic point id (uuid5 of text) the vector will get.
        text: The memory text to embed.
        payload: The upsert metadata dict; may include "namespace" which the
            drain pops out and passes as the upsert namespace arg.
        redis_client: Optional async Redis client (DB 0). A fresh connection
            from settings.REDIS_URL is created (and closed) when omitted.
    """
    close_after = False
    if redis_client is None:
        redis_client = redis.asyncio.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
        close_after = True
    try:
        await redis_client.xadd(
            BACKFILL_STREAM_KEY,
            {
                "memory_id": memory_id,
                "text": text,
                "payload": json.dumps(payload),
                "attempts": "0",
                "next_attempt_at": "0",
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    finally:
        if close_after:
            await redis_client.aclose()


async def _drain(redis_client=None, vector_client=None) -> dict[str, Any]:
    """Drain due entries from the backfill stream.

    For each due entry: embed + upsert via VectorClient. On success the entry
    is removed. On failure it is re-queued with attempts+1 and a backoff
    (60s * 2^attempts, capped at 1h); entries reaching BACKFILL_MAX_ATTEMPTS
    move to the DLQ list instead.
    """
    settings = get_settings()
    close_redis = redis_client is None
    close_vector = vector_client is None
    if redis_client is None:
        redis_client = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    if vector_client is None:
        from app.db.vector import VectorClient

        vector_client = VectorClient(settings)

    drained = retried = dead_lettered = 0
    try:
        entries = await redis_client.xrange(
            BACKFILL_STREAM_KEY, min="-", max="+", count=_DRAIN_BATCH_SIZE
        )
        now = time.time()
        for entry_id, fields in entries:
            try:
                next_attempt_at = float(fields.get("next_attempt_at") or 0.0)
            except (TypeError, ValueError):
                next_attempt_at = 0.0
            if next_attempt_at > now:
                continue
            try:
                current_attempts = int(fields.get("attempts") or 0)
            except (TypeError, ValueError):
                current_attempts = 0
            try:
                payload = json.loads(fields.get("payload") or "{}")
                namespace = payload.pop("namespace", "default")
                await vector_client.upsert(
                    text=fields["text"], metadata=payload, namespace=namespace
                )
                await redis_client.xdel(BACKFILL_STREAM_KEY, entry_id)
                drained += 1
                logger.info("Backfilled vector for memory %s", fields.get("memory_id"))
            except Exception as exc:
                attempts = current_attempts + 1
                # Persist-then-delete: write the replacement state (DLQ record
                # or requeued entry) BEFORE xdel'ing the original. If Redis
                # blips between the two writes we're left with a harmless
                # duplicate (point ids are uuid5(text), upserts idempotent)
                # instead of a silently lost, permanently vector-less memory.
                if attempts >= settings.BACKFILL_MAX_ATTEMPTS:
                    await redis_client.lpush(
                        BACKFILL_DLQ_KEY,
                        json.dumps(
                            {
                                "memory_id": fields.get("memory_id", ""),
                                "text": fields.get("text", ""),
                                "payload": fields.get("payload", "{}"),
                                "attempts": attempts,
                                "last_error": str(exc)[:500],
                                "failed_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ),
                    )
                    await redis_client.ltrim(BACKFILL_DLQ_KEY, 0, settings.DLQ_MAX_SIZE - 1)
                    await redis_client.xdel(BACKFILL_STREAM_KEY, entry_id)
                    dead_lettered += 1
                    logger.error(
                        "Backfill for memory %s exceeded %d attempts — moved to DLQ %s: %s",
                        fields.get("memory_id"),
                        settings.BACKFILL_MAX_ATTEMPTS,
                        BACKFILL_DLQ_KEY,
                        exc,
                    )
                else:
                    backoff = min(
                        _BACKOFF_BASE_SECONDS * (2 ** attempts), _BACKOFF_CAP_SECONDS
                    )
                    await redis_client.xadd(
                        BACKFILL_STREAM_KEY,
                        {
                            **fields,
                            "attempts": str(attempts),
                            "next_attempt_at": str(now + backoff),
                        },
                    )
                    await redis_client.xdel(BACKFILL_STREAM_KEY, entry_id)
                    retried += 1
                    logger.warning(
                        "Backfill attempt %d/%d for memory %s failed (%s); "
                        "next attempt in %.0fs",
                        attempts,
                        settings.BACKFILL_MAX_ATTEMPTS,
                        fields.get("memory_id"),
                        exc,
                        backoff,
                    )
        return {
            "status": "completed",
            "drained": drained,
            "retried": retried,
            "dead_lettered": dead_lettered,
        }
    finally:
        if close_vector:
            try:
                await vector_client.close()
            except Exception:
                pass
        if close_redis:
            try:
                await redis_client.aclose()
            except Exception:
                pass


async def requeue_dlq(redis_client=None, limit: int = 1000) -> dict[str, Any]:
    """Requeue dead-lettered backfill entries onto the stream, attempts reset.

    Manual recovery path (POST /ops/dlq/requeue) for entries that exhausted
    BACKFILL_MAX_ATTEMPTS while the embedding backend was down: pops the
    OLDEST records first (rpop; the DLQ is lpush'd) and re-XADDs each with
    attempts=0 so the 60s drain re-attempts embed + upsert.

    Pop-then-write rather than the drain's persist-then-delete: an atomic rpop
    makes concurrent requeue calls (e.g. a double-clicked dashboard button)
    safe from double-processing. If the stream write then fails, the record is
    lpush'd back and the loop stops; if even that restore fails the record is
    logged in full — never silently dropped. Malformed (non-JSON / non-dict)
    records are pushed back and counted, not requeued and not discarded. The
    iteration bound of min(limit, initial length) means each record present at
    call time is popped at most once, so kept-back malformed records can't
    spin the loop.
    """
    close_after = redis_client is None
    if redis_client is None:
        redis_client = redis.asyncio.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
    requeued = failed = malformed = 0
    try:
        initial_len = int(await redis_client.llen(BACKFILL_DLQ_KEY))
        for _ in range(min(limit, initial_len)):
            raw = await redis_client.rpop(BACKFILL_DLQ_KEY)
            if raw is None:
                break
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                record = None
            if not isinstance(record, dict):
                try:
                    await redis_client.lpush(BACKFILL_DLQ_KEY, raw)
                except Exception:
                    failed += 1
                    logger.critical(
                        "Malformed backfill DLQ record LOST during requeue "
                        "(restore failed): %s",
                        raw,
                    )
                    break
                malformed += 1
                logger.error(
                    "Malformed backfill DLQ record kept in place: %.200s", raw
                )
                continue
            payload = record.get("payload") or "{}"
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload)
            try:
                await redis_client.xadd(
                    BACKFILL_STREAM_KEY,
                    {
                        "memory_id": str(record.get("memory_id", "")),
                        "text": str(record.get("text", "")),
                        "payload": payload,
                        "attempts": "0",
                        "next_attempt_at": "0",
                        "enqueued_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                requeued += 1
            except Exception as exc:
                failed += 1
                try:
                    await redis_client.lpush(BACKFILL_DLQ_KEY, raw)
                except Exception:
                    logger.critical(
                        "Backfill DLQ record LOST during requeue "
                        "(stream write and restore both failed): %s",
                        raw,
                    )
                logger.error("Backfill DLQ requeue stream write failed — stopping: %s", exc)
                break
        remaining = int(await redis_client.llen(BACKFILL_DLQ_KEY))
        return {
            "status": "completed",
            "requeued": requeued,
            "failed": failed,
            "malformed_kept": malformed,
            "remaining": remaining,
        }
    finally:
        if close_after:
            await redis_client.aclose()


@celery_app.task(name="app.workers.backfill.drain_backfill_queue")
def drain_backfill_queue() -> dict[str, Any]:
    """Celery beat entrypoint — drain the backfill stream (runs every 60s)."""
    try:
        return asyncio.run(_drain())
    except Exception:
        logger.exception("Unhandled error in drain_backfill_queue")
        return {"status": "error", "drained": 0, "retried": 0, "dead_lettered": 0}
