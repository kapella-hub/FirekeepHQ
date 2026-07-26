"""Tests for header-based agent identity on Bridge ctx_* tools (SP0 D3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestDefaultAgentId:
    def test_explicit_param_wins(self):
        from app.mcp_server import _default_agent_id

        with patch(
            "app.mcp_server.get_http_headers", return_value={"x-agent-id": "alice"}
        ):
            assert _default_agent_id("bob") == "bob"

    def test_header_wins_over_sentinel_default(self):
        from app.mcp_server import _default_agent_id

        with patch(
            "app.mcp_server.get_http_headers", return_value={"x-agent-id": "alice"}
        ):
            assert _default_agent_id("default") == "alice"

    def test_fallback_to_default_without_header(self):
        from app.mcp_server import _default_agent_id

        with patch("app.mcp_server.get_http_headers", return_value={}):
            assert _default_agent_id("default") == "default"


class TestImportFallbackFailsLoudly:
    def test_import_fallback_returns_empty_and_logs_error(self, caplog):
        """If fastmcp.server.dependencies is unavailable, the module must fall
        back to a {} get_http_headers AND emit an ERROR log — never silently
        disable header identity (SP0 fail-loud requirement)."""
        import importlib
        import logging
        import sys
        import types

        import app.mcp_server as mod

        # Force the ImportError path deterministically: replace the real
        # fastmcp with a bare (non-package) module exposing only FastMCP, so
        # `from fastmcp.server.dependencies import ...` raises ImportError.
        saved = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if name == "fastmcp" or name.startswith("fastmcp.")
        }

        class _StubFastMCP:
            def __init__(self, name, **kwargs):
                self.name = name

            def tool(self, *args, **kwargs):
                def decorator(fn):
                    return fn

                return decorator

            def custom_route(self, *args, **kwargs):
                def decorator(fn):
                    return fn

                return decorator

            def run(self, *args, **kwargs):
                return None

        stub = types.ModuleType("fastmcp")
        stub.FastMCP = _StubFastMCP
        for name in saved:
            sys.modules.pop(name, None)
        sys.modules["fastmcp"] = stub

        try:
            with caplog.at_level(logging.ERROR, logger="app.mcp_server"):
                mod = importlib.reload(mod)
            # Fallback behavior: {} means "no headers", identity degrades to "default"
            assert mod.get_http_headers() == {}
            assert mod._default_agent_id("default") == "default"
            # Loudness: the degradation must be logged at ERROR level
            error_records = [
                r
                for r in caplog.records
                if r.levelno == logging.ERROR
                and "header-based identity DISABLED" in r.getMessage()
            ]
            assert error_records, (
                "expected an ERROR log announcing header identity is disabled"
            )
        finally:
            sys.modules.pop("fastmcp", None)
            sys.modules.update(saved)
            importlib.reload(mod)


class TestToolsUseHeaderIdentity:
    @pytest.mark.asyncio
    async def test_ctx_start_session_defaults_agent_from_header(self):
        from app.mcp_server import ctx_start_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice"},
            ),
        ):
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(
                return_value={"session_id": "abc", "created_at": "now"}
            )
            mock_get.return_value = mgr
            await ctx_start_session("test goal")

        assert mgr.start_session.call_args.kwargs["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_ctx_start_session_param_overrides_header(self):
        from app.mcp_server import ctx_start_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice"},
            ),
        ):
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(
                return_value={"session_id": "abc", "created_at": "now"}
            )
            mock_get.return_value = mgr
            await ctx_start_session("test goal", agent_id="bob")

        assert mgr.start_session.call_args.kwargs["agent_id"] == "bob"

    @pytest.mark.asyncio
    async def test_ctx_update_defaults_agent_from_header(self):
        from app.mcp_server import ctx_update

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice"},
            ),
        ):
            mgr = AsyncMock()
            mgr.update = AsyncMock(return_value={"status": "ok", "component_count": 1})
            mgr.get_active_session_id = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            await ctx_update("plan", "- [ ] Step 1")

        assert mgr.update.call_args.kwargs["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_ctx_complete_session_defaults_agent_from_header(self):
        from app.mcp_server import ctx_complete_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice"},
            ),
            patch("app.mcp_server._trigger_eval", new=AsyncMock(return_value=True)),
            patch(
                "app.mcp_server._trigger_skill_evaluate",
                new=AsyncMock(return_value=True),
            ),
        ):
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "s1"}
            )
            mgr.get_session_data = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            await ctx_complete_session()

        assert mgr.complete_session.call_args.kwargs["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_no_header_no_param_keeps_default(self):
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
            await ctx_start_session("test goal")

        assert mgr.start_session.call_args.kwargs["agent_id"] == "default"
