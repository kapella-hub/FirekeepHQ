"""Tests for direct message system."""

import json
import pytest
import pytest_asyncio
import fakeredis.aioredis


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestSendDM:
    @pytest.mark.asyncio
    async def test_send_creates_message(self, redis):
        from app.dm import send_dm
        msg = await send_dm(redis, "agent-alpha", "Hello!", "dashboard")
        assert msg["from"] == "dashboard"
        assert msg["to"] == "agent-alpha"
        assert msg["content"] == "Hello!"
        assert msg["read"] is False
        assert msg["id"].startswith("dm-")

    @pytest.mark.asyncio
    async def test_send_stores_in_redis(self, redis):
        from app.dm import send_dm
        await send_dm(redis, "agent-alpha", "Hello!", "dashboard")
        raw = await redis.lrange("nr:dm:agent-alpha", 0, -1)
        assert len(raw) == 1
        msg = json.loads(raw[0])
        assert msg["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_sets_ttl(self, redis):
        from app.dm import send_dm, DM_TTL_SECONDS
        await send_dm(redis, "agent-alpha", "Hello!", "dashboard")
        ttl = await redis.ttl("nr:dm:agent-alpha")
        assert 0 < ttl <= DM_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_send_multiple_messages(self, redis):
        from app.dm import send_dm
        await send_dm(redis, "agent-alpha", "First", "dashboard")
        await send_dm(redis, "agent-alpha", "Second", "agent-beta")
        raw = await redis.lrange("nr:dm:agent-alpha", 0, -1)
        assert len(raw) == 2
        # Newest first (lpush)
        assert json.loads(raw[0])["content"] == "Second"
        assert json.loads(raw[1])["content"] == "First"


class TestGetDMs:
    @pytest.mark.asyncio
    async def test_get_empty_inbox(self, redis):
        from app.dm import get_dms
        messages = await get_dms(redis, "ghost")
        assert messages == []

    @pytest.mark.asyncio
    async def test_get_returns_newest_first(self, redis):
        from app.dm import send_dm, get_dms
        await send_dm(redis, "agent-alpha", "First", "dashboard")
        await send_dm(redis, "agent-alpha", "Second", "dashboard")
        messages = await get_dms(redis, "agent-alpha")
        assert len(messages) == 2
        assert messages[0]["content"] == "Second"
        assert messages[1]["content"] == "First"

    @pytest.mark.asyncio
    async def test_get_unread_only(self, redis):
        from app.dm import send_dm, get_dms, mark_read
        await send_dm(redis, "agent-alpha", "Old", "dashboard")
        await mark_read(redis, "agent-alpha")
        await send_dm(redis, "agent-alpha", "New", "dashboard")
        messages = await get_dms(redis, "agent-alpha", unread_only=True)
        assert len(messages) == 1
        assert messages[0]["content"] == "New"

    @pytest.mark.asyncio
    async def test_get_respects_limit(self, redis):
        from app.dm import send_dm, get_dms
        for i in range(10):
            await send_dm(redis, "agent-alpha", f"msg-{i}", "dashboard")
        messages = await get_dms(redis, "agent-alpha", limit=3)
        assert len(messages) == 3

    @pytest.mark.asyncio
    async def test_get_isolates_agents(self, redis):
        from app.dm import send_dm, get_dms
        await send_dm(redis, "agent-alpha", "For alpha", "dashboard")
        await send_dm(redis, "agent-beta", "For beta", "dashboard")
        alpha_msgs = await get_dms(redis, "agent-alpha")
        beta_msgs = await get_dms(redis, "agent-beta")
        assert len(alpha_msgs) == 1
        assert len(beta_msgs) == 1
        assert alpha_msgs[0]["content"] == "For alpha"
        assert beta_msgs[0]["content"] == "For beta"


class TestMarkRead:
    @pytest.mark.asyncio
    async def test_mark_read_updates_messages(self, redis):
        from app.dm import send_dm, get_dms, mark_read
        await send_dm(redis, "agent-alpha", "Hello", "dashboard")
        await send_dm(redis, "agent-alpha", "World", "dashboard")
        count = await mark_read(redis, "agent-alpha")
        assert count == 2
        messages = await get_dms(redis, "agent-alpha")
        assert all(m["read"] is True for m in messages)

    @pytest.mark.asyncio
    async def test_mark_read_empty_inbox(self, redis):
        from app.dm import mark_read
        count = await mark_read(redis, "ghost")
        assert count == 0

    @pytest.mark.asyncio
    async def test_mark_read_idempotent(self, redis):
        from app.dm import send_dm, mark_read
        await send_dm(redis, "agent-alpha", "Hello", "dashboard")
        await mark_read(redis, "agent-alpha")
        count = await mark_read(redis, "agent-alpha")
        assert count == 0  # already read


class TestDMRouteHandlers:
    @pytest.mark.asyncio
    async def test_handle_get_dm_empty(self, redis):
        from app.routes import handle_get_dm
        result = await handle_get_dm(redis, "ghost")
        assert result["messages"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_handle_get_dm_with_messages(self, redis):
        from app.dm import send_dm
        from app.routes import handle_get_dm
        await send_dm(redis, "agent-alpha", "Hello", "dashboard")
        result = await handle_get_dm(redis, "agent-alpha")
        assert result["count"] == 1
        assert result["agent_id"] == "agent-alpha"

    @pytest.mark.asyncio
    async def test_handle_post_dm(self, redis):
        from app.routes import handle_post_dm
        result = await handle_post_dm(redis, "agent-alpha", "Test message", "dashboard")
        assert result["status"] == "sent"
        assert result["message"]["to"] == "agent-alpha"

    @pytest.mark.asyncio
    async def test_handle_mark_dm_read(self, redis):
        from app.dm import send_dm
        from app.routes import handle_mark_dm_read
        await send_dm(redis, "agent-alpha", "Hello", "dashboard")
        result = await handle_mark_dm_read(redis, "agent-alpha")
        assert result["marked_read"] == 1
