"""Tests for header-based identity on Cortex MCP memory tools (SP0 D3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test"),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests."""
    import app.mcp_server as mod

    mod._client = None
    yield
    if mod._client and not mod._client.is_closed:
        mod._client = None


class TestHeaderIdentity:
    def test_header_identity_reads_connection_headers(self):
        from app.mcp_server import _header_identity

        with patch(
            "app.mcp_server.get_http_headers",
            return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
        ):
            agent_id, session_id = _header_identity()
        assert agent_id == "alice"
        assert session_id == "sess-9"

    def test_header_identity_empty_outside_request_context(self):
        from app.mcp_server import _header_identity

        with patch("app.mcp_server.get_http_headers", return_value={}):
            assert _header_identity() == (None, None)

    def test_resolve_identity_param_overrides_header(self):
        from app.mcp_server import _resolve_identity

        with patch(
            "app.mcp_server.get_http_headers",
            return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
        ):
            session_id, agent_id = _resolve_identity("explicit-sess", "bob")
        assert session_id == "explicit-sess"
        assert agent_id == "bob"

    def test_resolve_identity_falls_back_to_header(self):
        from app.mcp_server import _resolve_identity

        with patch(
            "app.mcp_server.get_http_headers",
            return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
        ):
            session_id, agent_id = _resolve_identity("unknown", "unknown")
        assert session_id == "sess-9"
        assert agent_id == "alice"

    def test_resolve_identity_unknown_when_no_param_no_header(self):
        from app.mcp_server import _resolve_identity

        with patch("app.mcp_server.get_http_headers", return_value={}):
            assert _resolve_identity("unknown", "unknown") == ("unknown", "unknown")


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

        # Force the ImportError path deterministically: replace fastmcp with a
        # bare (non-package) module exposing only FastMCP, so
        # `from fastmcp.server.dependencies import ...` raises ImportError.
        saved = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if name == "fastmcp" or name.startswith("fastmcp.")
        }

        class _StubFastMCP:
            def __init__(self, name):
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
            # Fallback behavior: {} means "no headers", identity degrades to unknown
            assert mod.get_http_headers() == {}
            assert mod._header_identity() == (None, None)
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


class TestMemoryLearnHeaderIdentity:
    @pytest.mark.asyncio
    async def test_memory_learn_forwards_header_identity_to_rest(self):
        """With no explicit params, X-Agent-Id/X-Session-Id from the MCP
        connection are forwarded to POST /memory/learn."""
        mock_resp = _mock_response({"status": "stored", "vector_id": "v1"})
        with (
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
            ) as mock_post,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
            ),
        ):
            from app.mcp_server import memory_learn

            await memory_learn(action="did x", outcome="worked")

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["X-Agent-Id"] == "alice"
        assert headers["X-Session-Id"] == "sess-9"

    @pytest.mark.asyncio
    async def test_memory_learn_explicit_param_beats_header(self):
        mock_resp = _mock_response({"status": "stored", "vector_id": "v1"})
        with (
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
            ) as mock_post,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
            ),
        ):
            from app.mcp_server import memory_learn

            await memory_learn(
                action="did x", outcome="worked", agent_id="bob", session_id="my-sess"
            )

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["X-Agent-Id"] == "bob"
        assert headers["X-Session-Id"] == "my-sess"

    @pytest.mark.asyncio
    async def test_memory_recall_forwards_header_identity(self):
        mock_resp = _mock_response({"context_block": "ctx"})
        with (
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
            ) as mock_post,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-agent-id": "alice", "x-session-id": "sess-9"},
            ),
        ):
            from app.mcp_server import memory_recall

            await memory_recall(task="find auth bug notes")

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["X-Agent-Id"] == "alice"
        assert headers["X-Session-Id"] == "sess-9"
