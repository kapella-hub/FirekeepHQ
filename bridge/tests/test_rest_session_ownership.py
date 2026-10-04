"""Member ownership on Bridge's REST session routes (2026-10-01 authz audit, F3).

GET /sessions and GET /sessions/{id} were gated on session:read alone — and
every member key carries session:read — so any teammate could enumerate every
member's sessions and read their full shadow (plan, decisions, scratch incl.
the workspace snapshot). POST /sessions/{agent_id}/context wrote into whatever
session the self-asserted label's pointer named.

Now: a caller sees and writes only sessions it owns (workspace + member);
``session:read:workspace`` — SERVICE-ONLY, minted onto FIREKEEP_INTERNAL_KEY for
Cortex's background workers, and held by "*" keys — reads its whole workspace.
Ported from ae1f973's test_rest_session_ownership.py, adapted to main's
owner_member/owner_workspace binding.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from auth.config import AuthSettings

from app.config import Settings
import app.mcp_server as mcp_mod
from app.session import SessionManager


WS = "workspace-local"
OWNER = "member-owner"
ALICE_SESSION_ID = "alice-session"
ALICE_META = {
    "owner_member": "member-alice",
    "owner_workspace": WS,
    "goal": "Alice's private task",
    "status": "paused",
    "agent_id": "codex",
    "created_at": "2026-10-01T00:00:00+00:00",
    "updated_at": "2026-10-01T00:00:00+00:00",
    "tags": "[]",
    "outcome": "",
}
LEGACY_SESSION_ID = "legacy-session"
LEGACY_META = {
    "goal": "pre-upgrade work",
    "status": "paused",
    "agent_id": "old-agent",
    "created_at": "2026-07-01T00:00:00+00:00",
    "updated_at": "2026-07-01T00:00:00+00:00",
    "tags": "[]",
    "outcome": "",
}


def _identity(member_id: str, credential_id: str, *, workspace_id: str = WS,
              scopes: list[str] | None = None) -> dict:
    return {
        "workspace_id": workspace_id,
        "member_id": member_id,
        "credential_id": credential_id,
        "scopes": scopes or ["session:read"],
        "authenticated": True,
    }


def _request(path: str, identity: dict | None, *, session_id: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        # A shared display label must change nothing.
        "headers": [(b"x-agent-id", b"codex")],
        "query_string": b"",
        "path_params": {},
        "state": {},
    }
    if identity is not None:
        scope["state"]["identity"] = identity
    if session_id is not None:
        scope["path_params"] = {"session_id": session_id}
    return Request(scope)


def _post_request(agent_id: str, body: dict, identity: dict) -> Request:
    body_bytes = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request({
        "type": "http",
        "method": "POST",
        "path": f"/sessions/{agent_id}/context",
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"agent_id": agent_id},
        "state": {"identity": identity},
    }, receive)


def _json(response) -> dict:
    return json.loads(response.body)


@pytest.fixture(autouse=True)
def deployment_ids(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WS)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)


@pytest.fixture
def auth_enabled(monkeypatch):
    import auth.asgi as asgi_module
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def manager(mock_redis, monkeypatch) -> SessionManager:
    manager = SessionManager(mock_redis, Settings())
    monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))
    return manager


def _configure(mock_redis, sessions: dict[str, dict]) -> None:
    async def hgetall(key: str):
        for sid, meta in sessions.items():
            if key == f"nb:session:{sid}":
                return dict(meta)
        return {}

    mock_redis.hgetall.side_effect = hgetall
    mock_redis.get.return_value = None
    mock_redis.lrange.return_value = []
    mock_redis.zrevrangebyscore.side_effect = [list(sessions), []]


async def _list_and_read(identity: dict, session_id: str):
    listed = await mcp_mod._list_sessions(_request("/sessions", identity))
    read = await mcp_mod._get_session(
        _request(f"/sessions/{session_id}", identity, session_id=session_id))
    return listed, read


@pytest.mark.asyncio
async def test_same_label_bob_cannot_enumerate_or_read_alice_session(
        auth_enabled, manager, mock_redis):
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    bob = _identity("member-bob", "credential-bob-desktop")

    listed, read = await _list_and_read(bob, ALICE_SESSION_ID)

    assert listed.status_code == 200
    assert _json(listed) == {"sessions": []}
    assert read.status_code == 404
    assert _json(read) == {"error": "Session not found"}


@pytest.mark.asyncio
async def test_alice_can_enumerate_and_read_across_devices(auth_enabled, manager, mock_redis):
    """Credential is provenance, not the permission boundary: member is."""
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    alice_other_device = _identity("member-alice", "credential-alice-desktop")

    listed, read = await _list_and_read(alice_other_device, ALICE_SESSION_ID)

    assert [s["session_id"] for s in _json(listed)["sessions"]] == [ALICE_SESSION_ID]
    assert read.status_code == 200
    assert _json(read)["goal"] == ALICE_META["goal"]


@pytest.mark.asyncio
async def test_workspace_reader_can_enumerate_and_read_same_workspace(
        auth_enabled, manager, mock_redis):
    """FIREKEEP_INTERNAL_KEY's shape: Cortex's OWM/skill/pattern workers read
    every session in their workspace. Its member is the deployment owner, which
    alone would grant nothing on Alice's session."""
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    internal_service = _identity(
        OWNER, "credential-internal",
        scopes=["memory:write", "session:read", "eval:read", "eval:write",
                "session:read:workspace"],
    )

    listed, read = await _list_and_read(internal_service, ALICE_SESSION_ID)

    assert [s["session_id"] for s in _json(listed)["sessions"]] == [ALICE_SESSION_ID]
    assert read.status_code == 200


@pytest.mark.asyncio
async def test_internal_key_without_the_workspace_scope_sees_only_owner_sessions(
        auth_enabled, manager, mock_redis):
    """An internal key minted before 2026-10-01 and not yet upgraded by
    bootstrap-keys.sh fails CLOSED: it sees the owner's sessions, not Alice's."""
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META, LEGACY_SESSION_ID: LEGACY_META})
    old_internal = _identity(
        OWNER, "credential-internal",
        scopes=["memory:write", "session:read", "eval:read", "eval:write"],
    )

    listed, read = await _list_and_read(old_internal, ALICE_SESSION_ID)

    assert [s["session_id"] for s in _json(listed)["sessions"]] == [LEGACY_SESSION_ID]
    assert read.status_code == 404


@pytest.mark.asyncio
async def test_wildcard_key_reads_workspace_wide(auth_enabled, manager, mock_redis):
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    dashboard = _identity(OWNER, "credential-dashboard", scopes=["*"])

    listed, read = await _list_and_read(dashboard, ALICE_SESSION_ID)

    assert [s["session_id"] for s in _json(listed)["sessions"]] == [ALICE_SESSION_ID]
    assert read.status_code == 200


@pytest.mark.asyncio
async def test_workspace_reader_cannot_cross_workspace(auth_enabled, manager, mock_redis):
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    other_workspace_service = _identity(
        OWNER, "credential-internal", workspace_id="workspace-other",
        scopes=["session:read", "session:read:workspace"],
    )

    listed, read = await _list_and_read(other_workspace_service, ALICE_SESSION_ID)

    assert _json(listed) == {"sessions": []}
    assert read.status_code == 404


@pytest.mark.asyncio
async def test_workspace_scope_does_not_replace_session_read(auth_enabled, manager, mock_redis):
    workspace_scope_only = _identity(
        OWNER, "credential-internal", scopes=["session:read:workspace"])

    listed, read = await _list_and_read(workspace_scope_only, ALICE_SESSION_ID)

    assert listed.status_code == 403
    assert read.status_code == 403
    mock_redis.hgetall.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_id_filter_narrows_never_widens(auth_enabled, manager, mock_redis):
    """client/firekeep_client/state.py resolves its own active session with
    ?agent_id=<label>; naming another member's label returns nothing."""
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    bob = _identity("member-bob", "credential-bob")
    request = _request("/sessions", bob)
    request.scope["query_string"] = b"status=paused&agent_id=codex"

    listed = await mcp_mod._list_sessions(request)

    assert _json(listed) == {"sessions": []}


@pytest.mark.asyncio
async def test_legacy_session_is_readable_only_by_the_deployment_owner(
        auth_enabled, manager, mock_redis):
    _configure(mock_redis, {LEGACY_SESSION_ID: LEGACY_META})
    owner = _identity(OWNER, "credential-owner-laptop")
    bob = _identity("member-bob", "credential-bob")

    owner_listed, owner_read = await _list_and_read(owner, LEGACY_SESSION_ID)
    _configure(mock_redis, {LEGACY_SESSION_ID: LEGACY_META})
    bob_listed, bob_read = await _list_and_read(bob, LEGACY_SESSION_ID)

    assert [s["session_id"] for s in _json(owner_listed)["sessions"]] == [LEGACY_SESSION_ID]
    assert owner_read.status_code == 200
    assert _json(bob_listed) == {"sessions": []}
    assert bob_read.status_code == 404


# ---------------------------------------------------------------------------
# POST /sessions/{agent_id}/context — writes land only in an owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_context_refuses_to_write_through_another_members_label(
        auth_enabled, manager, mock_redis):
    """Any session:write holder could name Alice's label and append a
    'decision' to her live session. Refused, and nothing is written."""
    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    mock_redis.get.return_value = ALICE_SESSION_ID  # nb:active:codex -> Alice
    bob = _identity("member-bob", "credential-bob", scopes=["session:write"])

    response = await mcp_mod._post_session_context(_post_request(
        "codex", {"category": "decision", "content": "BOB: Alice approved it"}, bob))

    assert response.status_code == 404
    assert ALICE_SESSION_ID not in _json(response)["error"]
    mock_redis.lpush.assert_not_called()
    mock_redis.hset.assert_not_called()


@pytest.mark.asyncio
async def test_post_context_relay_service_key_writes_only_owner_sessions(
        auth_enabled, manager, mock_redis):
    """RELAY_INTERNAL_API_KEY carries the deployment owner's member, so it can
    persist scope decisions into the owner's sessions (and legacy ones) but not
    a teammate's. Since 2026-10-04 Relay no longer presents it with auth on: it
    writes with the key of the member that owns the scope session
    (THREAT-MODEL §5.14). This test pins Bridge's side of that contract."""
    relay = _identity(OWNER, "credential-relay", scopes=["session:write"])

    _configure(mock_redis, {LEGACY_SESSION_ID: LEGACY_META})
    mock_redis.get.return_value = LEGACY_SESSION_ID
    mock_redis.hget.return_value = "paused"
    ok = await mcp_mod._post_session_context(_post_request(
        "old-agent", {"category": "decision", "content": "screen resolved"}, relay))
    assert ok.status_code == 200
    assert mock_redis.lpush.await_count == 1

    _configure(mock_redis, {ALICE_SESSION_ID: ALICE_META})
    mock_redis.get.return_value = ALICE_SESSION_ID
    refused = await mcp_mod._post_session_context(_post_request(
        "codex", {"category": "decision", "content": "screen resolved"}, relay))
    assert refused.status_code == 404
    assert mock_redis.lpush.await_count == 1
