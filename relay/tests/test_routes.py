"""Tests for REST endpoints (presence)."""

import pytest
import pytest_asyncio
import fakeredis.aioredis


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestPresenceRoutes:
    @pytest.mark.asyncio
    async def test_get_presence_empty(self, redis):
        from app.routes import handle_get_presence
        result = await handle_get_presence(redis, include_idle=True)
        assert result["agents"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_presence_with_agents(self, redis):
        from app.presence import register
        from app.routes import handle_get_presence
        await register(redis, "agent-alpha", "goal", "host")
        result = await handle_get_presence(redis, include_idle=True)
        assert result["count"] == 1
        assert result["agents"][0]["agent_id"] == "agent-alpha"

    @pytest.mark.asyncio
    async def test_get_single_presence(self, redis):
        from app.presence import register
        from app.routes import handle_get_single_presence
        await register(redis, "agent-alpha", "goal", "host")
        result = await handle_get_single_presence(redis, "agent-alpha")
        assert result["agent_id"] == "agent-alpha"

    @pytest.mark.asyncio
    async def test_get_single_presence_not_found(self, redis):
        from app.routes import handle_get_single_presence
        result = await handle_get_single_presence(redis, "ghost")
        assert result is None
