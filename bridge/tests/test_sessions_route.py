"""Tests for Bridge /sessions REST endpoint handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.hset = AsyncMock()
    r.hget = AsyncMock(return_value=None)
    r.hgetall = AsyncMock(return_value={})
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    r.expire = AsyncMock()
    r.lpush = AsyncMock()
    r.lrange = AsyncMock(return_value=[])
    r.llen = AsyncMock(return_value=0)
    r.ltrim = AsyncMock()
    r.zadd = AsyncMock()
    r.zrangebyscore = AsyncMock(return_value=[])
    r.zrevrangebyscore = AsyncMock(return_value=[])
    r.zcard = AsyncMock(return_value=0)
    r.zrem = AsyncMock()
    r.zrange = AsyncMock(return_value=[])
    r.eval = AsyncMock(return_value="")
    r.persist = AsyncMock()
    return r


@pytest.fixture
def manager(mock_redis, settings):
    return SessionManager(mock_redis, settings)


class TestSessionsEndpointData:
    """Test that session listing returns the data shape the briefing resumables consumer expects."""

    @pytest.mark.asyncio
    async def test_list_sessions_returns_expected_fields(self, manager, mock_redis):
        """list_sessions returns session_id, goal, status, agent_id."""
        # First call returns IDs, second call returns empty (end of pagination)
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[["sess-001"], []])
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "test goal",
            "status": "active",
            "agent_id": "agent-alpha",
            "created_at": "2026-03-22T00:00:00+00:00",
            "updated_at": "2026-03-22T00:00:00+00:00",
            "tags": "[]",
        })

        sessions = await manager.list_sessions(status="active", limit=50)
        assert len(sessions) == 1
        sess = sessions[0]
        assert sess["session_id"] == "sess-001"
        assert sess["goal"] == "test goal"
        assert sess["status"] == "active"
        assert sess["agent_id"] == "agent-alpha"

    @pytest.mark.asyncio
    async def test_list_sessions_filters_by_status(self, manager, mock_redis):
        """Only sessions matching the status filter are returned."""
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[["s1", "s2"], []])

        async def mock_hgetall(key):
            if "s1" in key:
                return {
                    "goal": "g1", "status": "active", "agent_id": "a",
                    "created_at": "", "updated_at": "", "tags": "[]",
                }
            return {
                "goal": "g2", "status": "paused", "agent_id": "b",
                "created_at": "", "updated_at": "", "tags": "[]",
            }

        mock_redis.hgetall = mock_hgetall

        sessions = await manager.list_sessions(status="active", limit=50)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_session_data_includes_files(self, manager, mock_redis):
        """get_session_data returns files dict for the briefing resumables enrichment."""
        mock_redis.hgetall = AsyncMock(side_effect=[
            # First call: session metadata
            {
                "goal": "test", "status": "active", "agent_id": "a",
                "created_at": "", "updated_at": "", "tags": "[]",
                "outcome": "", "distillation": "",
            },
            # Second call: files hash
            {"src/main.py": json.dumps({"summary": "edited", "last_action": "now"})},
            # Third call: scratch hash
            {},
        ])
        mock_redis.get = AsyncMock(side_effect=[
            "plan text",  # plan
            None,         # proactive
        ])
        mock_redis.lrange = AsyncMock(return_value=[])

        data = await manager.get_session_data("sess-001")
        assert data is not None
        assert "files" in data
        assert "src/main.py" in data["files"]

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, manager, mock_redis):
        """Returns empty list when no sessions exist."""
        mock_redis.zrevrangebyscore = AsyncMock(return_value=[])
        sessions = await manager.list_sessions()
        assert sessions == []
