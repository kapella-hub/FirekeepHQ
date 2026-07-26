"""Tests for the docs->skills per-source ingest-status store (SP2.1)."""
from __future__ import annotations

import pytest

from app.knowledge.status import set_ingest_status, get_ingest_status


@pytest.fixture
def nondecoding_redis():
    """Mirrors Cortex's app.state.redis_client (NO decode_responses) — the
    F2 bug class. The status store must normalize bytes on read."""
    import fakeredis.aioredis
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.mark.asyncio
async def test_roundtrip_with_non_decoding_client(nondecoding_redis):
    await set_ingest_status(
        "Widget Runbook", "classified",
        disposition="procedural", skills_queued=2, note="", redis_client=nondecoding_redis,
    )
    got = await get_ingest_status("Widget Runbook", redis_client=nondecoding_redis)
    assert got is not None
    assert got["status"] == "classified"
    assert got["disposition"] == "procedural"
    assert got["skills_queued"] == 2            # coerced to int, not "2"
    assert isinstance(got["skills_queued"], int)
    assert got["updated_at"]                     # ISO8601 stamp present


@pytest.mark.asyncio
async def test_get_absent_returns_none(nondecoding_redis):
    assert await get_ingest_status("nope", redis_client=nondecoding_redis) is None


@pytest.mark.asyncio
async def test_set_applies_ttl(nondecoding_redis):
    await set_ingest_status("S", "queued", redis_client=nondecoding_redis)
    ttl = await nondecoding_redis.ttl("knowledge:ingest_status:S")
    assert ttl > 0


@pytest.mark.asyncio
async def test_none_client_is_noop():
    await set_ingest_status("S", "queued", redis_client=None)   # must not raise
    assert await get_ingest_status("S", redis_client=None) is None
