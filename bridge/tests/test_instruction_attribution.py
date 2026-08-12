"""Living Instructions round 2 — the measurement contract, Bridge half.

ctx_start_session reads the five X-Firekeep-* attribution headers
(case-insensitively, the x-agent-id get_http_headers pattern), persists them
on the session hash (the briefing_id precedent), and rides them on the
session_start replay payload alongside briefing_id — which is the payload
compute_session_eval reads on the Cortex side.

Contract: docs/superpowers/specs/2026-08-11-living-instructions-design.md,
"Round 2 — the measurement contract". The absence path is as load-bearing as
the presence path: every client before 0.1.41 sends none of these headers,
and those sessions must read as unattributed — absent fields, no errors —
rather than fail or fabricate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.session import SessionManager

HEADERS = {
    "x-firekeep-runtime": "claude",
    "x-firekeep-client": "0.1.41",
    "x-firekeep-instr-rendered": "aaa111bbb222",
    "x-firekeep-instr-expected": "aaa111bbb222",
    "x-firekeep-instr-gateway": "ccc333ddd444",
}

FIELDS = ("runtime", "client_version", "instr_rendered",
          "instr_expected", "instr_gateway")


class TestStartSessionPersistsAttribution:
    @pytest.mark.asyncio
    async def test_fields_stored_on_the_session_hash(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session(
            "goal", agent_id="alice", runtime="claude", client_version="0.1.41",
            instr_rendered="r" * 12, instr_expected="e" * 12,
            instr_gateway="g" * 12,
        )
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["runtime"] == "claude"
        assert mapping["client_version"] == "0.1.41"
        assert mapping["instr_rendered"] == "r" * 12
        assert mapping["instr_expected"] == "e" * 12
        assert mapping["instr_gateway"] == "g" * 12

    @pytest.mark.asyncio
    async def test_absent_fields_default_empty(self, mock_redis):
        """The briefing_id precedent: a header that never arrived stores ""
        on the hash — no error, no None-as-string artifact."""
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        for field in FIELDS:
            assert mapping[field] == "", field


def _mgr() -> AsyncMock:
    mgr = AsyncMock()
    mgr.start_session = AsyncMock(
        return_value={"session_id": "abc", "created_at": "now"}
    )
    return mgr


class TestCtxStartSessionAttribution:
    @pytest.mark.asyncio
    async def test_headers_forwarded_and_on_the_replay_payload(self):
        from app.mcp_server import ctx_start_session

        emit = AsyncMock()
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value=dict(HEADERS)),
            patch("app.mcp_server._replay_emit", emit),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            await ctx_start_session("goal", briefing_id="bf_1")

        kwargs = mgr.start_session.call_args.kwargs
        assert kwargs["runtime"] == "claude"
        assert kwargs["client_version"] == "0.1.41"
        assert kwargs["instr_rendered"] == "aaa111bbb222"
        assert kwargs["instr_expected"] == "aaa111bbb222"
        assert kwargs["instr_gateway"] == "ccc333ddd444"

        args = emit.call_args.args
        assert args[0] == "session_start"
        assert args[1] == "abc"
        payload = args[3]
        # briefing_id rides the payload — the eval's briefing_delivered
        # receipt reads it from here and nowhere else.
        assert payload["briefing_id"] == "bf_1"
        assert payload["goal"] == "goal"
        assert payload["tags"] == []
        assert payload["runtime"] == "claude"
        assert payload["client_version"] == "0.1.41"
        assert payload["instr_rendered"] == "aaa111bbb222"
        assert payload["instr_expected"] == "aaa111bbb222"
        assert payload["instr_gateway"] == "ccc333ddd444"

    @pytest.mark.asyncio
    async def test_header_names_match_case_insensitively(self):
        from app.mcp_server import ctx_start_session

        mixed = {"X-Firekeep-Runtime": "codex", "X-FIREKEEP-CLIENT": "0.1.41"}
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value=mixed),
            patch("app.mcp_server._replay_emit", AsyncMock()),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            await ctx_start_session("goal")

        kwargs = mgr.start_session.call_args.kwargs
        assert kwargs["runtime"] == "codex"
        assert kwargs["client_version"] == "0.1.41"
        assert kwargs["instr_rendered"] is None
        assert kwargs["instr_expected"] is None
        assert kwargs["instr_gateway"] is None

    @pytest.mark.asyncio
    async def test_no_headers_is_unattributed_not_an_error(self):
        """A pre-0.1.41 client sends nothing. The session must read as
        unattributed: None fields to the manager, and NO attribution keys on
        the payload at all — nothing downstream may mistake '' for a hash."""
        from app.mcp_server import ctx_start_session

        emit = AsyncMock()
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._replay_emit", emit),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            result = await ctx_start_session("goal")

        assert result["session_id"] == "abc"
        kwargs = mgr.start_session.call_args.kwargs
        for field in FIELDS:
            assert kwargs[field] is None, field
        payload = emit.call_args.args[3]
        for field in FIELDS:
            assert field not in payload, field
        assert payload["briefing_id"] == ""

    @pytest.mark.asyncio
    async def test_empty_header_values_read_as_absent(self):
        """An empty header value is not attribution; it must not overwrite
        "absent" with a stored empty-string-that-looks-deliberate."""
        from app.mcp_server import ctx_start_session

        emit = AsyncMock()
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch(
                "app.mcp_server.get_http_headers",
                return_value={"x-firekeep-runtime": "", "x-firekeep-client": "0.1.41"},
            ),
            patch("app.mcp_server._replay_emit", emit),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            await ctx_start_session("goal")

        kwargs = mgr.start_session.call_args.kwargs
        assert kwargs["runtime"] is None
        assert kwargs["client_version"] == "0.1.41"
        payload = emit.call_args.args[3]
        assert "runtime" not in payload
        assert payload["client_version"] == "0.1.41"
