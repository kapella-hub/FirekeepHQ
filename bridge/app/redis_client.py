"""Async Redis connection for FirekeepBridge."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return a shared async Redis client, creating on first call."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            # SP0 D7: a half-open connection must error within 5s instead of
            # blocking ctx_update/ctx_complete_session until process restart.
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    """Close the Redis client."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
