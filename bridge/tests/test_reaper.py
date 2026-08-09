"""Tests for the crashed-session reaper (app/reaper.py).

The reaper's whole job is to make a failure signal exist: a session whose agent
died stays "active" forever, is never distilled and never evaluated, so it drops
out of OWM scoring entirely instead of counting as the failure it was. These
tests pin the two halves of that — the Redis state transition (delegated to
SessionManager.abandon_session, invariants and all) and the scoring effects
(session_end carrying outcome="partial", plus the eval trigger).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.reaper import reap_pass, reaper_loop
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


def _stock(mock_redis, sessions: dict[str, dict], candidates: list[str] | None = None):
    """Wire mock_redis so the cutoff scan yields *candidates* and HGETALL
    answers per-session from *sessions* (missing id -> {}, i.e. dangling)."""
    mock_redis.zrangebyscore = AsyncMock(
        return_value=list(sessions) if candidates is None else candidates
    )

    async def _hgetall(key):
        return sessions.get(key.removeprefix("nb:session:"), {})

    mock_redis.hgetall = AsyncMock(side_effect=_hgetall)
    return mock_redis


@pytest.fixture
def no_side_effects():
    """Stub the two post-abandon effects so a pass makes no network calls.

    Patched on app.mcp_server, which is where reaper.after_abandon resolves
    them at call time — the same stubbing seam test_fire_and_forget_eval uses.
    """
    with (
        patch("app.mcp_server._replay_emit", new_callable=AsyncMock) as emit,
        patch("app.mcp_server._trigger_eval", new_callable=AsyncMock) as trig,
        patch("app.session._replay_emit", new_callable=AsyncMock),
    ):
        yield emit, trig


class TestReapsCrashedSessions:
    @pytest.mark.asyncio
    async def test_idle_active_session_is_abandoned(
        self, mock_redis, settings, no_side_effects
    ):
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 1, "reaped": 1, "skipped": 0}
        # Status flip goes through SessionManager.abandon_session's pipeline,
        # never a direct hset from the reaper.
        hset_calls = [c.kwargs for c in mock_redis._pipeline.hset.call_args_list]
        assert any(
            c.get("mapping", {}).get("status") == "abandoned" for c in hset_calls
        )

    @pytest.mark.asyncio
    async def test_sets_the_ttl_on_every_session_key(
        self, mock_redis, settings, no_side_effects
    ):
        """No TTL is the reason these sessions accumulate forever."""
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})

        await reap_pass(mock_redis, settings)

        expired = [c.args[0] for c in mock_redis._pipeline.expire.call_args_list]
        assert set(expired) == set(
            SessionManager(mock_redis, settings)._all_session_keys("sess-dead")
        )
        assert all(
            c.args[1] == settings.SESSION_TTL_DAYS * 86400
            for c in mock_redis._pipeline.expire.call_args_list
        )

    @pytest.mark.asyncio
    async def test_clears_the_owners_active_pointer(
        self, mock_redis, settings, no_side_effects
    ):
        """Reuse of abandon_session is what buys this. A reaper that flipped
        status directly would leave nb:active:<agent> naming a dead session —
        the dangling-pointer bug documented at session.py:143-163."""
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})
        mock_redis.get = AsyncMock(return_value="sess-dead")

        await reap_pass(mock_redis, settings)

        deleted = [c.args[0] for c in mock_redis._pipeline.delete.call_args_list]
        assert "nb:active:ghost" in deleted

    @pytest.mark.asyncio
    async def test_reaps_every_candidate_not_just_the_first(
        self, mock_redis, settings, no_side_effects
    ):
        _stock(mock_redis, {
            "s1": {"status": "active", "agent_id": "a"},
            "s2": {"status": "active", "agent_id": "b"},
            "s3": {"status": "active", "agent_id": "c"},
        })

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 3, "reaped": 3, "skipped": 0}


class TestScopeOfTheScan:
    @pytest.mark.asyncio
    async def test_only_scans_below_the_idle_cutoff(
        self, mock_redis, settings, no_side_effects
    ):
        """A recently active session is excluded by the query itself — the index
        is scored by last activity, so a live long-running session keeps scoring
        above the cutoff and is never a candidate."""
        _stock(mock_redis, {}, candidates=[])

        await reap_pass(mock_redis, settings)

        args = mock_redis.zrangebyscore.await_args.args
        assert args[0] == SessionManager.INDEX_KEY
        assert args[1] == "-inf"
        expected = (
            datetime.now(timezone.utc).timestamp() - settings.REAPER_IDLE_HOURS * 3600
        )
        assert abs(args[2] - expected) < 5  # seconds of slack for test runtime

    @pytest.mark.asyncio
    async def test_each_pass_is_bounded_by_the_per_pass_cap(
        self, mock_redis, settings, no_side_effects
    ):
        """The first pass after enabling on an old deployment faces months of
        backlog — unbounded, it would fire an eval POST per session in one
        burst. The cap is passed to zrangebyscore itself (start/num), so Redis
        returns the longest-idle sessions first and the hourly loop drains the
        backlog oldest-first with nothing missed, only deferred."""
        _stock(mock_redis, {}, candidates=[])

        await reap_pass(mock_redis, settings)

        kwargs = mock_redis.zrangebyscore.await_args.kwargs
        assert kwargs["start"] == 0
        assert kwargs["num"] == settings.REAPER_MAX_PER_PASS
        assert settings.REAPER_MAX_PER_PASS == 500

    @pytest.mark.asyncio
    async def test_no_candidates_is_a_clean_no_op(
        self, mock_redis, settings, no_side_effects
    ):
        _stock(mock_redis, {}, candidates=[])

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 0, "reaped": 0, "skipped": 0}
        mock_redis.hgetall.assert_not_awaited()

    @pytest.mark.parametrize("status", ["paused", "completed", "abandoned"])
    @pytest.mark.asyncio
    async def test_non_active_statuses_are_left_alone(
        self, mock_redis, settings, no_side_effects, status
    ):
        """Each already carries a decision somebody made and a TTL policy of its
        own; only "active" is the crashed state with nothing ahead of it."""
        _stock(mock_redis, {"sess-x": {"status": status, "agent_id": "a"}})

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 1, "reaped": 0, "skipped": 1}
        mock_redis._pipeline.hset.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_is_a_total_no_op(self, mock_redis, settings):
        settings.REAPER_ENABLED = False
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 0, "reaped": 0, "skipped": 0}
        mock_redis.zrangebyscore.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dangling_index_entry_is_self_healed(
        self, mock_redis, settings, no_side_effects
    ):
        """Metadata expired but the index entry survived. This sweep is the only
        thing that visits the oldest end of the index on a schedule."""
        _stock(mock_redis, {}, candidates=["ghost-id"])

        summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 1, "reaped": 0, "skipped": 1}
        mock_redis.zrem.assert_awaited_once_with(SessionManager.INDEX_KEY, "ghost-id")

    @pytest.mark.asyncio
    async def test_one_bad_session_does_not_abort_the_pass(
        self, mock_redis, settings, no_side_effects
    ):
        """Per-session isolation: a racing session must not strand every
        candidate behind it."""
        _stock(mock_redis, {
            "s1": {"status": "active", "agent_id": "a"},
            "s2": {"status": "active", "agent_id": "b"},
        })
        real_abandon = SessionManager.abandon_session

        async def _flaky(self, session_id=None, agent_id=None):
            if session_id == "s1":
                raise ValueError("Session s1 not found")
            return await real_abandon(self, session_id=session_id, agent_id=agent_id)

        with patch.object(SessionManager, "abandon_session", _flaky):
            summary = await reap_pass(mock_redis, settings)

        assert summary == {"scanned": 2, "reaped": 1, "skipped": 1}


class TestOutcomeSignal:
    """The point of the whole module: a reaped session must reach scoring."""

    @pytest.mark.asyncio
    async def test_session_end_carries_partial_and_the_reaped_flag(
        self, mock_redis, settings, no_side_effects
    ):
        emit, _ = no_side_effects
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})

        await reap_pass(mock_redis, settings)

        session_end = [c for c in emit.await_args_list if c.args[0] == "session_end"]
        assert len(session_end) == 1
        call = session_end[0]
        assert call.args[1] == "sess-dead"
        assert call.args[2] == "ghost"
        assert call.args[3]["outcome"] == "abandoned"
        assert call.args[3]["reaped"] is True
        # outcome="partial" is what OWM reads as the failure signal.
        assert call.kwargs["outcome"] == "partial"

    @pytest.mark.asyncio
    async def test_fires_the_eval_trigger(self, mock_redis, settings, no_side_effects):
        from app import mcp_server

        _, trig = no_side_effects
        _stock(mock_redis, {"sess-dead": {"status": "active", "agent_id": "ghost"}})

        await reap_pass(mock_redis, settings)
        await asyncio.gather(*mcp_server._background_tasks, return_exceptions=True)

        trig.assert_awaited_once()
        assert trig.await_args.args[1] == "sess-dead"

    @pytest.mark.asyncio
    async def test_human_abandon_payload_stays_reaped_free(self):
        """The reaped flag marks the reaper's work; an explicit
        ctx_abandon_session must keep the payload it already ships."""
        from app import mcp_server

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._trigger_eval", new_callable=AsyncMock),
            patch("app.mcp_server._replay_emit", new_callable=AsyncMock) as emit,
        ):
            mgr = AsyncMock()
            mgr.abandon_session = AsyncMock(
                return_value={"status": "abandoned", "session_id": "s1"}
            )
            mock_get.return_value = mgr
            await mcp_server.ctx_abandon_session()
            await asyncio.gather(
                *mcp_server._background_tasks, return_exceptions=True
            )

        assert emit.await_args.args[3] == {"outcome": "abandoned", "distilled": False}
        assert emit.await_args.kwargs["outcome"] == "partial"


class TestLoopWiring:
    @pytest.mark.asyncio
    async def test_loop_stops_on_stop_event(self):
        stop = asyncio.Event()
        stop.set()
        with patch("app.reaper.get_redis", new_callable=AsyncMock) as get_r:
            await reaper_loop(interval_seconds=0, stop_event=stop)
        get_r.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_loop_swallows_a_failed_pass(self):
        """A Redis blip must not kill the loop for the process lifetime."""
        stop = asyncio.Event()
        calls = 0

        async def _boom(*_a, **_k):
            nonlocal calls
            calls += 1
            stop.set()
            raise RuntimeError("redis is down")

        with (
            patch("app.reaper.get_redis", new_callable=AsyncMock),
            patch("app.reaper.reap_pass", new=_boom),
        ):
            await reaper_loop(interval_seconds=0, stop_event=stop)

        assert calls == 1  # returned normally rather than propagating

    @pytest.mark.asyncio
    async def test_lifespan_starts_and_cancels_the_reaper(self):
        import app.mcp_server as mod

        started = asyncio.Event()

        async def _fake_loop(*_a, **_k):
            started.set()
            await asyncio.sleep(3600)

        with (
            patch("app.distill_worker.distill_worker_loop", new=AsyncMock()),
            patch("app.distill_worker.close_distiller", new=AsyncMock()),
            patch("app.reaper.reaper_loop", new=_fake_loop),
        ):
            async with mod._lifespan(None):
                await started.wait()
        assert started.is_set()
