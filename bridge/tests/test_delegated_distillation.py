"""A session's distillate is written FOR the member who did the work.

Before 2026-10-04 the background distiller wrote every distillate to
``POST /memory/learn`` with FIREKEEP_BRIDGE_KEY, minted as the deployment
OWNER, so Cortex attributed every teammate's distilled session to the owner.
The distiller now names the session's verified owner (``owner_member``, bound
at ``ctx_start_session`` from the authenticated principal) through Cortex's
delegated-write contract, ``POST /memory/learn/delegated``.

- Auth enabled: delegated, naming ``owner_member`` -- or, for a LEGACY session
  with no recorded owner, the deployment owner (the policy every Bridge path
  uses for those, ``session_owned_by``). A session whose owner cannot be
  established is refused before any write: never attributed to the owner.
- Auth disabled: byte-identical to before -- one principal, ``/memory/learn``.

Ported from ae1f973's test_delegated_distillation.py and adapted: main binds
``owner_member``/``owner_workspace`` (and now ``owner_credential``) at start,
and Bridge uses its one service key rather than a second delegation key.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from starlette.requests import Request

from auth.config import AuthSettings

from app import distiller as distiller_module
from app.config import Settings
from app.distiller import Distiller
from app.distill_worker import DLQ_KEY, QUEUE_KEY, process_queue_once
from app.session import SessionManager

WS = "workspace-local"
OWNER = "member-owner"
ALICE = "member-alice"
ALICE_CRED = "a11ce0000000a11c"


@pytest.fixture(autouse=True)
def deployment_ids(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WS)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(distiller_module, "_auth_enabled", lambda: True, raising=False)


@pytest.fixture
def auth_off(monkeypatch):
    monkeypatch.setattr(distiller_module, "_auth_enabled", lambda: False, raising=False)


def _ok():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"status": "stored", "vector_id": "vector-1"}
    return response


def _distiller() -> Distiller:
    d = Distiller(Settings(FIREKEEP_API_KEY="bridge-service-key"))
    d._client = AsyncMock()
    d._client.post.return_value = _ok()
    return d


def _session(**meta) -> dict:
    return {
        "session_id": "sess-1", "goal": "ship attribution", "plan": "",
        "decisions": [], "progress": [], "tags": [], "files": {},
        "project": "firekeep", "agent_id": "codex", **meta,
    }


ALICE_SESSION = {"owner_member": ALICE, "owner_workspace": WS,
                 "owner_credential": ALICE_CRED}


@pytest.mark.asyncio
async def test_auth_on_writes_for_the_session_owner(auth_on):
    d = _distiller()
    result = await d.distill(_session(**ALICE_SESSION))

    assert result["status"] == "success"
    call = d._client.post.await_args
    assert call.args[0].endswith("/memory/learn/delegated")
    headers = call.kwargs["headers"]
    assert headers["X-API-Key"] == "bridge-service-key"
    assert headers["X-Firekeep-Delegated-Member-Id"] == ALICE
    assert headers["X-Firekeep-Delegated-Credential-Id"] == ALICE_CRED
    assert headers["X-Agent-Id"] == "codex"
    assert headers["X-Session-Id"] == "sess-1"
    assert call.kwargs["json"]["project"] == "firekeep"


@pytest.mark.asyncio
async def test_session_bound_before_credentials_were_recorded_names_member_only(auth_on):
    """Sessions started between #48 and this change carry owner_member but no
    owner_credential: the member is still verified, the credential is absent."""
    d = _distiller()
    await d.distill(_session(owner_member=ALICE, owner_workspace=WS))
    headers = d._client.post.await_args.kwargs["headers"]
    assert headers["X-Firekeep-Delegated-Member-Id"] == ALICE
    assert "X-Firekeep-Delegated-Credential-Id" not in headers


@pytest.mark.asyncio
async def test_legacy_unbound_session_belongs_to_the_deployment_owner(auth_on):
    """Not a fallback: a session with no recorded owner IS the owner's (the
    same rule session_owned_by applies on every Bridge path)."""
    d = _distiller()
    await d.distill(_session())
    call = d._client.post.await_args
    assert call.args[0].endswith("/memory/learn/delegated")
    headers = call.kwargs["headers"]
    assert headers["X-Firekeep-Delegated-Member-Id"] == OWNER
    assert "X-Firekeep-Delegated-Credential-Id" not in headers


@pytest.mark.asyncio
async def test_anonymous_credential_is_never_sent_as_a_credential(auth_on):
    """A session started while auth was off records the anonymous principal."""
    d = _distiller()
    await d.distill(_session(owner_member=OWNER, owner_workspace=WS,
                             owner_credential="anonymous"))
    headers = d._client.post.await_args.kwargs["headers"]
    assert headers["X-Firekeep-Delegated-Member-Id"] == OWNER
    assert "X-Firekeep-Delegated-Credential-Id" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [
    {"owner_member": "", "owner_workspace": WS},           # bound workspace, no member
    {"owner_member": "bad id!", "owner_workspace": WS},     # malformed member
    {"owner_member": ALICE, "owner_workspace": "workspace-other"},  # another tenant
])
async def test_unestablishable_owner_fails_closed_without_writing(auth_on, meta):
    d = _distiller()
    result = await d.distill(_session(**meta))
    assert result["status"] == "failed"
    assert result["error"] == "owner_unverifiable"
    assert result["permanent"] is True
    d._client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_off_is_unchanged(auth_off):
    d = _distiller()
    await d.distill(_session(owner_member=OWNER, owner_workspace=WS,
                             owner_credential="anonymous"))
    call = d._client.post.await_args
    assert call.args[0].endswith("/memory/learn")
    assert call.kwargs["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "bridge-service-key",
        "X-Session-Id": "sess-1",
        "X-Agent-Id": "codex",
    }


# ---------------------------------------------------------------------------
# The binding: recorded synchronously, from the verified principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_session_records_the_owner_credential(mock_redis):
    mgr = SessionManager(mock_redis, Settings())
    with patch("app.session._replay_emit", new=AsyncMock()):
        await mgr.start_session(
            "goal", agent_id="codex", owner_member=ALICE,
            owner_workspace=WS, owner_credential=ALICE_CRED)
    mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
    assert mapping["owner_member"] == ALICE
    assert mapping["owner_credential"] == ALICE_CRED


@pytest.mark.asyncio
async def test_unbound_start_records_no_credential(mock_redis):
    """Internal callers that bind no owner bind no credential either."""
    mgr = SessionManager(mock_redis, Settings())
    with patch("app.session._replay_emit", new=AsyncMock()):
        await mgr.start_session("goal", agent_id="codex")
    mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
    assert mapping["owner_credential"] == ""


@pytest.mark.asyncio
async def test_session_data_carries_the_owner_binding_to_the_distiller():
    """The worker hands get_session_data()'s dict to distill(): the binding
    must arrive there intact (meta is spread into it)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await redis.hset("nb:session:sess-1", mapping={
            "goal": "g", "status": "completed", "agent_id": "codex", "tags": "[]",
            "owner_member": ALICE, "owner_workspace": WS,
            "owner_credential": ALICE_CRED})
        data = await SessionManager(redis, Settings()).get_session_data("sess-1")
        assert (data["owner_member"], data["owner_credential"]) == (ALICE, ALICE_CRED)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_ctx_start_session_binds_the_verified_credential(monkeypatch):
    from app import mcp_server
    import auth.config as config_module

    monkeypatch.setattr(config_module, "get_auth_settings",
                        lambda: AuthSettings(ENABLED=True))
    identity = {"workspace_id": WS, "member_id": ALICE, "credential_id": ALICE_CRED,
                "scopes": ["session:write"], "authenticated": True}
    request = Request({"type": "http", "method": "POST", "path": "/mcp",
                       "headers": [(b"x-api-key", b"nxs_alice")],
                       "state": {"identity": identity}})
    manager = AsyncMock()
    manager.start_session.return_value = {"session_id": "sess-1", "created_at": "now"}
    with (
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "get_http_request", return_value=request),
        patch.object(mcp_server, "get_http_headers",
                     return_value={"x-firekeep-delegated-credential-id": "dead0000dead0000"}),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        await mcp_server.ctx_start_session("goal", agent_id="codex")

    kwargs = manager.start_session.await_args.kwargs
    assert kwargs["owner_member"] == ALICE
    assert kwargs["owner_credential"] == ALICE_CRED


# ---------------------------------------------------------------------------
# The worker: an unestablishable owner is parked, not retried ten times
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_attribution_failure_goes_straight_to_dlq(mock_redis):
    mock_redis.xrange = AsyncMock(return_value=[(
        "1-0", {"session_id": "sess-1", "attempts": "0", "next_attempt_at": "0"})])
    mock_redis.xdel = AsyncMock()
    mock_redis.xadd = AsyncMock()
    mock_redis.lpush = AsyncMock()
    with (
        patch("app.distill_worker.SessionManager") as MockMgr,
        patch("app.distill_worker._get_distiller") as mock_get_d,
    ):
        mgr = AsyncMock()
        mgr.get_session_data = AsyncMock(return_value=_session())
        MockMgr.return_value = mgr
        d = AsyncMock()
        d.distill = AsyncMock(return_value={
            "status": "failed", "error": "owner_unverifiable", "permanent": True})
        mock_get_d.return_value = d

        await process_queue_once(mock_redis, Settings())

    mock_redis.xadd.assert_not_awaited()            # not re-enqueued
    key, raw = mock_redis.lpush.await_args.args
    assert key == DLQ_KEY
    assert json.loads(raw)["error"] == "owner_unverifiable"
    mgr.set_distillation_status.assert_awaited_once_with("sess-1", "dlq")
    mock_redis.xdel.assert_awaited_once_with(QUEUE_KEY, "1-0")
