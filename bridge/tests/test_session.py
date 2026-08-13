"""Tests for session management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def manager(mock_redis, settings):
    return SessionManager(mock_redis, settings)


class TestStartSession:
    @pytest.mark.asyncio
    async def test_creates_session(self, manager, mock_redis):
        mock_redis.exists = AsyncMock(return_value=0)
        mock_redis.eval = AsyncMock(return_value="")
        result = await manager.start_session("implement feature X")
        assert "session_id" in result
        assert "created_at" in result

    @pytest.mark.asyncio
    async def test_pauses_existing_active_session(self, manager, mock_redis):
        mock_redis.exists = AsyncMock(return_value=0)
        mock_redis.eval = AsyncMock(return_value="old-session-id")
        result = await manager.start_session("new task")
        assert "session_id" in result
        eval_args = mock_redis.eval.await_args.args
        assert eval_args[1] == 2
        assert "nb:session:__placeholder__" not in eval_args
        assert eval_args[-1] == "nb:session:"

    @pytest.mark.asyncio
    async def test_session_id_collision_retries(self, manager, mock_redis):
        # First two IDs collide, third is unique
        mock_redis.exists = AsyncMock(side_effect=[1, 1, 0])
        mock_redis.eval = AsyncMock(return_value="")
        result = await manager.start_session("test")
        assert "session_id" in result
        assert mock_redis.exists.call_count == 3

    @pytest.mark.asyncio
    async def test_session_id_collision_exhausted(self, manager, mock_redis):
        mock_redis.exists = AsyncMock(return_value=1)
        with pytest.raises(RuntimeError, match="Failed to generate unique session ID"):
            await manager.start_session("test")


class TestGetActiveSession:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_active(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        result = await manager.get_active_session_id()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_session_id(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        result = await manager.get_active_session_id()
        assert result == "sess-123"


class TestUpdateComponent:
    @pytest.mark.asyncio
    async def test_update_plan(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        result = await manager.update("plan", "- [ ] Step 1\n- [ ] Step 2")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_plan_rejects_oversized(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        huge_plan = "x" * 20000
        with pytest.raises(ValueError, match="exceeds"):
            await manager.update("plan", huge_plan)

    @pytest.mark.asyncio
    async def test_update_decision_appends(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.llen = AsyncMock(return_value=5)
        result = await manager.update("decision", "chose approach A")
        assert result["status"] == "ok"
        assert result["component_count"] == 5

    @pytest.mark.asyncio
    async def test_update_file_upserts(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hlen = AsyncMock(return_value=3)
        result = await manager.update("file", "added function X", key="app/main.py")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_requires_active_session(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="No active session"):
            await manager.update("plan", "test")

    @pytest.mark.asyncio
    async def test_update_refuses_nonexistent_named_session(self, manager, mock_redis):
        """A named session that no longer EXISTS must refuse, not materialize.
        A stale X-Session-Id can outlive a reaped-then-expired session (the
        client stash lives 12h; the shim's in-memory id has no TTL), and
        before this guard the write created a ghost meta hash holding only
        updated_at — no status, no owner, no TTL — and reported ok for a
        write that was lost on arrival (external review 2026-08-12)."""
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=0)
        with pytest.raises(ValueError, match="Unknown session"):
            await manager.update("plan", "test", session_id="sess-expired")

    @pytest.mark.asyncio
    async def test_update_file_requires_key(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        with pytest.raises(ValueError, match="key"):
            await manager.update("file", "summary")

    @pytest.mark.asyncio
    async def test_update_scratch_requires_key(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        with pytest.raises(ValueError, match="key"):
            await manager.update("scratch", "value")


class TestCompleteSession:
    @pytest.mark.asyncio
    async def test_marks_completed(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hgetall = AsyncMock(return_value={"goal": "test", "status": "active", "agent_id": "default"})
        result = await manager.complete_session()
        assert result["status"] == "completed"
        mock_redis.pipeline.assert_called()

    @pytest.mark.asyncio
    async def test_requires_active_session(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="No active session"):
            await manager.complete_session()


class TestAbandonSession:
    @pytest.mark.asyncio
    async def test_marks_abandoned(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hgetall = AsyncMock(return_value={"goal": "test", "status": "active", "agent_id": "default"})
        result = await manager.abandon_session()
        assert result["status"] == "abandoned"


class TestResumeSession:
    @pytest.mark.asyncio
    async def test_resumes_paused_session(self, manager, mock_redis):
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "old task", "status": "paused", "agent_id": "default",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "tags": "[]",
        })
        mock_redis.eval = AsyncMock(return_value="")
        result = await manager.resume_session("sess-old")
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_resume_rejects_completed(self, manager, mock_redis):
        mock_redis.hgetall = AsyncMock(return_value={"status": "completed"})
        with pytest.raises(ValueError, match="Cannot resume"):
            await manager.resume_session("sess-done")


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_sessions(self, manager, mock_redis):
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[["sess-1", "sess-2"], []])
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "task", "status": "active", "agent_id": "default",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "tags": "[]",
        })
        result = await manager.list_sessions()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_iterates_until_limit_filled(self, manager, mock_redis):
        """Verify cursor-based iteration fetches more batches when filtering."""
        # First batch: 2 sessions, only 1 matches filter
        # Second batch: 1 session that matches
        # Third batch: empty (stop)
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[
            ["sess-1", "sess-2"],
            ["sess-3"],
            [],
        ])

        async def hgetall_side_effect(key):
            if "sess-1" in key:
                return {"goal": "a", "status": "active", "agent_id": "default",
                        "created_at": "t", "updated_at": "t"}
            if "sess-2" in key:
                return {"goal": "b", "status": "paused", "agent_id": "default",
                        "created_at": "t", "updated_at": "t"}
            if "sess-3" in key:
                return {"goal": "c", "status": "active", "agent_id": "default",
                        "created_at": "t", "updated_at": "t"}
            return {}

        mock_redis.hgetall = AsyncMock(side_effect=hgetall_side_effect)
        result = await manager.list_sessions(status="active", limit=2)
        assert len(result) == 2
        assert result[0]["session_id"] == "sess-1"
        assert result[1]["session_id"] == "sess-3"

    @pytest.mark.asyncio
    async def test_returns_briefing_id(self, manager, mock_redis):
        """SP1b §11: list_sessions must surface briefing_id so Cortex can
        resolve briefing_id -> session_id for tip-effectiveness reconciliation."""
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[["sess-1"], []])
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "task", "status": "paused", "agent_id": "default",
            "created_at": "t", "updated_at": "t", "briefing_id": "bf_xyz",
        })
        result = await manager.list_sessions()
        assert result[0]["briefing_id"] == "bf_xyz"

    @pytest.mark.asyncio
    async def test_briefing_id_defaults_empty_when_absent(self, manager, mock_redis):
        mock_redis.zrevrangebyscore = AsyncMock(side_effect=[["sess-1"], []])
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "task", "status": "paused", "agent_id": "default",
            "created_at": "t", "updated_at": "t",
        })
        result = await manager.list_sessions()
        assert result[0]["briefing_id"] == ""


class TestReplayEmission:
    @pytest.mark.asyncio
    async def test_start_session_emits_replay_event(self, manager, mock_redis):
        mock_redis.exists = AsyncMock(return_value=0)
        mock_redis.eval = AsyncMock(return_value="")

        with patch("app.session._replay_emit", new_callable=AsyncMock) as mock_emit:
            result = await manager.start_session("test goal", agent_id="test-agent")

        assert "session_id" in result
        mock_emit.assert_awaited_once()
        call = mock_emit.await_args
        assert call.kwargs["event_type"] == "session.started"
        assert call.kwargs["agent_id"] == "test-agent"
        assert call.kwargs["session_id"] == result["session_id"]
        assert call.kwargs["payload"]["goal"] == "test goal"

    @pytest.mark.asyncio
    async def test_update_emits_replay_event(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")

        with patch("app.session._replay_emit", new_callable=AsyncMock) as mock_emit:
            await manager.update("plan", "- [ ] step 1")

        mock_emit.assert_awaited_once()
        call = mock_emit.await_args
        assert call.kwargs["event_type"] == "session.updated"
        assert call.kwargs["session_id"] == "sess-123"
        assert call.kwargs["payload"]["category"] == "plan"
        assert call.kwargs["payload"]["content"] == "- [ ] step 1"

    @pytest.mark.asyncio
    async def test_update_file_includes_key_in_payload(self, manager, mock_redis):
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hlen = AsyncMock(return_value=1)

        with patch("app.session._replay_emit", new_callable=AsyncMock) as mock_emit:
            await manager.update("file", "added function X", key="app/main.py")

        mock_emit.assert_awaited_once()
        call = mock_emit.await_args
        assert call.kwargs["payload"]["category"] == "file"
        assert call.kwargs["payload"]["key"] == "app/main.py"

    @pytest.mark.asyncio
    async def test_complete_session_emits_replay_event(self, manager, mock_redis):
        """UPDATED 2026-08-12: the call now passes agent_id="test-agent". It
        used to omit it (caller defaulted to "default" while meta named
        "test-agent") — a cross-agent completion that complete_session now
        REFUSES, since exactly that gap let one terminal complete another's
        in-flight session via the shared active pointer."""
        mock_redis.get = AsyncMock(return_value="sess-123")
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "test", "status": "active", "agent_id": "test-agent",
        })

        with patch("app.session._replay_emit", new_callable=AsyncMock) as mock_emit:
            await manager.complete_session(
                session_id="sess-123", outcome="done", agent_id="test-agent"
            )

        mock_emit.assert_awaited_once()
        call = mock_emit.await_args
        assert call.kwargs["event_type"] == "session.completed"
        assert call.kwargs["session_id"] == "sess-123"
        assert call.kwargs["agent_id"] == "test-agent"
        assert call.kwargs["payload"]["outcome"] == "done"

    @pytest.mark.asyncio
    async def test_abandon_session_emits_replay_event(self, manager, mock_redis):
        """UPDATED 2026-08-12: passes agent_id="test-agent" for the same
        reason as the completion test above — a caller/owner mismatch is now
        an ownership refusal, not a silent cross-agent abandon."""
        mock_redis.get = AsyncMock(return_value="sess-456")
        mock_redis.hgetall = AsyncMock(return_value={
            "goal": "test", "status": "active", "agent_id": "test-agent",
        })

        with patch("app.session._replay_emit", new_callable=AsyncMock) as mock_emit:
            await manager.abandon_session(
                session_id="sess-456", agent_id="test-agent"
            )

        mock_emit.assert_awaited_once()
        call = mock_emit.await_args
        assert call.kwargs["event_type"] == "session.abandoned"
        assert call.kwargs["session_id"] == "sess-456"
        assert call.kwargs["agent_id"] == "test-agent"

    @pytest.mark.asyncio
    async def test_replay_emit_swallows_exception(self):
        """The _replay_emit helper itself must never raise — best-effort only."""
        from app import session as session_module

        with patch.object(
            session_module, "_ensure_replay", new_callable=AsyncMock,
            side_effect=RuntimeError("init broken"),
        ):
            # Should not raise
            await session_module._replay_emit(
                event_type="session.started",
                session_id="sid",
                agent_id="aid",
                payload={},
            )


class TestEnforceMaxSessions:
    @pytest.mark.asyncio
    async def test_skips_active_sessions(self, manager, mock_redis):
        mock_redis.zcard = AsyncMock(return_value=5)
        manager._s.MAX_SESSIONS = 3
        mock_redis.zrange = AsyncMock(return_value=["s1", "s2"])

        async def hgetall_side_effect(key):
            if "s1" in key:
                return {"status": "active", "agent_id": "default"}
            if "s2" in key:
                return {"status": "completed", "agent_id": "default"}
            return {}

        mock_redis.hgetall = AsyncMock(side_effect=hgetall_side_effect)
        await manager._enforce_max_sessions()
        # s1 (active) should NOT be deleted, s2 (completed) should be
        mock_redis.zrem.assert_called_once()
