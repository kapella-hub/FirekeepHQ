"""Eval storage — persist and query eval results in Redis DB 6.

Eval results are stored alongside replay data since they're derived from it.
"""

from __future__ import annotations

import logging
from typing import Any

import redis
import redis.asyncio as aioredis

from app.evals.models import EvalResult, EvalSummary

logger = logging.getLogger(__name__)

_EVAL_PREFIX = "rp:eval:"
_EVAL_INDEX = "rp:eval_index"  # Sorted set of session_ids scored by timestamp


async def store_eval(r: aioredis.Redis, result: EvalResult, ttl_days: int = 30) -> bool:
    """Store an eval result. Returns True on success, False if already exists or on error."""
    try:
        key = f"{_EVAL_PREFIX}{result.session_id}"
        data = result.model_dump_json()
        ttl = ttl_days * 86400

        if result.task_result is None:
            # D9a: ungraded writers are NX-create-only — no overwrite path
            # exists for them, under any interleaving.
            created = await r.set(key, data, ex=ttl, nx=True)
            if not created:
                logger.debug("Eval already exists for session %s, skipping",
                             result.session_id)
                return False
        else:
            # D9b: graded writes are first-graded-wins via WATCH/MULTI CAS.
            # A time-limited claim is NOT a correctness primitive (a writer
            # stalling past its TTL lets a successor acquire, then the first
            # writer overwrites from a stale read and deletes the successor's
            # lock — the fencing problem relay/app/leases.py already solves).
            # Here the decision (replace only a missing-or-ungraded record)
            # and the write are ONE atomic transaction, so a stale writer's
            # EXEC fails and it retries — no window, nothing to fence.
            for _attempt in range(8):
                try:
                    async with r.pipeline() as pipe:
                        await pipe.watch(key)
                        existing_raw = await pipe.get(key)
                        if existing_raw:
                            try:
                                existing = EvalResult.model_validate_json(existing_raw)
                                if existing.task_result is not None:
                                    await pipe.unwatch()
                                    logger.debug(
                                        "Graded eval already stored for %s, keeping it",
                                        result.session_id)
                                    return False
                            except Exception:
                                pass  # unparseable stored record: graded wins
                        pipe.multi()
                        pipe.set(key, data, ex=ttl)
                        await pipe.execute()
                        break
                except redis.WatchError:
                    continue          # the key changed under us; re-read and retry
            else:
                logger.warning("store_eval CAS exhausted retries for %s",
                               result.session_id)
                return False

        # Update the eval index (sorted set, score = timestamp)
        ts = result.created_at.timestamp()
        await r.zadd(_EVAL_INDEX, {result.session_id: ts})

        return True
    except Exception as e:
        logger.warning("Failed to store eval for %s: %s", result.session_id, e)
        return False


async def get_eval(r: aioredis.Redis, session_id: str) -> EvalResult | None:
    """Get eval result for a session."""
    try:
        key = f"{_EVAL_PREFIX}{session_id}"
        raw = await r.get(key)
        if not raw:
            return None
        return EvalResult.model_validate_json(raw)
    except Exception as e:
        logger.warning("Failed to read eval for %s: %s", session_id, e)
        return None


async def get_eval_summary(r: aioredis.Redis, limit: int = 50) -> EvalSummary:
    """Compute aggregate eval metrics across recent sessions."""
    try:
        # Get recent session IDs from the index
        session_ids = await r.zrevrange(_EVAL_INDEX, 0, limit - 1)
        if not session_ids:
            return EvalSummary()

        all_metrics: dict[str, list[float]] = {}
        total = 0
        with_failures = 0
        recent: list[dict[str, Any]] = []

        for sid in session_ids:
            eval_result = await get_eval(r, sid)
            if not eval_result:
                continue

            total += 1
            if eval_result.has_failures:
                with_failures += 1

            for metric_name, value in eval_result.metrics.items():
                if value is not None:
                    all_metrics.setdefault(metric_name, []).append(value)

            if len(recent) < 10:
                recent.append({
                    "session_id": eval_result.session_id,
                    "trigger": eval_result.trigger,
                    "event_count": eval_result.event_count,
                    "has_failures": eval_result.has_failures,
                    "metrics": eval_result.metrics,
                    "created_at": eval_result.created_at.isoformat(),
                })

        # Compute aggregates
        avg_metrics = {}
        metric_ranges = {}
        for name, values in all_metrics.items():
            if values:
                avg_metrics[name] = round(sum(values) / len(values), 4)
                metric_ranges[name] = {
                    "min": round(min(values), 4),
                    "max": round(max(values), 4),
                    "avg": avg_metrics[name],
                    "count": len(values),
                }

        return EvalSummary(
            total_sessions_evaluated=total,
            sessions_with_failures=with_failures,
            avg_metrics=avg_metrics,
            metric_ranges=metric_ranges,
            recent_evals=recent,
        )

    except Exception as e:
        logger.warning("Failed to compute eval summary: %s", e)
        return EvalSummary()
