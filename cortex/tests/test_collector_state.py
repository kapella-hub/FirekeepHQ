"""Tests for CollectorState (SP3 Task 5)."""
from __future__ import annotations
import pytest
from app.collectors.state import CollectorState


@pytest.fixture
def nondecoding_redis():
    import fakeredis.aioredis
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.mark.asyncio
async def test_version_roundtrip_non_decoding(nondecoding_redis):
    assert await CollectorState.seen_version("confluence", "p1", nondecoding_redis) == 0
    await CollectorState.record_version("confluence", "p1", 7, nondecoding_redis)
    v = await CollectorState.seen_version("confluence", "p1", nondecoding_redis)
    assert v == 7 and isinstance(v, int)


@pytest.mark.asyncio
async def test_run_record_roundtrip(nondecoding_redis):
    await CollectorState.record_run("confluence", seen=5, ingested=2, skipped=3,
                                    errors=0, health="ok", redis=nondecoding_redis)
    rec = await CollectorState.get_run("confluence", nondecoding_redis)
    assert rec["health"] == "ok" and rec["pages_ingested"] == 2 and isinstance(rec["pages_ingested"], int)
    assert rec["last_run"]


@pytest.mark.asyncio
async def test_get_run_absent_returns_none(nondecoding_redis):
    assert await CollectorState.get_run("nope", nondecoding_redis) is None
