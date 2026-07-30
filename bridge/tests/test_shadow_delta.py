"""Tests for SessionManager.get_shadow_epoch — the reader precompact's
ctx_update(category="scratch", key="shadow_epoch") writes through.
"""

import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.session import SessionManager


class TestShadowEpoch:
    @pytest.mark.asyncio
    async def test_shadow_epoch_is_empty_when_never_bumped(self, mock_redis):
        mock_redis.hget = AsyncMock(return_value=None)
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == ""

    @pytest.mark.asyncio
    async def test_shadow_epoch_reads_the_scratch_field_precompact_wrote(self, mock_redis):
        """precompact bumps the epoch through the ordinary ctx_update scratch path —
        no new MCP tool, and no new Redis key."""
        mock_redis.hget = AsyncMock(return_value="1700000000000")
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == "1700000000000"
        mock_redis.hget.assert_awaited_once_with("nb:session:sess-1:scratch", "shadow_epoch")

    @pytest.mark.asyncio
    async def test_epoch_is_NONE_not_empty_when_the_read_fails(self, mock_redis):
        """AMENDED 2026-07-30 (C2, Critical). An earlier version of this task returned
        "" on a read error and claimed that "mismatches every cursor". That was FALSE:
        "" is a real, matchable state carried by every cursor minted before the first
        compaction, so an errored read matched a STALE post-compaction cursor and served
        a delta to an agent that had just lost its context — a guard that failed OPEN.
        None is unmatchable by construction, so a failure cannot pass for a state."""
        mock_redis.hget = AsyncMock(side_effect=RuntimeError("redis down"))
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") is None
