"""FirekeepScope sessions are owned by the verified member, and Relay is no
longer a confused deputy into Bridge (§5.14).

Before: a scope session's ``agent_id`` came from the request, any key could
post screens into, poll, or complete any scope_id, and an answer for an
origin:"mcp" session was written into Bridge with RELAY_INTERNAL_API_KEY (an
owner-member service key) for whatever label the session named. Since Bridge
binds sessions to their member (#48), that write is refused for every
teammate — so teammates' decisions silently stopped persisting.

Now: the decision is written with the key of a principal who OWNS the scope
session — the answerer's own key when the answerer is the owner, otherwise the
owner's key the next time the owner's agent collects the answer
(scope_ask / scope_check). The relay service key is never presented while
auth is on.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import app.mcp_server as mcp_mod
import app.routes as routes_mod
from app.config import get_settings
from app.mcp_server import scope_ask, scope_check, scope_complete, scope_post, scope_start
from app.routes import (
    route_get_scope_session,
    route_get_scope_sessions,
    route_post_scope_answer,
)
from tests._principal_helpers import ALICE, BOB, DASHBOARD, KEYS, enable_auth, make_request

SCREEN = {"kind": "questions", "title": "Which DB?", "questions": []}
RELAY_SERVICE_KEY = "nxs_relay_service_key"


@pytest.fixture(autouse=True)
def _wiring(monkeypatch, redis):
    enable_auth(monkeypatch)

    async def _get_redis():
        return redis

    monkeypatch.setattr(routes_mod, "_get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "_replay_emit", AsyncMock())
    monkeypatch.setattr(get_settings(), "FIREKEEP_API_KEY", RELAY_SERVICE_KEY)
    monkeypatch.setattr(get_settings(), "BRIDGE_URL", "http://bridge:8070")

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("app.mcp_server.asyncio.sleep", _no_sleep)
    return redis


def as_caller(monkeypatch, ident):
    req = make_request(ident)
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: req)


@pytest.fixture
def bridge():
    with patch("app.scope.httpx.AsyncClient") as cls:
        client = AsyncMock()
        cls.return_value.__aenter__.return_value = client
        yield client


async def _alice_session_with_screen(monkeypatch):
    as_caller(monkeypatch, ALICE)
    session = await scope_start("pick a database", agent_id="alice-agent")
    posted = await scope_post(SCREEN, scope_id=session["scope_id"])
    assert posted["status"] == "posted"
    return session["scope_id"], posted["screen_id"]


async def _answer(ident, scope_id, screen_id):
    return await route_post_scope_answer(make_request(
        ident, path=f"/scope/sessions/{scope_id}/screens/{screen_id}/answer",
        path_params={"scope_id": scope_id, "screen_id": screen_id},
        body={"answers": {"q1": {"choice": "postgres"}}, "source": "dashboard"},
    ))


@pytest.mark.asyncio
async def test_bob_cannot_post_into_poll_or_complete_alices_scope_session(monkeypatch, redis):
    scope_id, _ = await _alice_session_with_screen(monkeypatch)
    as_caller(monkeypatch, BOB)
    assert "error" in await scope_post(SCREEN, scope_id=scope_id)
    assert "error" in await scope_check(scope_id)
    assert "error" in await scope_complete(scope_id)
    assert "error" in await scope_ask(SCREEN, scope_id=scope_id)
    stored = await redis.hgetall(f"nr:scope:session:{scope_id}")
    assert stored["status"] == "active"
    assert await redis.llen(f"nr:scope:screens_order:{scope_id}") == 1


@pytest.mark.asyncio
async def test_a_scope_session_records_its_verified_owner(monkeypatch, redis):
    scope_id, _ = await _alice_session_with_screen(monkeypatch)
    stored = await redis.hgetall(f"nr:scope:session:{scope_id}")
    assert stored["owner_member"] == "member-alice"


@pytest.mark.asyncio
async def test_rest_listing_and_reads_are_owner_or_admin(monkeypatch, redis):
    scope_id, _ = await _alice_session_with_screen(monkeypatch)

    bob_list = await route_get_scope_sessions(make_request(BOB, method="GET", path="/scope/sessions"))
    assert json.loads(bob_list.body)["count"] == 0
    dash_list = await route_get_scope_sessions(make_request(DASHBOARD, method="GET", path="/scope/sessions"))
    assert json.loads(dash_list.body)["count"] == 1

    bob_get = await route_get_scope_session(make_request(
        BOB, method="GET", path=f"/scope/sessions/{scope_id}", path_params={"scope_id": scope_id}))
    assert bob_get.status_code == 404


@pytest.mark.asyncio
async def test_bob_cannot_answer_alices_screen(monkeypatch, redis, bridge):
    scope_id, screen_id = await _alice_session_with_screen(monkeypatch)
    resp = await _answer(BOB, scope_id, screen_id)
    assert resp.status_code in (403, 404)
    assert not await redis.exists(f"nr:scope:answer:{scope_id}:{screen_id}")
    bridge.post.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_answer_for_a_teammate_defers_the_bridge_write_to_the_owner(monkeypatch, redis, bridge):
    scope_id, screen_id = await _alice_session_with_screen(monkeypatch)

    resp = await _answer(DASHBOARD, scope_id, screen_id)
    assert resp.status_code == 200
    # The dashboard is not Alice: neither its key nor the relay service key may
    # write into Alice's Bridge session on her behalf.
    bridge.post.assert_not_called()

    as_caller(monkeypatch, ALICE)
    checked = await scope_check(scope_id)
    assert screen_id in checked["answered"]
    bridge.post.assert_called_once()
    url = bridge.post.call_args[0][0]
    headers = bridge.post.call_args[1]["headers"]
    assert url == "http://bridge:8070/sessions/alice-agent/context"
    assert headers["X-API-Key"] == KEYS["cred-alice"]

    await scope_check(scope_id)  # idempotent: the decision is written once
    bridge.post.assert_called_once()


@pytest.mark.asyncio
async def test_owner_answering_their_own_screen_writes_with_their_own_key(monkeypatch, redis, bridge):
    scope_id, screen_id = await _alice_session_with_screen(monkeypatch)
    resp = await _answer(ALICE, scope_id, screen_id)
    assert resp.status_code == 200
    bridge.post.assert_called_once()
    assert bridge.post.call_args[1]["headers"]["X-API-Key"] == KEYS["cred-alice"]


@pytest.mark.asyncio
async def test_scope_ask_collects_a_deferred_decision_with_the_owners_key(monkeypatch, redis, bridge):
    as_caller(monkeypatch, ALICE)
    session = await scope_start("g", agent_id="alice-agent")
    scope_id = session["scope_id"]

    answered = {}

    async def _answer_once(_s):
        if not answered:
            answered["r"] = await _answer(DASHBOARD, scope_id, f"{scope_id}-1")

    monkeypatch.setattr("app.mcp_server.asyncio.sleep", _answer_once)
    result = await scope_ask(SCREEN, scope_id=scope_id)
    assert result["status"] == "answered"
    assert answered["r"].status_code == 200
    bridge.post.assert_called_once()
    assert bridge.post.call_args[1]["headers"]["X-API-Key"] == KEYS["cred-alice"]


@pytest.mark.asyncio
async def test_the_relay_service_key_is_never_presented_with_auth_on(monkeypatch, redis, bridge):
    scope_id, screen_id = await _alice_session_with_screen(monkeypatch)
    await _answer(DASHBOARD, scope_id, screen_id)
    as_caller(monkeypatch, ALICE)
    await scope_check(scope_id)
    for call in bridge.post.call_args_list:
        assert call[1]["headers"].get("X-API-Key") != RELAY_SERVICE_KEY


@pytest.mark.asyncio
async def test_auth_off_answer_persists_immediately_with_the_configured_key_as_before(monkeypatch, redis, bridge):
    import auth.config as auth_config
    from auth.config import AuthSettings

    monkeypatch.setattr(auth_config, "_settings", AuthSettings(ENABLED=False))
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: (_ for _ in ()).throw(RuntimeError()))
    session = await scope_start("g", agent_id="agent-x")
    posted = await scope_post(SCREEN, scope_id=session["scope_id"])
    resp = await _answer(None, session["scope_id"], posted["screen_id"])
    assert resp.status_code == 200
    bridge.post.assert_called_once()
    assert bridge.post.call_args[0][0] == "http://bridge:8070/sessions/agent-x/context"
    assert bridge.post.call_args[1]["headers"]["X-API-Key"] == RELAY_SERVICE_KEY
