"""SP1b D2: ctx_start_session + start_session accept & store briefing_id.

Links briefing_id -> session_id so the Cortex briefing tip-shown A/B recording
closes its feedback loop. Mirrors test_project_field.py — the bridge mock_redis
fixture (AsyncMock) does not round-trip get_session_data, so the proof is the
hset mapping the session hash is written from.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.session import SessionManager


class TestStartSessionBriefingId:
    @pytest.mark.asyncio
    async def test_start_session_stores_briefing_id(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice", briefing_id="bf_abc123")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["briefing_id"] == "bf_abc123"

    @pytest.mark.asyncio
    async def test_start_session_defaults_briefing_id_empty(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["briefing_id"] == ""


class TestCtxStartSessionBriefingId:
    @pytest.mark.asyncio
    async def test_ctx_start_session_forwards_briefing_id(self):
        from app.mcp_server import ctx_start_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
        ):
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(
                return_value={"session_id": "abc", "created_at": "now"}
            )
            mock_get.return_value = mgr
            await ctx_start_session("goal", briefing_id="bf_abc123")

        assert mgr.start_session.call_args.kwargs["briefing_id"] == "bf_abc123"
