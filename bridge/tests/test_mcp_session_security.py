"""Bridge MCP tools act for the LIVE caller, never as the deputy service key.

F1 (2026-10-01 authz audit): Bridge's configured Cortex key
(FIREKEEP_API_KEY = FIREKEEP_BRIDGE_KEY) is minted with
member_id=$OWNER_MEMBER_ID. Every synchronous Cortex call Bridge made on a
caller's behalf presented that key, so Cortex filtered member-private
(docdex/maildex) recall by the OWNER's member — and Bob's ctx_update rendered
Alice-the-owner's private chunks into Bob's shadow. Ported from ae1f973's
bridge/tests/test_mcp_session_security.py (outbound cases), adapted to main:
main keeps the configured key when auth is DISABLED (personal mode unchanged),
and the eval trigger deliberately keeps the service key (eval:grade).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import httpx
import pytest
from starlette.requests import Request

from auth.config import AuthSettings


SERVICE_KEY = "nxs_service_owner_bridge_key"
ALICE_KEY = "nxs_alice_owner_member_key"
BOB_KEY = "nxs_bob_member_key"

ALICE = {
    "workspace_id": "workspace-local",
    "member_id": "member-alice",
    "credential_id": "cred-alice",
    "scopes": ["*"],
}
BOB = {
    "workspace_id": "workspace-local",
    "member_id": "member-bob",
    "credential_id": "cred-bob",
    "scopes": ["memory:read", "memory:write", "session:read", "session:write"],
}

ALICE_PRIVATE = "ALICE-PRIVATE maildex: salary negotiation notes for the Q4 offer"
TEAM_SHARED = "TEAM-SHARED: deploy uses tar-over-SSH on port 65002"


def _request(identity: dict | None, api_key: str | None) -> Request:
    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode("utf-8")))
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    if identity is not None:
        scope["state"] = {"identity": identity}
    return Request(scope)


@pytest.fixture
def auth_enabled(monkeypatch):
    import auth.config as config_module
    monkeypatch.setattr(config_module, "get_auth_settings",
                        lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def auth_disabled(monkeypatch):
    import auth.config as config_module
    monkeypatch.setattr(config_module, "get_auth_settings",
                        lambda: AuthSettings(ENABLED=False))


@pytest.fixture
def service_key(monkeypatch):
    from app import mcp_server
    monkeypatch.setattr(mcp_server.settings, "FIREKEEP_API_KEY", SERVICE_KEY)
    monkeypatch.setattr(mcp_server.settings, "PROACTIVE_RECALL_ENABLED", True)
    monkeypatch.setattr(mcp_server.settings, "PROACTIVE_RECALL_CATEGORIES",
                        "plan,decision,progress")
    return SERVICE_KEY


# ---------------------------------------------------------------------------
# The key resolver
# ---------------------------------------------------------------------------


def test_caller_key_is_the_live_request_key_under_auth(auth_enabled, service_key, monkeypatch):
    from app import mcp_server
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, BOB_KEY))
    assert mcp_server._caller_cortex_key() == BOB_KEY


def test_caller_key_never_falls_back_to_the_service_key(auth_enabled, service_key, monkeypatch):
    """Under auth, a missing caller key is None — NOT the owner-minted service
    key. That fallback is exactly the confused deputy."""
    from app import mcp_server
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, None))
    assert mcp_server._caller_cortex_key() is None

    def _no_request():
        raise RuntimeError("no request context")

    monkeypatch.setattr(mcp_server, "get_http_request", _no_request)
    assert mcp_server._caller_cortex_key() is None


def test_auth_disabled_keeps_the_configured_key(auth_disabled, service_key, monkeypatch):
    """Personal mode has one principal (the owner); behaviour is unchanged."""
    from app import mcp_server
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(None, None))
    assert mcp_server._caller_cortex_key() == SERVICE_KEY


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_recall_uses_live_caller_key_not_internal_key(
        auth_enabled, service_key, monkeypatch):
    from app import mcp_server

    manager = AsyncMock()
    manager.update.return_value = {"status": "ok", "component_count": 1}
    manager.get_active_session_id.return_value = "session-1"
    manager.get_session_data.return_value = None
    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, BOB_KEY))
    with (
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "fetch_relevant_memories", new=recall),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        await mcp_server.ctx_update("plan", "a sufficiently long planning update")

    assert recall.await_args.kwargs["api_key"] == BOB_KEY


@pytest.mark.asyncio
async def test_proactive_recall_is_skipped_not_service_keyed_without_a_caller_key(
        auth_enabled, service_key, monkeypatch):
    from app import mcp_server

    manager = AsyncMock()
    manager.update.return_value = {"status": "ok", "component_count": 1}
    manager.get_active_session_id.return_value = "session-1"
    manager.get_session_data.return_value = None
    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, None))
    with (
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "fetch_relevant_memories", new=recall),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        result = await mcp_server.ctx_update("plan", "a sufficiently long planning update")

    assert result["status"] == "ok"
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_recall_keeps_configured_key_when_auth_disabled(
        auth_disabled, service_key, monkeypatch):
    from app import mcp_server

    manager = AsyncMock()
    manager.update.return_value = {"status": "ok", "component_count": 1}
    manager.get_active_session_id.return_value = "session-1"
    manager.get_session_data.return_value = None
    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(None, None))
    with (
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "fetch_relevant_memories", new=recall),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        await mcp_server.ctx_update("plan", "a sufficiently long planning update")

    assert recall.await_args.kwargs["api_key"] == SERVICE_KEY


@pytest.mark.asyncio
async def test_prior_art_recall_uses_live_caller_key(auth_enabled, service_key, monkeypatch):
    """ctx_start_session's prior-art recall returns memories to the caller,
    so it is the same F1 shape as proactive recall."""
    from app import mcp_server

    monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", True)
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, BOB_KEY))
    manager = AsyncMock()
    manager.start_session.return_value = {"session_id": "s-bob", "created_at": "now"}
    assemble = AsyncMock(return_value={})
    with (
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "assemble_prior_art", new=assemble),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
    ):
        await mcp_server.ctx_start_session("build the thing everyone builds", agent_id="bob")

    assert assemble.await_args.kwargs["api_key"] == BOB_KEY


@pytest.mark.asyncio
async def test_completion_forwards_live_key_to_skill_but_keeps_service_key_for_eval(
        auth_enabled, service_key, monkeypatch):
    """Skill evaluate is a synchronous call on the caller's behalf: caller key.
    The eval trigger keeps the service key on purpose — Cortex honors its
    task_result hint only under eval:grade, a SERVICE_ONLY scope minted solely
    onto the Bridge key (see _trigger_eval's docstring)."""
    from app import mcp_server

    manager = AsyncMock()
    manager.complete_session.return_value = {
        "status": "completed", "session_id": "session-1",
        "task_result": None, "task_result_source": None,
    }
    sent: list[tuple[str, dict]] = []

    async def _post(url, **kwargs):
        sent.append((url, kwargs.get("headers") or {}))
        return MagicMock(status_code=200)

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post.side_effect = _post

    detached: list = []

    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, BOB_KEY))
    with (
        patch.object(mcp_server, "get_http_headers", return_value={}),
        patch.object(mcp_server, "_verified_member_id", return_value="member-bob"),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=manager)),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
        patch.object(mcp_server, "_spawn_background", side_effect=detached.append),
        patch("httpx.AsyncClient", return_value=client),
    ):
        await mcp_server.ctx_complete_session("session-1")
        # Drive the detached eval trigger so its headers are observable.
        for coro in detached:
            await coro

    headers_by_route = {
        ("skill" if url.endswith("/skill/evaluate") else "eval"): headers
        for url, headers in sent
    }
    assert headers_by_route["skill"]["X-API-Key"] == BOB_KEY
    assert headers_by_route["eval"]["X-API-Key"] == SERVICE_KEY


@pytest.mark.asyncio
async def test_skill_trigger_sends_no_key_rather_than_the_service_key(
        auth_enabled, service_key):
    from app import mcp_server

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post.return_value = MagicMock(status_code=200)
    with patch("httpx.AsyncClient", return_value=client):
        ok = await mcp_server._trigger_skill_evaluate(
            "http://cortex", "session-1", api_key=None)

    assert ok is False
    client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# End to end: Bob's ctx_update cannot surface Alice's member-private content
# ---------------------------------------------------------------------------


def _fake_cortex(seen_keys: list[str]):
    """A Cortex /memory/recall that filters member-private chunks by the key
    presented — the visibility contract cortex/app/main.py enforces. The
    service key and Alice's key both resolve to the owner member (Alice), as
    bootstrap-keys.sh mints them; Bob's resolves to Bob."""
    member_for_key = {SERVICE_KEY: "member-alice", ALICE_KEY: "member-alice",
                      BOB_KEY: "member-bob"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/memory/recall"
        key = request.headers.get("x-api-key", "")
        seen_keys.append(key)
        member = member_for_key.get(key)
        if member is None:
            return httpx.Response(401, json={"detail": "Unknown API key"})
        sources = [{"content": TEAM_SHARED, "score": 1.0,
                    "metadata": {"raw_score": 0.9}}]
        if member == "member-alice":
            sources.append({"content": ALICE_PRIVATE, "score": 0.9,
                            "metadata": {"raw_score": 0.88,
                                         "visibility": "member"}})
        return httpx.Response(200, json={"sources": sources, "degraded": False})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_bobs_ctx_update_cannot_surface_alice_member_private_content(
        auth_enabled, service_key, monkeypatch):
    from app import mcp_server
    from app.config import get_settings
    from app.session import SessionManager

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    mgr = SessionManager(r, get_settings())
    await r.hset("nb:session:s-bob", mapping={
        "goal": "g", "status": "active", "agent_id": "bob",
        "owner_member": "member-bob", "tags": "[]",
    })
    await r.set("nb:active:bob", "s-bob")

    seen_keys: list[str] = []
    transport = _fake_cortex(seen_keys)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _request(BOB, BOB_KEY))
    with (
        patch.object(mcp_server, "get_http_headers",
                     return_value={"x-api-key": BOB_KEY}),
        patch.object(mcp_server, "_get_manager", new=AsyncMock(return_value=mgr)),
        patch.object(mcp_server, "_replay_emit", new=AsyncMock()),
        patch("app.proactive_recall.httpx.AsyncClient", side_effect=_client),
    ):
        result = await mcp_server.ctx_update(
            "plan", "plan the Q4 compensation work for the team", agent_id="bob")
        shadow = await mcp_server.ctx_get_shadow(agent_id="bob")

    assert result["status"] == "ok"
    assert seen_keys == [BOB_KEY]
    assert SERVICE_KEY not in seen_keys
    stored = json.loads(await r.get("nb:session:s-bob:proactive"))
    assert [m["content"] for m in stored] == [TEAM_SHARED]
    assert ALICE_PRIVATE not in shadow["shadow"]
    assert TEAM_SHARED in shadow["shadow"]
