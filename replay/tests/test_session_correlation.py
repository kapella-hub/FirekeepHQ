"""Verify that events emitted with a session_id land in the correct session index."""
import pytest
import pytest_asyncio
import fakeredis.aioredis
from replay.emitter import init_emitter, emit, close_emitter
from replay.reader import get_session_timeline
from replay.config import ReplaySettings


@pytest_asyncio.fixture
async def replay_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = ReplaySettings(ENABLED=True, REDIS_URL="redis://fake")
    await init_emitter(redis_client=r, settings=settings)
    yield r
    await close_emitter()
    await r.aclose()


class TestSessionCorrelation:
    @pytest.mark.asyncio
    async def test_events_indexed_by_session(self, replay_redis):
        await emit("memory_read", "sess-123", "agent-1", {"query": "test"})
        await emit("memory_write", "sess-123", "agent-1", {"action": "learned"})
        await emit("memory_read", "sess-456", "agent-2", {"query": "other"})

        timeline_123 = await get_session_timeline(replay_redis, "sess-123", limit=10)
        timeline_456 = await get_session_timeline(replay_redis, "sess-456", limit=10)

        assert timeline_123["total"] == 2
        assert timeline_456["total"] == 1

    @pytest.mark.asyncio
    async def test_unknown_session_not_mixed(self, replay_redis):
        await emit("memory_read", "sess-real", "agent-1", {"query": "q"})
        await emit("memory_write", "unknown", "unknown", {"action": "stale"})

        timeline = await get_session_timeline(replay_redis, "sess-real", limit=10)
        assert timeline["total"] == 1
