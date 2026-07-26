"""Tests that ctx_complete_session does not block on the eval trigger (SP0 D5)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class TestFireAndForgetEval:
    @pytest.mark.asyncio
    async def test_complete_returns_fast_when_eval_hangs(self):
        """Even if the Cortex eval endpoint hangs, completion must return
        immediately (defect #21: ~50s inline await broke MCP client timeouts)."""
        from app import mcp_server

        async def hanging_eval(*args, **kwargs):
            await asyncio.sleep(30)
            return True

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._trigger_eval", new=hanging_eval),
            patch(
                "app.mcp_server._trigger_skill_evaluate",
                new=AsyncMock(return_value=True),
            ),
            # SP1a: repo root is now on sys.path (auth import fix in conftest.py),
            # so the real `replay` package resolves and _replay_emit would try a
            # genuine (unreachable outside Docker) Redis connection, stalling past
            # this test's 2s wait_for. Not what this test is about — stub it out.
            patch("app.mcp_server._replay_emit", new=AsyncMock()),
        ):
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "s1"}
            )
            mock_get.return_value = mgr

            result = await asyncio.wait_for(
                mcp_server.ctx_complete_session(), timeout=2.0
            )

        assert result["status"] == "completed"
        assert result["eval_triggered"] == "scheduled"

        # Clean up the detached hanging task
        for task in list(mcp_server._background_tasks):
            task.cancel()
        await asyncio.gather(*mcp_server._background_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_spawn_background_keeps_and_releases_reference(self):
        from app import mcp_server

        async def quick():
            return 42

        task = mcp_server._spawn_background(quick())
        assert task in mcp_server._background_tasks
        await task
        await asyncio.sleep(0)  # let the done-callback run
        assert task not in mcp_server._background_tasks

    @pytest.mark.asyncio
    async def test_abandon_returns_fast_when_eval_hangs(self):
        from app import mcp_server

        async def hanging_eval(*args, **kwargs):
            await asyncio.sleep(30)
            return True

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._trigger_eval", new=hanging_eval),
            # SP1a: see note in test_complete_returns_fast_when_eval_hangs above.
            patch("app.mcp_server._replay_emit", new=AsyncMock()),
        ):
            mgr = AsyncMock()
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "s1"}
            )
            mock_get.return_value = mgr

            result = await asyncio.wait_for(
                mcp_server.ctx_abandon_session(), timeout=2.0
            )

        assert result["status"] == "abandoned"
        for task in list(mcp_server._background_tasks):
            task.cancel()
        await asyncio.gather(*mcp_server._background_tasks, return_exceptions=True)
