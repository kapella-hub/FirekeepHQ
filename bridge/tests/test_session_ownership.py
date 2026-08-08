"""Session ownership and finished-session write guards.

WHY THESE EXIST — two live-sweep findings, both proven between two probe
identities on the production deployment:

1. ``ctx_resume_session`` performed NO ownership check. Any agent that knew a
   session id (``ctx_list_sessions()`` with no filter returns every agent's)
   could call resume and become its owner, on an ACTIVE session, while the
   victim's ``nb:active:<agent>`` pointer was left dangling — so both agents'
   ``ctx_get_shadow`` resolved to the same session, and the memory distilled at
   completion was attributed to the thief.

2. ``SessionManager.update`` resolved the target from the active pointer and
   never read the session's status, so a write into a COMPLETED session
   returned ``{"status": "ok"}``, showed up in the shadow, and was dropped from
   long-term memory — distillation had already run and never runs again.

Each test below pins one of those failures. They are written against the mock
Redis rather than a live one because the defects are in the control flow
(which check runs before which write), not in Redis semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def manager(mock_redis, settings):
    return SessionManager(mock_redis, settings)


class TestResumeOwnership:
    @pytest.mark.asyncio
    async def test_refuses_another_agents_session(self, manager, mock_redis):
        """A resume by a non-owner must be refused, not silently granted.

        Guards the takeover: pre-fix this returned {"status": "active"} and
        rewrote the session's agent_id to the caller.
        """
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "paused", "agent_id": "owner"}
        )
        with pytest.raises(ValueError, match="belongs to agent 'owner'"):
            await manager.resume_session("sess-1", agent_id="thief")

    @pytest.mark.asyncio
    async def test_refuses_active_session_even_with_takeover(self, manager, mock_redis):
        """An explicit takeover still must not evict a live agent.

        The tool documents itself as resuming a PAUSED session; only
        completed/abandoned were refused, so an ACTIVE session was stealable.
        """
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "active", "agent_id": "owner"}
        )
        with pytest.raises(ValueError, match="is ACTIVE for agent 'owner'"):
            await manager.resume_session("sess-1", agent_id="thief", takeover=True)

    @pytest.mark.asyncio
    async def test_owner_resumes_own_paused_session(self, manager, mock_redis):
        """The ordinary path must be untouched by the ownership check."""
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "paused", "agent_id": "owner"}
        )
        result = await manager.resume_session("sess-1", agent_id="owner")
        assert result == {"status": "active", "session_id": "sess-1"}

    @pytest.mark.asyncio
    async def test_takeover_clears_previous_owners_active_pointer(
        self, manager, mock_redis
    ):
        """A deliberate hand-off must TRANSFER the session, not share it.

        The dangling ``nb:active:<prev>`` pointer is what let two agents hold
        one session and what made the completed-session write below reachable.
        It is cleared inside RESUME_SESSION_LUA so the swap is atomic with the
        resume — hence the assertion on the eval arguments.
        """
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "paused", "agent_id": "owner"}
        )
        await manager.resume_session("sess-1", agent_id="thief", takeover=True)
        args = mock_redis.eval.await_args.args
        assert args[1] == 3, "numkeys must include the previous owner's pointer"
        assert args[2] == "nb:active:thief"
        assert args[4] == "nb:active:owner"

    @pytest.mark.asyncio
    async def test_self_resume_passes_empty_previous_owner_key(
        self, manager, mock_redis
    ):
        """Resuming your own session must not name a pointer to clear.

        The Lua guard is `KEYS[3] ~= ''`; passing the caller's own key here
        would delete the pointer the same script is about to set.
        """
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "paused", "agent_id": "owner"}
        )
        await manager.resume_session("sess-1", agent_id="owner")
        assert mock_redis.eval.await_args.args[4] == ""


class TestUpdateRefusesFinishedSessions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "abandoned"])
    async def test_refuses_write_into_finished_session(
        self, manager, mock_redis, status
    ):
        """A write that can never reach long-term memory must fail loudly.

        Pre-fix: {'status': 'ok', 'component_count': N} for an entry that
        distillation had already run past.
        """
        mock_redis.get = AsyncMock(return_value="sess-1")
        mock_redis.hget = AsyncMock(return_value=status)
        with pytest.raises(ValueError, match=f"Cannot update {status} session"):
            await manager.update(category="progress", content="lost write")

    @pytest.mark.asyncio
    async def test_refusal_happens_before_any_write(self, manager, mock_redis):
        """The guard must precede the category dispatch.

        A refusal that fired after the LPUSH would still leave the entry in the
        shadow, which is exactly the state the finding describes.
        """
        mock_redis.get = AsyncMock(return_value="sess-1")
        mock_redis.hget = AsyncMock(return_value="completed")
        with pytest.raises(ValueError):
            await manager.update(category="progress", content="lost write")
        mock_redis.lpush.assert_not_awaited()
        mock_redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["active", "paused", None])
    async def test_allows_live_sessions(self, manager, mock_redis, status):
        """Every non-terminal status still writes — including a missing one.

        A session hash with no ``status`` field (older records) must not be
        treated as finished; the guard tests membership, not absence.
        """
        mock_redis.get = AsyncMock(return_value="sess-1")
        mock_redis.hget = AsyncMock(return_value=status)
        mock_redis.llen = AsyncMock(return_value=1)
        result = await manager.update(category="progress", content="live write")
        assert result["status"] == "ok"


class TestFinishClearsEveryPointer:
    @pytest.mark.asyncio
    async def test_complete_clears_callers_pointer_too(self, manager, mock_redis):
        """Completion must release the session for EVERY agent naming it.

        Clearing only ``meta['agent_id']`` is what left the other agent
        pointing at a finished session — the precondition for the lost-write
        bug above.
        """
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "active", "agent_id": "owner"}
        )
        mock_redis.get = AsyncMock(return_value="sess-1")
        await manager.complete_session(
            session_id="sess-1", outcome="done", agent_id="other"
        )
        deleted = [c.args[0] for c in mock_redis._pipeline.delete.call_args_list]
        assert "nb:active:owner" in deleted
        assert "nb:active:other" in deleted

    @pytest.mark.asyncio
    async def test_abandon_clears_callers_pointer_too(self, manager, mock_redis):
        """Same contract on the abandon path — it had the identical shape."""
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "active", "agent_id": "owner"}
        )
        mock_redis.get = AsyncMock(return_value="sess-1")
        await manager.abandon_session(session_id="sess-1", agent_id="other")
        deleted = [c.args[0] for c in mock_redis._pipeline.delete.call_args_list]
        assert "nb:active:owner" in deleted
        assert "nb:active:other" in deleted

    @pytest.mark.asyncio
    async def test_pointer_naming_a_different_session_is_left_alone(
        self, manager, mock_redis
    ):
        """Never delete a pointer that names somebody else's live session."""
        mock_redis.hgetall = AsyncMock(
            return_value={"status": "active", "agent_id": "owner"}
        )
        mock_redis.get = AsyncMock(return_value="a-different-session")
        await manager.complete_session(session_id="sess-1", agent_id="other")
        deleted = [c.args[0] for c in mock_redis._pipeline.delete.call_args_list]
        assert deleted == []
