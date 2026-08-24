"""Tests for MCP tool wiring — verify tools call through to session manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestMCPTools:
    @pytest.mark.asyncio
    async def test_ctx_start_session(self):
        from app.mcp_server import ctx_start_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(return_value={"session_id": "abc", "created_at": "now"})
            mock_get.return_value = mgr
            result = await ctx_start_session("test goal")
            assert result["session_id"] == "abc"
            mgr.start_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_ctx_update(self):
        from app.mcp_server import ctx_update
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.update = AsyncMock(return_value={"status": "ok", "component_count": 1})
            mock_get.return_value = mgr
            result = await ctx_update("plan", "- [ ] Step 1")
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_ctx_update_catches_value_error(self):
        from app.mcp_server import ctx_update
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.update = AsyncMock(side_effect=ValueError("No active session"))
            mock_get.return_value = mgr
            result = await ctx_update("plan", "test")
            assert "error" in result
            assert "No active session" in result["error"]

    @pytest.mark.asyncio
    async def test_ctx_get_shadow_no_session(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_get_shadow()
            assert "error" in result

    @pytest.mark.asyncio
    async def test_ctx_complete_catches_value_error(self):
        from app.mcp_server import ctx_complete_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(side_effect=ValueError("No active session"))
            mock_get.return_value = mgr
            result = await ctx_complete_session()
            assert "error" in result
            assert "No active session" in result["error"]

    @pytest.mark.asyncio
    async def test_ctx_abandon(self):
        from app.mcp_server import ctx_abandon_session
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.after_abandon", new=AsyncMock()),
        ):
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value="s1")
            mgr.get_session_data = AsyncMock(return_value={"owner_member": ""})
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "s1"}
            )
            mock_get.return_value = mgr
            result = await ctx_abandon_session()
            assert result["status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_ctx_abandon_catches_value_error(self):
        from app.mcp_server import ctx_abandon_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_abandon_session()
            assert "error" in result
            mgr.abandon_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ctx_resume_catches_value_error(self):
        from app.mcp_server import ctx_resume_session
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.resume_session = AsyncMock(side_effect=ValueError("Session not found"))
            mock_get.return_value = mgr
            result = await ctx_resume_session("bad-id")
            assert "error" in result

    @pytest.mark.asyncio
    async def test_ctx_list_sessions(self):
        from app.mcp_server import ctx_list_sessions
        with patch("app.mcp_server._get_manager") as mock_get:
            mgr = AsyncMock()
            mgr.list_sessions = AsyncMock(return_value=[])
            mock_get.return_value = mgr
            result = await ctx_list_sessions()
            assert result["sessions"] == []
