"""Relay REST routes authorize against the verified principal (§5.14).

The dashboard (DASHBOARD_API_KEY, scopes ["*"]) is the owner's admin surface
and keeps reading any inbox and removing any presence row. A member key is
confined to its own member's inboxes and rows; the path's ``agent_id`` is a
label, not a credential.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import app.mcp_server as mcp_mod
import app.routes as routes_mod
from app.routes import (
    route_delete_presence,
    route_get_dm,
    route_mark_dm_read,
    route_post_dm,
)
from tests._principal_helpers import ALICE, BOB, DASHBOARD, enable_auth, make_request


@pytest.fixture(autouse=True)
def _wiring(monkeypatch, redis):
    enable_auth(monkeypatch)

    async def _get_redis():
        return redis

    monkeypatch.setattr(routes_mod, "_get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "_replay_emit", AsyncMock())
    return redis


async def _register_alice(monkeypatch):
    req = make_request(ALICE)
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: req)
    assert "error" not in await mcp_mod.relay_register("alice-agent", "g", "h")


async def _dm(ident, label, content, from_id="dashboard"):
    return await route_post_dm(make_request(
        ident, path=f"/dm/{label}", path_params={"agent_id": label},
        body={"content": content, "from_id": from_id},
    ))


@pytest.mark.asyncio
async def test_get_dm_shows_bob_nothing_from_alices_inbox_and_the_dashboard_everything(monkeypatch, redis):
    await _register_alice(monkeypatch)
    assert (await _dm(DASHBOARD, "alice-agent", "secret")).status_code == 200

    bob = await route_get_dm(make_request(BOB, method="GET", path="/dm/alice-agent",
                                          path_params={"agent_id": "alice-agent"}))
    assert bob.status_code == 200 and json.loads(bob.body)["count"] == 0

    alice = await route_get_dm(make_request(ALICE, method="GET", path="/dm/alice-agent",
                                            path_params={"agent_id": "alice-agent"}))
    assert json.loads(alice.body)["count"] == 1

    dash = await route_get_dm(make_request(DASHBOARD, method="GET", path="/dm/alice-agent",
                                           path_params={"agent_id": "alice-agent"}))
    assert json.loads(dash.body)["count"] == 1


@pytest.mark.asyncio
async def test_bob_cannot_mark_alices_messages_read(monkeypatch, redis):
    await _register_alice(monkeypatch)
    await _dm(DASHBOARD, "alice-agent", "secret")
    marked = await route_mark_dm_read(make_request(BOB, path="/dm/alice-agent/read",
                                                   path_params={"agent_id": "alice-agent"}))
    assert json.loads(marked.body)["marked_read"] == 0
    stored = [json.loads(m) for m in await redis.lrange("nr:dm:alice-agent", 0, -1)]
    assert [m["read"] for m in stored] == [False]


@pytest.mark.asyncio
async def test_bob_cannot_post_a_dm_as_alices_bound_label(monkeypatch, redis):
    await _register_alice(monkeypatch)
    resp = await _dm(BOB, "dashboard", "approve", from_id="alice-agent")
    assert resp.status_code == 403
    assert await redis.llen("nr:dm:dashboard") == 0


@pytest.mark.asyncio
async def test_delete_presence_refuses_bob_and_allows_the_dashboard(monkeypatch, redis):
    await _register_alice(monkeypatch)
    bob = await route_delete_presence(make_request(BOB, method="DELETE", path="/presence/alice-agent",
                                                   path_params={"agent_id": "alice-agent"}))
    assert bob.status_code == 403
    assert await redis.exists("nr:presence:alice-agent")

    dash = await route_delete_presence(make_request(DASHBOARD, method="DELETE",
                                                    path="/presence/alice-agent",
                                                    path_params={"agent_id": "alice-agent"}))
    assert dash.status_code == 200 and json.loads(dash.body)["removed"] is True


@pytest.mark.asyncio
async def test_rest_routes_fail_closed_without_an_identity(monkeypatch, redis):
    resp = await route_get_dm(make_request(None, method="GET", path="/dm/x",
                                           path_params={"agent_id": "x"}))
    assert resp.status_code == 401
