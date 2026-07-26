"""Self-diagnosis -- detect quality trends and regressions across sessions."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


async def compute_trends(replay_redis: aioredis.Redis, window_size: int = 10) -> dict[str, Any]:
    """Compare recent N sessions vs previous N sessions.

    Returns trend indicators for each metric:
    - "improving": recent avg > previous avg by >10%
    - "stable": within 10%
    - "degrading": recent avg < previous avg by >10%
    """
    # Read eval results from Redis — stored as rp:eval:{session_id} JSON blobs
    # Pattern matches the eval store in cortex/app/evals/store.py
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await replay_redis.scan(cursor, match="rp:eval:*", count=100)
        keys.extend(batch)
        if cursor == 0:
            break

    if len(keys) < 2 * window_size:
        return {"status": "insufficient_data", "sessions_available": len(keys), "required": 2 * window_size}

    # Read all eval results, sort by timestamp
    evals: list[dict[str, Any]] = []
    for key in keys:
        raw = await replay_redis.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            metrics = data.get("metrics")
            if not metrics or not isinstance(metrics, dict):
                continue
            metrics["_created"] = data.get("created_at", "")
            evals.append(metrics)
        except (json.JSONDecodeError, TypeError):
            continue

    evals.sort(key=lambda e: e.get("_created", ""), reverse=True)

    recent = evals[:window_size]
    previous = evals[window_size : 2 * window_size]

    if not recent or not previous:
        return {"status": "insufficient_data"}

    # Compare averages for each metric
    metric_keys = [
        "tool_success_rate",
        "memory_read_count",
        "memory_write_count",
        "failure_rate",
        "event_count",
        "session_duration_ms",
    ]

    trends: dict[str, str] = {}
    for mk in metric_keys:
        recent_vals = [e.get(mk, 0) for e in recent if mk in e]
        prev_vals = [e.get(mk, 0) for e in previous if mk in e]

        if not recent_vals or not prev_vals:
            trends[mk] = "no_data"
            continue

        recent_avg = sum(recent_vals) / len(recent_vals)
        prev_avg = sum(prev_vals) / len(prev_vals)

        if prev_avg == 0:
            trends[mk] = "stable" if recent_avg == 0 else "improving"
        else:
            change = (recent_avg - prev_avg) / abs(prev_avg)
            if change > 0.1:
                trends[mk] = "improving"
            elif change < -0.1:
                trends[mk] = "degrading"
            else:
                trends[mk] = "stable"

    return {
        "status": "ok",
        "window_size": window_size,
        "sessions_analyzed": len(recent) + len(previous),
        "trends": trends,
    }


async def detect_regressions(replay_redis: aioredis.Redis, threshold: float = 0.2) -> list[dict]:
    """Flag metrics that have degraded beyond threshold."""
    result = await compute_trends(replay_redis)
    if result.get("status") != "ok":
        return []

    regressions = []
    for metric, trend in result.get("trends", {}).items():
        if trend == "degrading":
            regressions.append({"metric": metric, "trend": "degrading"})

    return regressions
