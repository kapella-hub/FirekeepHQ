"""Tests for FirekeepScope MCP tools (SP2 Phase A Task 6)."""

from unittest.mock import patch

import pytest

from app.mcp_server import scope_start, scope_ask, scope_post, scope_check, scope_complete


@pytest.mark.asyncio
class TestScopeStart:
    async def test_creates_mcp_origin_session(self, redis):
        with patch("app.mcp_server.get_redis", return_value=redis):
            result = await scope_start(goal="Design auth", agent_id="agent-kiro")
        assert result["origin"] == "mcp"
        assert result["scope_id"].startswith("sc_")


@pytest.mark.asyncio
class TestScopePost:
    async def test_posts_async_screen_and_returns_immediately(self, redis):
        with patch("app.mcp_server.get_redis", return_value=redis):
            result = await scope_post(
                screen={"kind": "questions", "title": "t", "questions": []},
                agent_id="agent-kiro", goal="g",
            )
        assert result["status"] == "posted"
        assert "scope_id" in result and "screen_id" in result


@pytest.mark.asyncio
class TestScopeCheck:
    async def test_reports_answered_and_pending(self, redis):
        with patch("app.mcp_server.get_redis", return_value=redis):
            posted = await scope_post(screen={"kind": "questions", "title": "t", "questions": []}, agent_id="a", goal="g")
            from app.scope import post_answer
            await post_answer(redis, posted["scope_id"], posted["screen_id"], answers={"q1": {"choice": "a"}}, source="dashboard")
            result = await scope_check(posted["scope_id"])
        assert posted["screen_id"] in result["answered"]
        assert result["pending"] == []


@pytest.mark.asyncio
class TestScopeAsk:
    async def test_returns_answered_once_resolved_during_poll(self, redis, monkeypatch):
        # scope_ask's poll loop checks-then-sleeps; monkeypatching sleep to
        # answer the screen simulates "a human answers between polls" without
        # a real 24s wait, and correctly exercises the "answered" branch on
        # the loop's *next* iteration (the check that already ran this
        # iteration still saw "pending").
        from app.scope import create_session, get_screens, post_answer

        with patch("app.mcp_server.get_redis", return_value=redis):
            session = await create_session(redis, agent_id="a", goal="g", origin="mcp")
            scope_id = session["scope_id"]

            async def fake_sleep(_seconds):
                screens = await get_screens(redis, scope_id)
                pending = [s for s in screens if s["status"] == "pending"]
                if pending:
                    await post_answer(redis, scope_id, pending[0]["screen_id"], answers={"q1": {"choice": "a"}}, source="dashboard")

            monkeypatch.setattr("app.mcp_server.asyncio.sleep", fake_sleep)

            result = await scope_ask(screen={"kind": "questions", "title": "t", "questions": []}, scope_id=scope_id)

        assert result["status"] == "answered"
        assert result["answers"]["q1"]["choice"] == "a"

    async def test_returns_pending_after_poll_budget_exhausted(self, redis, monkeypatch):
        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr("app.mcp_server.asyncio.sleep", fake_sleep)
        with patch("app.mcp_server.get_redis", return_value=redis):
            result = await scope_ask(screen={"kind": "questions", "title": "t", "questions": []}, agent_id="a", goal="g")
        assert result["status"] == "pending"
        assert "scope_id" in result and "screen_id" in result

    async def test_rejects_invalid_agent_id_on_auto_create(self, redis):
        # agent_id is only consumed on the auto-create-session path (no
        # scope_id given). A malformed value here (space + "!" fall outside
        # _VALID_NAME's [a-zA-Z0-9._-] charset) must be rejected before a
        # session is ever persisted, matching scope_start's validation.
        with patch("app.mcp_server.get_redis", return_value=redis):
            result = await scope_ask(
                screen={"kind": "questions", "title": "t", "questions": []},
                agent_id="bad agent!",
            )
        assert result["status"] == "unavailable"
        assert "error" in result

    async def test_explicit_scope_id_path_ignores_malformed_agent_id(self, redis, monkeypatch):
        # When scope_id is provided, agent_id is unused (the session already
        # exists) — validation must not gate this branch.
        from app.scope import create_session, get_screens, post_answer

        with patch("app.mcp_server.get_redis", return_value=redis):
            session = await create_session(redis, agent_id="a", goal="g", origin="mcp")
            scope_id = session["scope_id"]

            async def fake_sleep(_seconds):
                screens = await get_screens(redis, scope_id)
                pending = [s for s in screens if s["status"] == "pending"]
                if pending:
                    await post_answer(redis, scope_id, pending[0]["screen_id"], answers={"q1": {"choice": "a"}}, source="dashboard")

            monkeypatch.setattr("app.mcp_server.asyncio.sleep", fake_sleep)

            result = await scope_ask(
                screen={"kind": "questions", "title": "t", "questions": []},
                scope_id=scope_id,
                agent_id="bad agent!",
            )

        assert result["status"] == "answered"


@pytest.mark.asyncio
class TestScopeComplete:
    async def test_marks_session_completed(self, redis):
        with patch("app.mcp_server.get_redis", return_value=redis):
            session = await scope_start(goal="g", agent_id="a")
            result = await scope_complete(session["scope_id"])
        assert result["status"] == "completed"

    async def test_unknown_session_returns_error(self, redis):
        with patch("app.mcp_server.get_redis", return_value=redis):
            result = await scope_complete("sc_missing")
        assert "error" in result
