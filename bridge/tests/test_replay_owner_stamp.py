"""Bridge's replay events carry the session owner's verified principal.

Replay reads are scoped per event (replay/authz.py): a non-admin member sees
only events stamped with its own member, and an unstamped event belongs to the
deployment owner. Bridge's lifecycle and context events (session.started /
session_start, session.updated / ctx_update, session.completed / session_end,
session.abandoned) are most of a session's timeline, and an eval's owner is
read from the session-start event — so they are stamped with the session's
owner_member / owner_workspace, the verified principal #48 records at start.
That is also the right stamp on the reaper's path, which has no caller.

A legacy session (no owner_member) stays unstamped, matching
session_owned_by's legacy rule: it belongs to the deployment owner.

Starting a session runs Lua (START_SESSION_LUA), which fakeredis can only do
with `lupa`; those tests skip without it. Everything else seeds the session
hash directly and runs everywhere.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.config import Settings
from app.session import Caller, SessionManager

WS = "workspace-a"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.hset("nb:session:s-alice", mapping={
        "goal": "g", "status": "active", "agent_id": "codex",
        "owner_member": "member-alice", "owner_workspace": WS, "tags": "[]",
        "created_at": "2026-10-04T00:00:00+00:00",
        "updated_at": "2026-10-04T00:00:00+00:00",
    })
    await r.hset("nb:session:s-legacy", mapping={
        "goal": "g", "status": "active", "agent_id": "old", "tags": "[]",
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
    })
    await r.set("nb:active:codex", "s-alice")
    await r.zadd("nb:sessions", {"s-alice": 2.0, "s-legacy": 1.0})
    yield r
    await r.aclose()


def _stamp(call) -> tuple[str | None, str | None]:
    return call.kwargs.get("workspace_id"), call.kwargs.get("member_id")


def _by_type(calls, key=lambda c: c.kwargs["event_type"]):
    return {key(c): c for c in calls}


@pytest.mark.asyncio
async def test_session_manager_events_carry_the_owner(redis):
    mgr = SessionManager(redis, Settings())
    with patch("app.session._replay_emit", new_callable=AsyncMock) as emit:
        await mgr.update("plan", "- step", agent_id="codex", session_id="s-alice")
        await mgr.complete_session(session_id="s-alice", agent_id="codex", outcome="done",
                                   caller=Caller(WS, "member-alice"))

    events = _by_type(emit.await_args_list)
    for event_type in ("session.updated", "session.completed"):
        assert _stamp(events[event_type]) == (WS, "member-alice"), event_type


@pytest.mark.asyncio
async def test_an_abandoned_session_is_stamped_without_a_caller(redis):
    """The reaper abandons with no request principal; the event still belongs
    to the session's owner."""
    mgr = SessionManager(redis, Settings())
    with patch("app.session._replay_emit", new_callable=AsyncMock) as emit:
        await mgr.abandon_session(session_id="s-alice", agent_id="codex")

    abandoned = _by_type(emit.await_args_list)["session.abandoned"]
    assert _stamp(abandoned) == (WS, "member-alice")


@pytest.mark.asyncio
async def test_a_legacy_session_stays_unattributed(redis):
    mgr = SessionManager(redis, Settings())
    with patch("app.session._replay_emit", new_callable=AsyncMock) as emit:
        await mgr.update("plan", "- step", agent_id="old", session_id="s-legacy")
    assert _stamp(emit.await_args_list[0]) == (None, None)


def _mcp_patches(stack: ExitStack, mgr, *, header_sid: str | None):
    from app import mcp_server

    caller = Caller(WS, "member-alice")
    for p in (
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=mgr)),
        patch.object(mcp_server, "_verified_caller", return_value=caller),
        patch.object(mcp_server, "_verified_member_id", return_value="member-alice"),
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_header_session_id", return_value=header_sid),
        patch.object(mcp_server, "_trigger_eval", new=AsyncMock(return_value=True)),
        patch.object(mcp_server, "_trigger_skill_evaluate",
                     new=AsyncMock(return_value=True)),
        patch.object(mcp_server, "_spawn_background"),
        patch("app.session._replay_emit", new=AsyncMock()),
    ):
        stack.enter_context(p)
    return stack.enter_context(patch("replay.emitter.emit", new_callable=AsyncMock))


@pytest.mark.asyncio
async def test_mcp_context_and_end_events_carry_the_owner(redis):
    from app import mcp_server

    mgr = SessionManager(redis, Settings())
    with ExitStack() as stack:
        emit = _mcp_patches(stack, mgr, header_sid="s-alice")
        await mcp_server.ctx_update(category="progress", content="did a thing",
                                    agent_id="codex")
        await mcp_server.ctx_complete_session(agent_id="codex", outcome="done")
        # The reaper's path: no caller at all.
        await mcp_server.after_abandon("s-alice", "codex", reaped=True)

    calls = [c for c in emit.await_args_list]
    seen = {c.args[0] for c in calls}
    assert {"ctx_update", "session_end"} <= seen
    for call in calls:
        assert _stamp(call) == (WS, "member-alice"), call.args[0]


@pytest.mark.asyncio
async def test_session_start_events_carry_the_owner(redis):
    """session_start's stamp is what decides who may read the session's eval
    (replay.authz.session_owner)."""
    pytest.importorskip("lupa")
    from app import mcp_server

    mgr = SessionManager(redis, Settings())
    with ExitStack() as stack:
        emit = _mcp_patches(stack, mgr, header_sid=None)
        with patch("app.session._replay_emit", new_callable=AsyncMock) as mgr_emit:
            started = await mcp_server.ctx_start_session(goal="g", agent_id="codex")

    assert "session_id" in started, started
    start = _by_type(emit.await_args_list, key=lambda c: c.args[0])["session_start"]
    assert _stamp(start) == (WS, "member-alice")
    manager_start = _by_type(mgr_emit.await_args_list)["session.started"]
    assert _stamp(manager_start) == (WS, "member-alice")
