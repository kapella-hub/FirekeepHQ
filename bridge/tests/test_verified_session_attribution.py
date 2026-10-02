"""A session is bound to the VERIFIED principal at start, and starting or
resuming under a label never pauses or repoints another member's session.

2026-10-01 authz audit, F3: ``nb:active:{agent_id}`` is keyed by a
self-asserted label, and START_SESSION_LUA paused whatever it named and
repointed it — so ``ctx_start_session(agent_id="alice")`` from Bob paused
Alice's live session and stole her pointer. Ported from ae1f973's
test_verified_session_attribution.py and adapted: main keeps label-keyed
pointers and adds a guarded compare-and-set instead of runtime-keyed ones.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from auth.config import AuthSettings

from app.config import Settings
from app.session import (
    Caller,
    SessionAccessError,
    SessionManager,
    _POINTER_MOVED,
)


WS = "workspace-local"
ALICE = Caller(WS, "member-alice")
BOB = Caller(WS, "member-bob")
ALICE_META = {
    "goal": "Alice's private task", "status": "active", "agent_id": "codex",
    "owner_member": "member-alice", "owner_workspace": WS, "tags": "[]",
}


@pytest.fixture(autouse=True)
def deployment_ids(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WS)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner")


def _request(identity: dict) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (b"x-api-key", b"nxs_alice"),
            (b"x-agent-id", b"codex"),
            # Self-asserted headers that must never select the owner.
            (b"x-firekeep-delegated-member-id", b"member-mallory"),
        ],
        "state": {"identity": identity},
    })


@pytest.mark.asyncio
async def test_ctx_start_binds_only_the_verified_request_principal(monkeypatch):
    from app import mcp_server
    import auth.config as config_module

    monkeypatch.setattr(config_module, "get_auth_settings",
                        lambda: AuthSettings(ENABLED=True))
    identity = {
        "workspace_id": WS, "member_id": "member-alice",
        "credential_id": "credential-alice", "scopes": ["session:write"],
        "authenticated": True,
    }
    manager = AsyncMock()
    manager.start_session.return_value = {"session_id": "sess-1", "created_at": "now"}
    with (
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "get_http_request", return_value=_request(identity)),
        patch.object(mcp_server, "get_http_headers",
                     return_value={"x-agent-id": "codex",
                                   "x-firekeep-delegated-member-id": "member-mallory"}),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        await mcp_server.ctx_start_session("goal", agent_id="member-mallory")

    kwargs = manager.start_session.await_args.kwargs
    assert kwargs["owner_member"] == "member-alice"
    assert kwargs["owner_workspace"] == WS
    assert kwargs["caller"] == ALICE
    assert "mallory" not in repr({k: v for k, v in kwargs.items() if k != "agent_id"})


@pytest.mark.asyncio
async def test_tools_fail_closed_without_a_verified_principal(monkeypatch):
    """Auth on and no identity attached (a wiring error): no session tool may
    fall back to a label or to the anonymous owner."""
    from app import mcp_server
    import auth.config as config_module

    monkeypatch.setattr(config_module, "get_auth_settings",
                        lambda: AuthSettings(ENABLED=True))
    no_identity = Request({"type": "http", "method": "POST", "path": "/mcp",
                           "headers": [], "state": {}})
    get_manager = AsyncMock()
    with (
        patch.object(mcp_server, "_get_manager", new=get_manager),
        patch.object(mcp_server, "get_http_request", return_value=no_identity),
        patch.object(mcp_server, "get_http_headers", return_value={}),
    ):
        results = [
            await mcp_server.ctx_start_session("goal", agent_id="codex"),
            await mcp_server.ctx_update("plan", "x" * 20, agent_id="codex"),
            await mcp_server.ctx_get_shadow(agent_id="codex"),
            await mcp_server.ctx_list_sessions(),
            await mcp_server.ctx_complete_session("s-alice", agent_id="codex"),
            await mcp_server.ctx_resume_session("s-alice", agent_id="codex"),
            await mcp_server.ctx_abandon_session("s-alice", agent_id="codex"),
        ]
    for result in results:
        assert "No verified principal" in result["error"]
    get_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_flight_prior_art_is_confined_to_the_callers_workspace():
    from app.prior_art import fetch_in_flight

    mgr = AsyncMock()
    mgr.list_sessions.return_value = []
    await fetch_in_flight(mgr, "codex", caller=ALICE)

    kwargs = mgr.list_sessions.await_args.kwargs
    assert kwargs["caller"] == ALICE
    assert kwargs["workspace_wide"] is True


@pytest.mark.asyncio
async def test_start_session_persists_owner_workspace(mock_redis):
    manager = SessionManager(mock_redis, Settings())
    manager._generate_unique_session_id = AsyncMock(return_value="sess-1")

    await manager.start_session(
        "goal", agent_id="codex", owner_member="member-alice",
        owner_workspace=WS, caller=ALICE)

    mapping = mock_redis.hset.await_args_list[0].kwargs["mapping"]
    assert mapping["owner_member"] == "member-alice"
    assert mapping["owner_workspace"] == WS


@pytest.mark.asyncio
async def test_start_refuses_a_label_whose_pointer_names_another_members_session(mock_redis):
    manager = SessionManager(mock_redis, Settings())
    manager._generate_unique_session_id = AsyncMock(return_value="sess-bob")
    mock_redis.get.return_value = "s-alice"
    mock_redis.hgetall.return_value = dict(ALICE_META)

    with pytest.raises(SessionAccessError, match="in use by another member"):
        await manager.start_session("goal", agent_id="codex",
                                    owner_member="member-bob", caller=BOB)

    mock_redis.eval.assert_not_awaited()
    mock_redis.hset.assert_not_called()
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_start_over_own_pointer_compares_and_sets_the_value_it_checked(mock_redis):
    manager = SessionManager(mock_redis, Settings())
    manager._generate_unique_session_id = AsyncMock(return_value="sess-2")
    mock_redis.get.return_value = "s-alice"
    mock_redis.hgetall.return_value = dict(ALICE_META)

    await manager.start_session("goal", agent_id="codex",
                                owner_member="member-alice", caller=ALICE)

    args = mock_redis.eval.await_args.args
    assert args[2] == "nb:active:codex"
    assert args[-1] == "s-alice"  # ARGV[5]: the CAS expectation


@pytest.mark.asyncio
async def test_start_retries_when_the_pointer_moved_after_the_check(mock_redis):
    manager = SessionManager(mock_redis, Settings())
    manager._generate_unique_session_id = AsyncMock(return_value="sess-2")
    mock_redis.eval.side_effect = [_POINTER_MOVED, ""]

    await manager.start_session("goal", agent_id="codex", caller=ALICE)

    assert mock_redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_resume_refuses_to_pause_another_members_session_under_the_label(mock_redis):
    """Bob resuming HIS OWN session under the label 'codex' must not pause
    Alice's live session that the label currently names."""
    manager = SessionManager(mock_redis, Settings())

    async def hgetall(key):
        if key == "nb:session:s-bob":
            return {"status": "paused", "agent_id": "codex",
                    "owner_member": "member-bob", "owner_workspace": WS}
        if key == "nb:session:s-alice":
            return dict(ALICE_META)
        return {}

    mock_redis.hgetall.side_effect = hgetall
    mock_redis.get.return_value = "s-alice"

    with pytest.raises(SessionAccessError, match="in use by another member"):
        await manager.resume_session("s-bob", agent_id="codex", caller=BOB)

    mock_redis.eval.assert_not_awaited()
    mock_redis.hset.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_start_reports_the_label_collision_instead_of_raising(mock_redis):
    from app import mcp_server

    manager = SessionManager(mock_redis, Settings())
    manager._generate_unique_session_id = AsyncMock(return_value="sess-bob")
    mock_redis.get.return_value = "s-alice"
    mock_redis.hgetall.return_value = dict(ALICE_META)
    with (
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "_verified_member_id", return_value="member-bob"),
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        result = await mcp_server.ctx_start_session("goal", agent_id="codex")

    assert "in use by another member" in result["error"]
    mock_redis.eval.assert_not_awaited()


# ---------------------------------------------------------------------------
# The Lua itself — only where fakeredis can run Lua (needs `lupa`). The mock
# tests above pin the Python control flow everywhere; these pin the script.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lua_start_leaves_alices_session_and_pointer_untouched():
    pytest.importorskip("lupa")
    import fakeredis.aioredis

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.hset("nb:session:s-alice", mapping=ALICE_META)
    await r.set("nb:active:codex", "s-alice")
    manager = SessionManager(r, Settings())

    with patch("app.session._replay_emit", new=AsyncMock()):
        with pytest.raises(SessionAccessError):
            await manager.start_session("goal", agent_id="codex",
                                        owner_member="member-bob", caller=BOB)
        mine = await manager.start_session("goal", agent_id="codex-bob",
                                           owner_member="member-bob", caller=BOB)

    assert await r.hget("nb:session:s-alice", "status") == "active"
    assert await r.get("nb:active:codex") == "s-alice"
    assert await r.get("nb:active:codex-bob") == mine["session_id"]
    await r.aclose()


@pytest.mark.asyncio
async def test_lua_start_is_a_no_op_when_the_pointer_moved():
    pytest.importorskip("lupa")
    import fakeredis.aioredis
    from app.session import START_SESSION_LUA

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.hset("nb:session:s-new", mapping={"status": "active"})
    await r.set("nb:active:codex", "s-new")

    out = await r.eval(START_SESSION_LUA, 2, "nb:active:codex", "nb:sessions",
                       "now", 1.0, "s-mine", "nb:session:", "s-old-checked")

    assert out == _POINTER_MOVED
    assert await r.get("nb:active:codex") == "s-new"
    assert await r.hget("nb:session:s-new", "status") == "active"
    await r.aclose()
