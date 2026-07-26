"""Tests for FirekeepScope session/screen/answer storage (SP2 Phase A)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.scope import (
    create_session, get_session, list_sessions, complete_session,
    abandon_stale_sessions, mirror_screen, get_screens, post_answer,
    get_events,
)


class TestSessions:
    @pytest.mark.asyncio
    async def test_create_session_sets_fields(self, redis):
        session = await create_session(
            redis, agent_id="agent-a", goal="Design auth", origin="cli",
            project="firekeep", bridge_session_id="bsess-1",
        )
        assert session["agent_id"] == "agent-a"
        assert session["goal"] == "Design auth"
        assert session["origin"] == "cli"
        assert session["status"] == "active"
        assert session["scope_id"].startswith("sc_")

    @pytest.mark.asyncio
    async def test_create_session_rejects_bad_origin(self, redis):
        with pytest.raises(ValueError):
            await create_session(redis, agent_id="a", goal="g", origin="bogus")

    @pytest.mark.asyncio
    async def test_create_session_rejects_invalid_scope_id(self, redis):
        with pytest.raises(ValueError):
            await create_session(redis, agent_id="a", goal="g", origin="cli", scope_id="sc bad id")

    @pytest.mark.asyncio
    async def test_create_session_rejects_scope_id_with_quote(self, redis):
        with pytest.raises(ValueError):
            await create_session(redis, agent_id="a", goal="g", origin="cli", scope_id='sc_"injected')

    @pytest.mark.asyncio
    async def test_create_session_upsert_is_idempotent(self, redis):
        s1 = await create_session(redis, agent_id="a", goal="g1", origin="cli", scope_id="sc_fixed")
        s2 = await create_session(redis, agent_id="a", goal="g2-ignored", origin="mcp", scope_id="sc_fixed")
        assert s2["goal"] == "g1"  # second call is a no-op read, not an overwrite
        assert s1["created_at"] == s2["created_at"]

    @pytest.mark.asyncio
    async def test_get_session_missing_returns_none(self, redis):
        assert await get_session(redis, "sc_missing") is None

    @pytest.mark.asyncio
    async def test_list_sessions_filters_by_status(self, redis):
        active = await create_session(redis, agent_id="a", goal="g1", origin="cli")
        await create_session(redis, agent_id="a", goal="g2", origin="cli", scope_id="sc_done")
        await complete_session(redis, "sc_done")
        results = await list_sessions(redis, status="active")
        ids = [s["scope_id"] for s in results]
        assert active["scope_id"] in ids
        assert "sc_done" not in ids

    @pytest.mark.asyncio
    async def test_list_sessions_reports_pending_screens(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        results = await list_sessions(redis, status="active")
        match = next(s for s in results if s["scope_id"] == session["scope_id"])
        assert match["pending_screens"] is True

    @pytest.mark.asyncio
    async def test_complete_session_sets_ttl(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        await complete_session(redis, session["scope_id"])
        updated = await get_session(redis, session["scope_id"])
        assert updated["status"] == "completed"
        ttl = await redis.ttl(f"nr:scope:session:{session['scope_id']}")
        assert 0 < ttl <= 86400 * 7

    @pytest.mark.asyncio
    async def test_complete_session_expires_screen_seq_and_answer_keys(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        scope_id = session["scope_id"]
        screen = await mirror_screen(redis, scope_id, {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        await post_answer(redis, scope_id, screen["screen_id"], answers={"q1": {"choice": "a"}}, source="local")

        await complete_session(redis, scope_id)

        seq_ttl = await redis.ttl(f"nr:scope:screen_seq:{scope_id}")
        answer_ttl = await redis.ttl(f"nr:scope:answer:{scope_id}:{screen['screen_id']}")
        assert seq_ttl > 0
        assert answer_ttl > 0

    @pytest.mark.asyncio
    async def test_complete_session_is_noop_when_not_active(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        scope_id = session["scope_id"]
        await redis.hset(f"nr:scope:session:{scope_id}", "last_activity_at", 0)
        count = await abandon_stale_sessions(redis)
        assert count == 1
        abandoned = await get_session(redis, scope_id)
        assert abandoned["status"] == "abandoned"

        result = await complete_session(redis, scope_id)
        assert result["status"] == "abandoned"  # not flipped to "completed"

    @pytest.mark.asyncio
    async def test_abandon_stale_sessions_sweeps_inactive(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        # Simulate 73h of inactivity
        await redis.hset(f"nr:scope:session:{session['scope_id']}", "last_activity_at", 0)
        count = await abandon_stale_sessions(redis)
        assert count == 1
        updated = await get_session(redis, session["scope_id"])
        assert updated["status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_abandon_stale_sessions_skips_recent(self, redis):
        await create_session(redis, agent_id="a", goal="g", origin="cli")
        count = await abandon_stale_sessions(redis)
        assert count == 0


class TestScreens:
    @pytest.mark.asyncio
    async def test_mirror_screen_mints_screen_id(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {
            "kind": "questions", "mode": "gating", "title": "Auth flow", "questions": [],
        })
        assert screen["screen_id"] == f"{session['scope_id']}-1"
        assert screen["status"] == "pending"
        assert screen["v"] == 1  # Global Constraint: Screen objects carry "v": 1

    @pytest.mark.asyncio
    async def test_mirror_screen_mints_sequential_ids(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        s1 = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "a", "questions": []})
        s2 = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "b", "questions": []})
        assert s1["screen_id"] == f"{session['scope_id']}-1"
        assert s2["screen_id"] == f"{session['scope_id']}-2"

    @pytest.mark.asyncio
    async def test_mirror_screen_upsert_with_explicit_id_is_idempotent(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen_id = f"{session['scope_id']}-9"
        payload = {"screen_id": screen_id, "kind": "questions", "mode": "gating", "title": "a", "questions": []}
        await mirror_screen(redis, session["scope_id"], payload)
        await mirror_screen(redis, session["scope_id"], payload)  # retry
        screens = await get_screens(redis, session["scope_id"])
        assert len(screens) == 1  # no duplicate order-list entry

    @pytest.mark.asyncio
    async def test_mirror_screen_rejects_invalid_screen_id(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        with pytest.raises(ValueError):
            await mirror_screen(redis, session["scope_id"], {
                "screen_id": "bad id!", "kind": "questions", "mode": "gating", "title": "t", "questions": [],
            })

    @pytest.mark.asyncio
    async def test_mirror_screen_forces_v_and_status_ignoring_caller_injection(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {
            "screen_id": f"{session['scope_id']}-injected", "kind": "questions", "mode": "gating",
            "title": "t", "questions": [], "v": 99, "status": "resolved",
        })
        assert screen["v"] == 1
        assert screen["status"] == "pending"
        stored = (await get_screens(redis, session["scope_id"]))[0]
        assert stored["v"] == 1
        assert stored["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_screens_preserves_order(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "first", "questions": []})
        await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "second", "questions": []})
        screens = await get_screens(redis, session["scope_id"])
        assert [s["title"] for s in screens] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_mirror_screen_retry_does_not_revert_resolved_screen(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        payload = {"kind": "questions", "mode": "gating", "title": "t", "questions": []}
        screen = await mirror_screen(redis, session["scope_id"], payload)
        screen_id = screen["screen_id"]
        await post_answer(redis, session["scope_id"], screen_id, answers={"q1": {"choice": "a"}}, source="local")

        retry_payload = {**payload, "screen_id": screen_id}
        result = await mirror_screen(redis, session["scope_id"], retry_payload)

        assert result["status"] == "resolved"
        assert result["answer"]["answers"]["q1"]["choice"] == "a"

        screens = await get_screens(redis, session["scope_id"])
        assert len(screens) == 1  # no duplicate order-list entry
        assert screens[0]["status"] == "resolved"
        assert screens[0]["answer"]["answers"]["q1"]["choice"] == "a"


class TestAnswers:
    @pytest.mark.asyncio
    async def test_post_answer_resolves_screen(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        result = await post_answer(
            redis, session["scope_id"], screen["screen_id"],
            answers={"q1": {"choice": "a", "choices": None, "text": None, "other_text": None, "note": None}},
            source="local",
        )
        assert result["resolved"] is True
        screens = await get_screens(redis, session["scope_id"])
        assert screens[0]["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_post_answer_second_writer_loses_race(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        first = await post_answer(redis, session["scope_id"], screen["screen_id"], answers={"q1": {"choice": "a"}}, source="local")
        second = await post_answer(redis, session["scope_id"], screen["screen_id"], answers={"q1": {"choice": "b"}}, source="dashboard")
        assert first["resolved"] is True
        assert second["resolved"] is False
        assert second["answer"]["answers"]["q1"]["choice"] == "a"  # first writer's answer stands

    @pytest.mark.asyncio
    async def test_post_answer_unknown_screen_raises(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        with pytest.raises(ValueError):
            await post_answer(redis, session["scope_id"], "sc_x-99", answers={}, source="local")

    @pytest.mark.asyncio
    async def test_post_answer_unknown_screen_never_poisons_arbiter(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        scope_id = session["scope_id"]
        screen_id = f"{scope_id}-1"

        # First call targets a screen_id that was never mirrored — must raise,
        # and must NOT leave behind a winning arbiter key for that screen_id.
        with pytest.raises(ValueError):
            await post_answer(redis, scope_id, screen_id, answers={"q1": {"choice": "ghost"}}, source="local")

        # Now mirror that exact screen_id for real and answer it — this must
        # succeed and resolve with the real answer, not the ghost payload.
        await mirror_screen(redis, scope_id, {"screen_id": screen_id, "kind": "questions", "mode": "gating", "title": "t", "questions": []})
        result = await post_answer(redis, scope_id, screen_id, answers={"q1": {"choice": "real"}}, source="local")

        assert result["resolved"] is True
        assert result["answer"]["answers"]["q1"]["choice"] == "real"

    @pytest.mark.asyncio
    async def test_post_answer_rejects_bad_source(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        with pytest.raises(ValueError):
            await post_answer(redis, session["scope_id"], screen["screen_id"], answers={}, source="carrier-pigeon")

    @pytest.mark.asyncio
    async def test_post_answer_persists_to_bridge_for_mcp_origin(self, redis):
        session = await create_session(redis, agent_id="agent-x", goal="g", origin="mcp")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        with patch("app.scope.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await post_answer(
                redis, session["scope_id"], screen["screen_id"],
                answers={"q1": {"choice": "a"}}, source="dashboard",
                bridge_url="http://bridge:8070",
            )
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://bridge:8070/sessions/agent-x/context"
            assert call_args[1]["json"]["category"] == "decision"
            # No api_key supplied — backward-compatible, no X-API-Key header.
            assert "X-API-Key" not in call_args[1]["headers"]

    @pytest.mark.asyncio
    async def test_post_answer_persists_to_bridge_with_api_key(self, redis):
        session = await create_session(redis, agent_id="agent-x", goal="g", origin="mcp")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        with patch("app.scope.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await post_answer(
                redis, session["scope_id"], screen["screen_id"],
                answers={"q1": {"choice": "a"}}, source="dashboard",
                bridge_url="http://bridge:8070", api_key="secret-key-123",
            )
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[1]["headers"]["X-API-Key"] == "secret-key-123"

    @pytest.mark.asyncio
    async def test_post_answer_skips_bridge_for_cli_origin(self, redis):
        session = await create_session(redis, agent_id="agent-x", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        with patch("app.scope.httpx.AsyncClient") as mock_client_cls:
            await post_answer(
                redis, session["scope_id"], screen["screen_id"],
                answers={"q1": {"choice": "a"}}, source="local",
                bridge_url="http://bridge:8070",
            )
            mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_answer_bridge_failure_is_non_fatal(self, redis):
        session = await create_session(redis, agent_id="agent-x", goal="g", origin="mcp")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        with patch("app.scope.httpx.AsyncClient", side_effect=RuntimeError("network down")):
            result = await post_answer(
                redis, session["scope_id"], screen["screen_id"],
                answers={"q1": {"choice": "a"}}, source="local",
                bridge_url="http://bridge:8070",
            )
        assert result["resolved"] is True  # answer still resolves despite Bridge failure


class TestEvents:
    @pytest.mark.asyncio
    async def test_get_events_returns_posted_and_answered(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        await post_answer(redis, session["scope_id"], screen["screen_id"], answers={"q1": {"choice": "a"}}, source="local")
        events = await get_events(redis, session["scope_id"])
        types = [e["type"] for e in events]
        assert types == ["screen.posted", "screen.answered"]

    @pytest.mark.asyncio
    async def test_get_events_since_cursor(self, redis):
        session = await create_session(redis, agent_id="a", goal="g", origin="cli")
        await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        first_batch = await get_events(redis, session["scope_id"])
        await mirror_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t2", "questions": []})
        second_batch = await get_events(redis, session["scope_id"], since=len(first_batch))
        assert len(second_batch) == 1
        assert second_batch[0]["type"] == "screen.posted"
