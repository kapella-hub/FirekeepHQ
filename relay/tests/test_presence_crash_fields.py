"""SP1b D1: crash-detection depends on presence.register storing started_at + session_id.

register() must store a heartbeat-INDEPENDENT registration timestamp (started_at)
and the session_id; heartbeat_presence() must NOT overwrite started_at (only bump
last_heartbeat) but MAY backfill session_id within one turn. The Cortex briefing
crash-detect rule (WS-2, D1) reads both fields, so they are pinned here.
"""
from __future__ import annotations

import pytest

from app.presence import register, heartbeat_presence, PRESENCE_PREFIX


class TestPresenceCrashDetectFields:
    @pytest.mark.asyncio
    async def test_register_stores_started_at_and_session_id(self, redis):
        await register(redis, "alice", "fix collector", "host1", session_id="sess-1")
        data = await redis.hgetall(f"{PRESENCE_PREFIX}alice")
        assert data["session_id"] == "sess-1"
        assert "started_at" in data
        float(data["started_at"])  # must be a parseable epoch

    @pytest.mark.asyncio
    async def test_register_defaults_session_id_empty(self, redis):
        await register(redis, "bob", "goal", "host2")
        data = await redis.hgetall(f"{PRESENCE_PREFIX}bob")
        assert data["session_id"] == ""

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_started_at_and_backfills_session_id(self, redis):
        await register(redis, "carol", "goal", "host3")
        original = (await redis.hgetall(f"{PRESENCE_PREFIX}carol"))["started_at"]
        await heartbeat_presence(redis, "carol", session_id="sess-9")
        data = await redis.hgetall(f"{PRESENCE_PREFIX}carol")
        assert data["started_at"] == original   # registration time is stable
        assert data["session_id"] == "sess-9"   # backfilled within one turn
