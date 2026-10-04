"""Member-bound Relay operations require relay:* scopes; the internal service
key gets ONE exact, non-enrollable scope for its two Relay writes.

Every key deploy/bootstrap-keys.sh mints carries the deployment OWNER's
member_id. Once Relay bound records to the verified member (§5.14), a leaked
FIREKEEP_INTERNAL_KEY — which Sentinel and Cortex's workers hold, and which
carries no relay scope — would have been the owner inside Relay: reading the
owner's DMs, releasing the owner's leases, completing tasks (a Hands phone
approval is a completed relay task, THREAT-MODEL row 12). Member-bound tools
now declare relay:read / relay:write; the internal key's legitimate Relay
writes — Sentinel's alert broadcast and Cortex's nightly POST /tasks — are
authorized by the service-only ``relay:write:service``, honoured by exactly
those two operations.

Driven through the REAL FirekeepKeyAuthMiddleware with real key records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

import app.mcp_server as mcp_mod
import app.routes as routes_mod
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.keys import _hash_key, build_credential_record
from tests._principal_helpers import enable_auth

# deploy/bootstrap-keys.sh's declared list for FIREKEEP_INTERNAL_KEY.
INTERNAL_SCOPES = [
    "memory:write", "session:read", "eval:read", "eval:write",
    "session:read:workspace", "relay:write:service",
]
MEMBER_SCOPES = ["relay:read", "relay:write", "memory:read", "memory:write"]
INTERNAL_KEY = "nxs_" + "1" * 48
MEMBER_KEY = "nxs_" + "2" * 48


def _tool_endpoint(call):
    async def endpoint(request):
        mcp_mod.get_http_request = lambda: request  # restored by monkeypatch below
        return JSONResponse(await call())
    return endpoint


@pytest_asyncio.fixture
async def client(monkeypatch, redis):
    enable_auth(monkeypatch)
    auth_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    now = datetime.now(timezone.utc)
    for key, cred, scopes in (
        (INTERNAL_KEY, "cred-internal", INTERNAL_SCOPES),
        (MEMBER_KEY, "cred-owner-laptop", MEMBER_SCOPES),
    ):
        record = build_credential_record(cred, cred, scopes, now, None)
        await auth_redis.hset(f"auth:key:{_hash_key(key)}", mapping=record)

    async def _get_redis():
        return redis

    monkeypatch.setattr(mcp_mod, "get_redis", _get_redis)
    monkeypatch.setattr(routes_mod, "_get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "_replay_emit", AsyncMock())
    monkeypatch.setattr(mcp_mod, "get_http_request", mcp_mod.get_http_request)

    await redis.lpush("nr:dm:owner-agent", '{"id":"dm-1","from":"x","to":"owner-agent",'
                      '"content":"owner secret","timestamp":1,"read":false}')

    tools = {
        "broadcast": lambda: mcp_mod.relay_broadcast("alerts", "[ERROR] git: x", sender="sentinel"),
        "get_dm": lambda: mcp_mod.relay_get_dm("owner-agent"),
        "send_dm": lambda: mcp_mod.relay_send_dm("owner-agent", "hi", from_id="sentinel"),
        "post": lambda: mcp_mod.relay_post("note", author="sentinel"),
        "lease": lambda: mcp_mod.relay_lease("f.py", agent_id="sentinel"),
        "release": lambda: mcp_mod.relay_release("f.py", agent_id="sentinel"),
        "register": lambda: mcp_mod.relay_register("sentinel", "g", "h"),
        "task_post": lambda: mcp_mod.relay_task_post("t", assigner="sentinel"),
        "scope_start": lambda: mcp_mod.scope_start("g", agent_id="sentinel"),
    }
    routes = [Route(f"/tool/{name}", _tool_endpoint(call), methods=["POST"])
              for name, call in tools.items()]
    routes += [
        Route("/tasks", routes_mod.route_post_task, methods=["POST"]),
        Route("/dm/{agent_id}", routes_mod.route_get_dm, methods=["GET"]),
        Route("/presence/{agent_id}", routes_mod.route_delete_presence, methods=["DELETE"]),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(
        FirekeepKeyAuthMiddleware, enabled=True, redis_url="redis://unused/7",
        redis_client=auth_redis,
    )])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://relay") as c:
        yield c
    await auth_redis.aclose()


def _h(key):
    return {"X-API-Key": key}


@pytest.mark.asyncio
async def test_the_internal_key_keeps_its_two_relay_writes(client):
    sent = await client.post("/tool/broadcast", headers=_h(INTERNAL_KEY))
    assert sent.json() == {"status": "sent", "channel": "alerts"}
    task = await client.post("/tasks", headers=_h(INTERNAL_KEY),
                             json={"title": "reauthor skill x", "assigner": "cortex-fleet"})
    assert task.status_code == 201


@pytest.mark.parametrize("tool", [
    "get_dm", "send_dm", "post", "lease", "release", "register", "task_post", "scope_start",
])
@pytest.mark.asyncio
async def test_the_internal_key_is_not_the_owner_inside_relay(client, tool):
    resp = await client.post(f"/tool/{tool}", headers=_h(INTERNAL_KEY))
    body = resp.json()
    assert body.get("status") == "forbidden", body
    assert "relay:" in body["error"]


@pytest.mark.asyncio
async def test_the_internal_key_cannot_read_the_owners_inbox_over_rest(client):
    resp = await client.get("/dm/owner-agent", headers=_h(INTERNAL_KEY))
    assert resp.status_code == 403
    gone = await client.delete("/presence/owner-agent", headers=_h(INTERNAL_KEY))
    assert gone.status_code == 403


@pytest.mark.asyncio
async def test_a_member_key_with_relay_scopes_is_unaffected(client):
    dm = await client.post("/tool/get_dm", headers=_h(MEMBER_KEY))
    assert dm.json()["count"] == 1
    for tool in ("broadcast", "post", "lease", "register", "task_post", "scope_start"):
        body = (await client.post(f"/tool/{tool}", headers=_h(MEMBER_KEY))).json()
        assert body.get("status") not in ("forbidden", "unauthorized", "unavailable"), (tool, body)


def test_relay_write_service_is_service_only():
    from auth import keys
    assert "relay:write:service" in keys.SCOPES
    assert "relay:write:service" in keys.SERVICE_ONLY_SCOPES
    assert "relay:write:service" not in keys.ENROLLABLE_SCOPES
    assert "relay:write:service" not in keys.ANONYMOUS_SCOPES
