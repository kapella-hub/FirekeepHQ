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
        # "dispatched", not "scheduled": the call is detached, so this response
        # cannot know the eval computed. It claimed "scheduled" throughout the
        # 12-day window in which every trigger was 403-ing.
        assert result["eval_triggered"] == "dispatched"

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


class TestEvalTriggerContract:
    """The trigger call must be identifiable and its failure must be loud.

    Every session completion 403'd for 12 days and the only trace was a
    WARNING line in a container log, while the tool response said the eval was
    "scheduled". Two separate lies: the severity, and the word.
    """

    @pytest.mark.asyncio
    async def test_marks_the_trigger_as_session_complete(self):
        """`trigger` was hardcoded "manual" server-side, so a Bridge-initiated
        eval was indistinguishable from a human one — and "all 19 evals say
        manual" was the signal that surfaced the outage in the first place."""
        from app import mcp_server

        captured = {}

        class _Resp:
            status_code = 200

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, params=None):
                captured["params"] = params
                return _Resp()

        import httpx

        with patch.object(httpx, "AsyncClient", lambda **k: _Client()):
            ok = await mcp_server._trigger_eval("http://cortex", "s1")

        assert ok is True
        assert captured["params"] == {"trigger": "session_complete"}

    @pytest.mark.asyncio
    async def test_a_4xx_is_logged_at_error_not_warning(self, caplog):
        """A 4xx here is a CONFIGURATION fault that repeats on every session
        and silently starves OWM, quality trends and the pattern A/B join. It
        sat at WARNING and nobody saw it."""
        import logging

        from app import mcp_server

        class _Resp:
            status_code = 403

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, params=None):
                return _Resp()

        import httpx

        with caplog.at_level(logging.ERROR):
            with patch.object(httpx, "AsyncClient", lambda **k: _Client()):
                ok = await mcp_server._trigger_eval("http://cortex", "s1")

        assert ok is False
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
