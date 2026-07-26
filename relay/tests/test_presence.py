"""Tests for agent presence registry."""

import time
import pytest
import pytest_asyncio
import fakeredis.aioredis


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_creates_presence(self, redis):
        from app.presence import register
        result = await register(redis, "agent-alpha", "Fix auth bug", "DESKTOP-MAIN")
        assert result["agent_id"] == "agent-alpha"
        assert result["goal"] == "Fix auth bug"
        assert result["hostname"] == "DESKTOP-MAIN"
        assert result["status"] == "active"
        data = await redis.hgetall("nr:presence:agent-alpha")
        assert data["agent_id"] == "agent-alpha"
        # No TTL — key persists until deregistered
        ttl = await redis.ttl("nr:presence:agent-alpha")
        assert ttl == -1  # -1 means no expiry

    @pytest.mark.asyncio
    async def test_register_with_session_id(self, redis):
        from app.presence import register
        result = await register(redis, "agent-alpha", "Fix auth bug", "DESKTOP-MAIN", session_id="abc-123")
        assert result["session_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_register_without_session_id(self, redis):
        from app.presence import register
        result = await register(redis, "agent-alpha", "Fix auth bug", "DESKTOP-MAIN")
        assert result["session_id"] == ""

    @pytest.mark.asyncio
    async def test_register_overwrites_existing(self, redis):
        from app.presence import register
        await register(redis, "agent-alpha", "Old goal", "OLD-HOST")
        result = await register(redis, "agent-alpha", "New goal", "NEW-HOST")
        assert result["goal"] == "New goal"
        data = await redis.hgetall("nr:presence:agent-alpha")
        assert data["goal"] == "New goal"

    @pytest.mark.asyncio
    async def test_register_adds_to_index(self, redis):
        from app.presence import register
        await register(redis, "agent-alpha", "goal", "host")
        score = await redis.zscore("nr:presence:__index", "agent-alpha")
        assert score is not None


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp(self, redis):
        from app.presence import register, heartbeat_presence
        await register(redis, "agent-alpha", "goal", "host")
        result = await heartbeat_presence(redis, "agent-alpha")
        assert result["refreshed"] is True
        # No TTL — key persists until deregistered
        ttl = await redis.ttl("nr:presence:agent-alpha")
        assert ttl == -1

    @pytest.mark.asyncio
    async def test_heartbeat_backfills_session_id(self, redis):
        from app.presence import register, heartbeat_presence
        await register(redis, "agent-alpha", "goal", "host")
        await heartbeat_presence(redis, "agent-alpha", session_id="sess-123")
        data = await redis.hgetall("nr:presence:agent-alpha")
        assert data["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_heartbeat_nonexistent_agent(self, redis):
        from app.presence import heartbeat_presence
        result = await heartbeat_presence(redis, "ghost")
        assert result["refreshed"] is False


class TestDeregister:
    @pytest.mark.asyncio
    async def test_deregister_removes_presence(self, redis):
        from app.presence import register, deregister
        await register(redis, "agent-alpha", "goal", "host")
        result = await deregister(redis, "agent-alpha")
        assert result["removed"] is True
        assert not await redis.exists("nr:presence:agent-alpha")
        score = await redis.zscore("nr:presence:__index", "agent-alpha")
        assert score is None

    @pytest.mark.asyncio
    async def test_deregister_nonexistent(self, redis):
        from app.presence import deregister
        result = await deregister(redis, "ghost")
        assert result["removed"] is False


class TestWhoIsOnline:
    @pytest.mark.asyncio
    async def test_lists_online_agents(self, redis):
        from app.presence import register, who_is_online
        await register(redis, "agent-alpha", "goal A", "host-A")
        await register(redis, "agent-beta", "goal B", "host-B")
        result = await who_is_online(redis)
        assert len(result) == 2
        ids = {a["agent_id"] for a in result}
        assert ids == {"agent-alpha", "agent-beta"}

    @pytest.mark.asyncio
    async def test_cleans_orphaned_index(self, redis):
        from app.presence import register, who_is_online
        await register(redis, "agent-alpha", "goal", "host")
        # Manually delete hash to simulate orphaned index entry
        await redis.delete("nr:presence:agent-alpha")
        result = await who_is_online(redis)
        assert len(result) == 0
        # Orphaned index entry should be cleaned up
        score = await redis.zscore("nr:presence:__index", "agent-alpha")
        assert score is None

    @pytest.mark.asyncio
    async def test_status_computed_from_heartbeat(self, redis):
        from app.presence import register, who_is_online
        await register(redis, "agent-alpha", "goal", "host")
        result = await who_is_online(redis)
        assert len(result) == 1
        # Just registered — should be active
        assert result[0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_idle_status_for_old_heartbeat(self, redis):
        from app.presence import register, who_is_online
        await register(redis, "agent-alpha", "goal", "host")
        # Manually set heartbeat to 20 minutes ago
        old_ts = str(time.time() - 1200)
        await redis.hset("nr:presence:agent-alpha", "last_heartbeat", old_ts)
        result = await who_is_online(redis)
        assert len(result) == 1
        assert result[0]["status"] == "idle"

    @pytest.mark.asyncio
    async def test_exclude_idle(self, redis):
        from app.presence import register, who_is_online
        await register(redis, "agent-alpha", "goal", "host")
        old_ts = str(time.time() - 1200)
        await redis.hset("nr:presence:agent-alpha", "last_heartbeat", old_ts)
        result = await who_is_online(redis, include_idle=False)
        assert len(result) == 0
