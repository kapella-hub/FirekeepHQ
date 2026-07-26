"""Async Redis connection for FirekeepRelay."""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from app.config import get_settings

_redis: aioredis.Redis | None = None
_redis_lock = asyncio.Lock()


async def get_redis() -> aioredis.Redis:
    """Return a shared async Redis client, creating on first call."""
    global _redis
    if _redis is None:
        async with _redis_lock:
            if _redis is None:
                settings = get_settings()
                _redis = aioredis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    max_connections=20,
                )
    return _redis


async def close_redis() -> None:
    """Close the Redis client."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
