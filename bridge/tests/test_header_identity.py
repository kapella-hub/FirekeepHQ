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


class TestHeaderSessionId:
    """Unit contract of _header_session_id (mirrors cortex's _header_identity)."""

    def test_reads_header(self):
        from app.mcp_server import _header_session_id

        with patch(
            "app.mcp_server.get_http_headers",
            return_value={"x-session-id": "sess-1"},
        ):
            assert _header_session_id() == "sess-1"

    def test_header_name_is_case_insensitive(self):
        from app.mcp_server import _header_session_id

        with patch(
            "app.mcp_server.get_http_headers",
            return_value={"X-Session-Id": "sess-1"},
        ):
            assert _header_session_id() == "sess-1"

    def test_none_when_absent(self):
        from app.mcp_server import _header_session_id

        with patch("app.mcp_server.get_http_headers", return_value={}):
            assert _header_session_id() is None

    def test_none_when_empty(self):
        from app.mcp_server import _header_session_id

        with patch(
            "app.mcp_server.get_http_headers", return_value={"x-session-id": ""}
        ):
            assert _header_session_id() is None


class TestToolsUseHeaderSession:
    """The server half of the cross-terminal clobber fix (2026-08-12).

    Two terminals on one machine share agent_id, and Bridge keys the active
    pointer per-agent (nb:active:{agent_id}) — so a session-resolving tool
    called with no session_id used to resolve to whichever terminal wrote the
    pointer last. Terminal B's no-arg ctx_complete_session completed terminal
    A's in-flight session. These tests pin cortex's documented precedence on
    every session-resolving tool: explicit param > connection X-Session-Id
    header > active pointer.

    The pointer fallback (no header + no param) is deliberately KEPT — it is
    the backward-compat path for every client that predates the header.
    """

    def _completion_patches(self, headers: dict):
        return (
            patch("app.mcp_server.get_http_headers", return_value=headers),
            patch("app.mcp_server._replay_emit", new=AsyncMock()),
            patch("app.mcp_server._trigger_eval", new=AsyncMock(return_value=True)),
            patch(
                "app.mcp_server._trigger_skill_evaluate",
                new=AsyncMock(return_value=True),
            ),
        )

    # ------------------------------------------------------------------
    # ctx_complete_session
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_complete_header_beats_pointer(self):
        from app.mcp_server import ctx_complete_session

        p1, p2, p3, p4 = self._completion_patches({"x-session-id": "sess-hdr"})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "sess-hdr"}
            )
            mock_get.return_value = mgr
            await ctx_complete_session()

        assert mgr.complete_session.call_args.kwargs["session_id"] == "sess-hdr"

    @pytest.mark.asyncio
    async def test_complete_explicit_param_beats_header(self):
        from app.mcp_server import ctx_complete_session

        p1, p2, p3, p4 = self._completion_patches({"x-session-id": "sess-hdr"})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "sess-param"}
            )
            mock_get.return_value = mgr
            await ctx_complete_session(session_id="sess-param")

        assert mgr.complete_session.call_args.kwargs["session_id"] == "sess-param"

    @pytest.mark.asyncio
    async def test_complete_no_header_no_param_still_resolves_via_pointer(self):
        """Backward-compat pin: header-less clients keep today's behavior —
        session_id=None reaches SessionManager, whose pointer fallback runs."""
        from app.mcp_server import ctx_complete_session

        p1, p2, p3, p4 = self._completion_patches({})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "sess-ptr"}
            )
            mock_get.return_value = mgr
            await ctx_complete_session()

        assert mgr.complete_session.call_args.kwargs["session_id"] is None

    # ------------------------------------------------------------------
    # ctx_abandon_session
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_abandon_header_beats_pointer(self):
        from app.mcp_server import ctx_abandon_session

        p1, p2, p3, p4 = self._completion_patches({"x-session-id": "sess-hdr"})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"owner_member": ""})
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "sess-hdr"}
            )
            mock_get.return_value = mgr
            await ctx_abandon_session()

        assert mgr.abandon_session.call_args.kwargs["session_id"] == "sess-hdr"

    @pytest.mark.asyncio
    async def test_abandon_explicit_param_beats_header(self):
        from app.mcp_server import ctx_abandon_session

        p1, p2, p3, p4 = self._completion_patches({"x-session-id": "sess-hdr"})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"owner_member": ""})
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "sess-param"}
            )
            mock_get.return_value = mgr
            await ctx_abandon_session(session_id="sess-param")

        assert mgr.abandon_session.call_args.kwargs["session_id"] == "sess-param"

    @pytest.mark.asyncio
    async def test_abandon_no_header_no_param_still_resolves_via_pointer(self):
        """Public fallback behavior is unchanged; only resolution moved up —
        the tool now resolves the active pointer itself and passes that exact
        frozen SID to abandon_session, rather than passing None through."""
        from app.mcp_server import ctx_abandon_session

        p1, p2, p3, p4 = self._completion_patches({})
        with patch("app.mcp_server._get_manager") as mock_get, p1, p2, p3, p4:
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value="sess-ptr")
            mgr.get_session_data = AsyncMock(return_value={"owner_member": ""})
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "sess-ptr"}
            )
            mock_get.return_value = mgr
            await ctx_abandon_session()

        assert mgr.abandon_session.call_args.kwargs["session_id"] == "sess-ptr"

    # ------------------------------------------------------------------
    # ctx_update — public signature unchanged; the header threads through a
    # new session_id kwarg on SessionManager.update
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_update_header_scopes_the_write(self):
        from app.mcp_server import ctx_update

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-session-id": "sess-hdr"},
            ),
            patch("app.mcp_server._replay_emit", new=AsyncMock()),
        ):
            mgr = AsyncMock()
            mgr.update = AsyncMock(return_value={"status": "ok", "component_count": 1})
            mock_get.return_value = mgr
            # category "file" sidesteps the snapshot (decision/plan) and
            # proactive-recall (plan/progress) side channels.
            await ctx_update("file", "added helper", key="app/x.py")

        assert mgr.update.call_args.kwargs["session_id"] == "sess-hdr"
        # With the header present, nothing on the path may consult the shared
        # pointer — that lookup is the clobber.
        mgr.get_active_session_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_no_header_keeps_pointer_resolution(self):
        from app.mcp_server import ctx_update

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._replay_emit", new=AsyncMock()),
        ):
            mgr = AsyncMock()
            mgr.update = AsyncMock(return_value={"status": "ok", "component_count": 1})
            mgr.get_active_session_id = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            await ctx_update("file", "added helper", key="app/x.py")

        assert mgr.update.call_args.kwargs["session_id"] is None

    # ------------------------------------------------------------------
    # ctx_get_shadow
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_shadow_header_beats_pointer(self):
        from app.mcp_server import ctx_get_shadow

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-session-id": "sess-hdr"},
            ),
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(
                return_value={"goal": "g", "status": "active"}
            )
            # epoch=None takes the plain full-restore path — no residency logic.
            mgr.get_shadow_epoch = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_get_shadow()

        mgr.get_session_data.assert_awaited_once_with("sess-hdr")
        mgr.get_active_session_id.assert_not_awaited()
        assert result["session_id"] == "sess-hdr"

    @pytest.mark.asyncio
    async def test_get_shadow_explicit_param_beats_header(self):
        from app.mcp_server import ctx_get_shadow

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-session-id": "sess-hdr"},
            ),
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(
                return_value={"goal": "g", "status": "active"}
            )
            mgr.get_shadow_epoch = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_get_shadow(session_id="sess-param")

        mgr.get_session_data.assert_awaited_once_with("sess-param")
        assert result["session_id"] == "sess-param"

    @pytest.mark.asyncio
    async def test_get_shadow_no_header_no_param_still_resolves_via_pointer(self):
        from app.mcp_server import ctx_get_shadow

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
        ):
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value="sess-ptr")
            mgr.get_session_data = AsyncMock(
                return_value={"goal": "g", "status": "active"}
            )
            mgr.get_shadow_epoch = AsyncMock(return_value=None)
            mock_get.return_value = mgr
            result = await ctx_get_shadow()

        mgr.get_session_data.assert_awaited_once_with("sess-ptr")
        assert result["session_id"] == "sess-ptr"
