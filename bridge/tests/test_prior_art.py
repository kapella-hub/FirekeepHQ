"""Tests for prior art at the moment of intent (app/prior_art.py).

The contract these pin: a declared goal is answered with what the team already
built and who is mid-flight on similar work — and NOTHING about that answer may
cost the caller the session it actually asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.prior_art import (
    assemble_prior_art,
    fetch_in_flight,
    fetch_team_memories,
    render_prior_art,
)


def _recall_client(payload: dict) -> AsyncMock:
    """An httpx.AsyncClient stand-in returning *payload* from POST."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = payload

    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class TestFetchTeamMemories:
    @pytest.mark.asyncio
    async def test_floor_filters_on_raw_score_not_normalized_score(self):
        """0.55 is a floor on `metadata.raw_score`. `score` is 1.0 by
        construction for the best entry in any result set, so a floor on it
        would filter nothing at all (cortex/app/main.py's own measurement)."""
        client = _recall_client({
            "sources": [
                {"content": "shipped Keep Backup end-to-end", "score": 1.0,
                 "metadata": {"raw_score": 0.63}},
                {"content": "unrelated but pinned to the top", "score": 1.0,
                 "metadata": {"raw_score": 0.41}},
                {"content": "right at the floor", "score": 0.0,
                 "metadata": {"raw_score": 0.55}},
            ],
        })

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await fetch_team_memories(
                "harden the backup retention policy",
                api_url="http://cortex:8100",
                min_score=0.55,
            )

        assert [m["summary"] for m in result] == [
            "shipped Keep Backup end-to-end",
            "right at the floor",
        ]
        assert result[0]["raw_score"] == 0.63

    @pytest.mark.asyncio
    async def test_body_carries_the_prior_art_trigger(self):
        """The compliance measurement slices deliberate recall from pushed
        recall on this exact string."""
        client = _recall_client({"sources": []})

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            await fetch_team_memories(
                "harden the backup retention policy",
                api_url="http://cortex:8100",
            )

        body = client.post.call_args.kwargs["json"]
        assert body["trigger"] == "prior-art"
        assert body["format"] == "raw"
        assert "namespace" not in body

    @pytest.mark.asyncio
    async def test_sends_the_internal_key(self):
        client = _recall_client({"sources": []})

        # A fixture value shaped so no secret scanner pattern-matches it — the
        # original "internal-key-123" tripped gitleaks' generic-api-key rule
        # and broke the CI secrets gate for three runs.
        fake_key = "not a real credential"
        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            await fetch_team_memories(
                "harden the backup retention policy",
                api_url="http://cortex:8100",
                api_key=fake_key,
            )

        assert client.post.call_args.kwargs["headers"]["X-API-Key"] == fake_key

    @pytest.mark.asyncio
    async def test_skips_sources_without_a_raw_score(self):
        client = _recall_client({
            "sources": [{"content": "bare graph node", "score": 1.0, "metadata": {}}],
        })

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await fetch_team_memories(
                "harden the backup retention policy", api_url="http://cortex:8100"
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_everything_when_recall_is_degraded(self):
        client = _recall_client({
            "sources": [{"content": "graph-only guess", "score": 1.0,
                         "metadata": {"raw_score": 0.9}}],
            "degraded": True,
        })

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await fetch_team_memories(
                "harden the backup retention policy", api_url="http://cortex:8100"
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_summary_is_collapsed_and_truncated(self):
        client = _recall_client({
            "sources": [{"content": "line one\n\n   line two " + "x" * 400,
                         "metadata": {"raw_score": 0.9, "timestamp": "2026-08-01T12:30:00Z"}}],
        })

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await fetch_team_memories(
                "harden the backup retention policy", api_url="http://cortex:8100"
            )

        summary = result[0]["summary"]
        assert "\n" not in summary
        assert summary.startswith("line one line two ")
        assert len(summary) == 203  # 200 chars + the visible ellipsis
        assert result[0]["when"] == "2026-08-01"

    @pytest.mark.asyncio
    async def test_returns_empty_when_cortex_is_unreachable(self):
        client = AsyncMock()
        client.post.side_effect = Exception("Connection refused")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await fetch_team_memories(
                "harden the backup retention policy", api_url="http://cortex:8100"
            )

        assert result == []


class TestFetchInFlight:
    @pytest.mark.asyncio
    async def test_excludes_the_caller_and_caps_at_three(self):
        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[
            {"session_id": "s0", "agent_id": "me", "goal": "the goal I just declared",
             "created_at": _iso(seconds=1)},
            {"session_id": "s1", "agent_id": "agent-x", "goal": "harden backup retention",
             "created_at": _iso(hours=2)},
            {"session_id": "s2", "agent_id": "agent-y", "goal": "second",
             "created_at": _iso(hours=3)},
            {"session_id": "s3", "agent_id": "me", "goal": "my other terminal",
             "created_at": _iso(hours=4)},
            {"session_id": "s4", "agent_id": "agent-z", "goal": "third",
             "created_at": _iso(hours=5)},
            {"session_id": "s5", "agent_id": "agent-w", "goal": "fourth, over the cap",
             "created_at": _iso(hours=6)},
        ])

        result = await fetch_in_flight(mgr, agent_id="me", limit=3)

        assert [s["agent_id"] for s in result] == ["agent-x", "agent-y", "agent-z"]
        assert result[0]["goal"] == "harden backup retention"
        assert mgr.list_sessions.call_args.kwargs["status"] == "active"

    @pytest.mark.asyncio
    async def test_skips_sessions_with_no_owner(self):
        """A legacy session naming no agent cannot be attributed, and 'somebody
        is on this' is the entire value of the line."""
        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[
            {"session_id": "s1", "agent_id": "", "goal": "orphan", "created_at": _iso(hours=1)},
        ])

        assert await fetch_in_flight(mgr, agent_id="me") == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_redis_fails(self):
        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(side_effect=Exception("redis down"))

        assert await fetch_in_flight(mgr, agent_id="me") == []


class TestRenderPriorArt:
    def test_block_format_is_pinned(self):
        block = render_prior_art({
            "memories": [
                {"summary": "Shipped Keep Backup end-to-end...", "raw_score": 0.63,
                 "when": "2026-07-02"},
            ],
            "in_flight": [
                {"agent_id": "agent-x", "goal": "harden backup retention",
                 "started_at": _iso(hours=2)},
            ],
        })

        assert block == (
            "[prior art] the team may have been here before — recall before building:\n"
            "- Shipped Keep Backup end-to-end... (raw 0.63)\n"
            'in flight right now: agent-x — "harden backup retention" (2h ago)'
        )

    def test_several_in_flight_share_one_line(self):
        block = render_prior_art({
            "memories": [],
            "in_flight": [
                {"agent_id": "agent-x", "goal": "one", "started_at": _iso(minutes=5)},
                {"agent_id": "agent-y", "goal": "two", "started_at": _iso(days=3)},
            ],
        })

        assert block.splitlines()[-1] == (
            'in flight right now: agent-x — "one" (5m ago); agent-y — "two" (3d ago)'
        )

    def test_unreadable_timestamp_drops_only_the_parenthetical(self):
        block = render_prior_art({
            "memories": [],
            "in_flight": [{"agent_id": "agent-x", "goal": "one", "started_at": ""}],
        })

        assert block.splitlines()[-1] == 'in flight right now: agent-x — "one"'

    def test_empty_renders_nothing(self):
        assert render_prior_art({}) == ""
        assert render_prior_art({"memories": [], "in_flight": []}) == ""


class TestAssemblePriorArt:
    @pytest.mark.asyncio
    async def test_nothing_found_is_an_absent_key_not_an_empty_one(self):
        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        client = _recall_client({"sources": []})

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await assemble_prior_art(
                "harden the backup retention policy",
                mgr=mgr, agent_id="me", api_url="http://cortex:8100",
            )

        assert result == {}

    @pytest.mark.asyncio
    async def test_a_dead_cortex_still_yields_the_in_flight_leg(self):
        """The two legs fail independently — one deadline, but not one fate."""
        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[
            {"session_id": "s1", "agent_id": "agent-x", "goal": "harden backup retention",
             "created_at": _iso(hours=2)},
        ])
        client = AsyncMock()
        client.post.side_effect = Exception("Connection refused")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await assemble_prior_art(
                "harden the backup retention policy",
                mgr=mgr, agent_id="me", api_url="http://cortex:8100",
            )

        assert result["memories"] == []
        assert [s["agent_id"] for s in result["in_flight"]] == ["agent-x"]

    @pytest.mark.asyncio
    async def test_a_hanging_cortex_is_bounded_by_the_deadline(self):
        import asyncio

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(30)

        mgr = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])

        with patch("app.prior_art.fetch_team_memories", side_effect=_hang):
            result = await asyncio.wait_for(
                assemble_prior_art(
                    "harden the backup retention policy",
                    mgr=mgr, agent_id="me", api_url="http://cortex:8100",
                    timeout=0.05,
                ),
                timeout=5.0,
            )

        assert result == {}


class TestCtxStartSessionPriorArt:
    """The MCP surface: what an agent that declares a goal actually receives."""

    @staticmethod
    def _manager(sessions: list[dict]) -> AsyncMock:
        mgr = AsyncMock()
        mgr.start_session = AsyncMock(
            return_value={"session_id": "abc", "created_at": "now"}
        )
        mgr.list_sessions = AsyncMock(return_value=sessions)
        return mgr

    @pytest.mark.asyncio
    async def test_response_carries_prior_art_and_its_rendered_block(self, monkeypatch):
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
        mgr = self._manager([
            {"session_id": "s1", "agent_id": "agent-x", "goal": "harden backup retention",
             "created_at": _iso(hours=2)},
        ])
        client = _recall_client({
            "sources": [{"content": "Shipped Keep Backup end-to-end, restore verified",
                         "score": 1.0, "metadata": {"raw_score": 0.63}}],
        })

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assert result["session_id"] == "abc"
        assert result["prior_art"]["memories"][0]["raw_score"] == 0.63
        assert result["prior_art"]["in_flight"][0]["agent_id"] == "agent-x"
        assert result["prior_art_text"] == (
            "[prior art] the team may have been here before — recall before building:\n"
            "- Shipped Keep Backup end-to-end, restore verified (raw 0.63)\n"
            'in flight right now: agent-x — "harden backup retention" (2h ago)'
        )

    @pytest.mark.asyncio
    async def test_nothing_to_say_adds_no_block(self, monkeypatch):
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
        mgr = self._manager([])
        client = _recall_client({"sources": [
            {"content": "below the floor", "score": 1.0, "metadata": {"raw_score": 0.2}},
        ]})

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assert result["session_id"] == "abc"
        assert "prior_art" not in result
        assert "prior_art_text" not in result

    @pytest.mark.asyncio
    async def test_cortex_down_still_creates_the_session(self, monkeypatch):
        """The whole point of the ordering: prior art is assembled AFTER the
        session exists, so no failure in it can cost the caller a session."""
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
        mgr = self._manager([])
        client = AsyncMock()
        client.post.side_effect = Exception("Connection refused")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assert result == {"session_id": "abc", "created_at": "now"}
        mgr.start_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_assembly_blowing_up_never_reaches_the_caller(self, monkeypatch):
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
        mgr = self._manager([])

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.mcp_server.assemble_prior_art",
                   side_effect=RuntimeError("assembly exploded")):
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assert result == {"session_id": "abc", "created_at": "now"}

    @pytest.mark.asyncio
    async def test_the_flag_is_the_gate(self, monkeypatch):
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", False)
        mgr = self._manager([])

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.mcp_server.assemble_prior_art") as assemble:
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assemble.assert_not_called()
        assert result == {"session_id": "abc", "created_at": "now"}

    @pytest.mark.asyncio
    async def test_the_caller_is_excluded_from_its_own_in_flight_list(self, monkeypatch):
        from app import mcp_server

        monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
        mgr = self._manager([
            {"session_id": "abc", "agent_id": "me", "goal": "build a backup system",
             "created_at": _iso(seconds=1)},
        ])
        client = _recall_client({"sources": []})

        with patch("app.mcp_server._get_manager", return_value=mgr), \
             patch("app.prior_art.httpx.AsyncClient", return_value=client):
            result = await mcp_server.ctx_start_session(
                "build a backup system for the Keep", agent_id="me"
            )

        assert "prior_art" not in result
