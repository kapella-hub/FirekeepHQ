"""Relay stamps the VERIFIED principal on every task write.

THREAT-MODEL row 12 / §5.8: a Hands phone approval is a relay task completed
with the result `approve`, and until this change relay recorded status,
result and assignee but not WHO wrote them — so any holder of the workspace
key, the driving agent included, could complete its own `hands_permit:` task.

The stamp comes from the auth layer (`auth.principal.principal_from_scope`,
fed by FirekeepKeyAuthMiddleware), never from `X-Agent-Id`, `assigner` or
`assignee`, which are display labels any caller sets to anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

import app.mcp_server as mcp_mod
import app.routes as routes_mod
from app.tasks import create_task, get_task, update_task
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.keys import _hash_key, build_credential_record

DASHBOARD = {
    "workspace_id": "ws-1",
    "member_id": "member-owner",
    "credential_id": "cred-dashboard",
    "scopes": ["*"],
}
AGENT = {
    "workspace_id": "ws-1",
    "member_id": "member-owner",
    "credential_id": "cred-agent",
    "scopes": ["relay:read", "relay:write"],
}


def _stamp(identity: dict) -> dict:
    return {
        "workspace_id": identity["workspace_id"],
        "member_id": identity["member_id"],
        "credential_id": identity["credential_id"],
        "authenticated": True,
    }


@pytest.fixture
def effects(monkeypatch):
    import app.pubsub as pubsub_mod
    monkeypatch.setattr(pubsub_mod, "broadcast", AsyncMock())
    monkeypatch.setattr(mcp_mod, "broadcast", AsyncMock())
    monkeypatch.setattr(mcp_mod, "_replay_emit", AsyncMock())


@pytest.fixture
def patched_redis(monkeypatch, redis):
    async def _fake():
        return redis
    monkeypatch.setattr(mcp_mod, "get_redis", _fake)
    monkeypatch.setattr(routes_mod, "_get_redis", _fake)
    return redis


def _as_caller(monkeypatch, identity: dict | None, *, x_agent_id: str = "dashboard"):
    """Make the MCP tools see an HTTP request whose verified identity is
    `identity`, carrying a spoofable X-Agent-Id header that must be ignored."""
    scope = {
        "type": "http",
        "headers": [(b"x-agent-id", x_agent_id.encode())],
        "state": {"identity": identity} if identity is not None else {},
    }
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: SimpleNamespace(scope=scope))


# --- the store layer ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_stamps_created_by_and_reads_it_back(redis):
    task = await create_task(redis, "t", created_by=_stamp(AGENT))
    assert task["created_by"] == _stamp(AGENT)
    stored = await get_task(redis, task["id"])
    assert stored["created_by"] == _stamp(AGENT)


@pytest.mark.asyncio
async def test_a_task_created_without_a_principal_carries_no_stamp(redis):
    task = await create_task(redis, "t")
    assert "created_by" not in task
    assert "created_by" not in await get_task(redis, task["id"])


@pytest.mark.asyncio
async def test_a_terminal_update_stamps_completed_by_and_history(redis):
    task = await create_task(redis, "t", created_by=_stamp(AGENT))
    updated = await update_task(redis, task["id"], status="completed", result="approve",
                                principal=_stamp(DASHBOARD))
    assert updated["completed_by"] == _stamp(DASHBOARD)
    assert updated["updated_by"] == _stamp(DASHBOARD)
    assert updated["created_by"] == _stamp(AGENT)
    assert updated["history"][-1]["state"] == "completed"
    assert updated["history"][-1]["by"] == _stamp(DASHBOARD)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "rejected"])
@pytest.mark.asyncio
async def test_every_terminal_status_records_who_resolved_it(redis, status):
    task = await create_task(redis, "t")
    updated = await update_task(redis, task["id"], status=status, principal=_stamp(DASHBOARD))
    assert updated["completed_by"] == _stamp(DASHBOARD)


@pytest.mark.asyncio
async def test_a_result_only_rewrite_of_a_completed_task_restamps_completed_by(redis):
    """The forgery this guards: a human completes with a non-approve result,
    then the agent rewrites ONLY `result` to "approve". If `completed_by`
    stayed the human's, the broker would read a human-approved task."""
    task = await create_task(redis, "t")
    await update_task(redis, task["id"], status="completed", result="no", principal=_stamp(DASHBOARD))
    forged = await update_task(redis, task["id"], result="approve", principal=_stamp(AGENT))
    assert forged["result"] == "approve"
    assert forged["completed_by"] == _stamp(AGENT)


@pytest.mark.asyncio
async def test_reopening_a_task_clears_completed_by(redis):
    task = await create_task(redis, "t")
    await update_task(redis, task["id"], status="completed", principal=_stamp(DASHBOARD))
    reopened = await update_task(redis, task["id"], status="pending", principal=_stamp(AGENT))
    assert "completed_by" not in reopened
    assert reopened["updated_by"] == _stamp(AGENT)


@pytest.mark.asyncio
async def test_an_unverified_terminal_write_leaves_no_completed_by(redis):
    """A write with no verified principal must not inherit the previous
    writer's stamp — absent is the fail-closed answer."""
    task = await create_task(redis, "t")
    await update_task(redis, task["id"], status="completed", result="no", principal=_stamp(DASHBOARD))
    after = await update_task(redis, task["id"], result="approve", principal=None)
    assert "completed_by" not in after
    assert "updated_by" not in after


# --- the MCP tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_task_update_stamps_the_verified_principal_not_x_agent_id(
    monkeypatch, patched_redis, effects,
):
    _as_caller(monkeypatch, AGENT)
    posted = await mcp_mod.relay_task_post(title="hands_permit:c", assigner="dashboard")
    assert posted["task"]["created_by"] == _stamp(AGENT)

    _as_caller(monkeypatch, AGENT, x_agent_id="dashboard")
    done = await mcp_mod.relay_task_update(
        task_id=posted["task"]["id"], status="completed", result="approve", assignee="dashboard",
    )
    assert done["task"]["completed_by"] == _stamp(AGENT)
    assert done["task"]["completed_by"]["credential_id"] != DASHBOARD["credential_id"]

    listed = await mcp_mod.relay_task_list(title="hands_permit:c")
    assert listed["tasks"][0]["completed_by"] == _stamp(AGENT)
    assert listed["tasks"][0]["created_by"] == _stamp(AGENT)


@pytest.mark.asyncio
async def test_auth_disabled_stamps_the_anonymous_principal_as_unauthenticated(
    monkeypatch, patched_redis, effects,
):
    from auth import config as auth_config
    monkeypatch.setattr(auth_config, "get_auth_settings",
                        lambda: SimpleNamespace(ENABLED=False))
    _as_caller(monkeypatch, None)
    posted = await mcp_mod.relay_task_post(title="t")
    stamp = posted["task"]["created_by"]
    assert stamp["credential_id"] == "anonymous" and stamp["authenticated"] is False


@pytest.mark.asyncio
async def test_no_http_context_means_no_stamp_and_no_crash(monkeypatch, patched_redis, effects):
    def _no_request():
        raise RuntimeError("No active HTTP request found.")
    monkeypatch.setattr(mcp_mod, "get_http_request", _no_request)
    posted = await mcp_mod.relay_task_post(title="t")
    assert posted["status"] == "created" and "created_by" not in posted["task"]
    done = await mcp_mod.relay_task_update(task_id=posted["task"]["id"], status="completed")
    assert done["status"] == "updated" and "completed_by" not in done["task"]


# --- REST POST /tasks behind the REAL auth middleware ------------------------


@pytest.mark.asyncio
async def test_rest_post_stamps_the_key_holder_through_the_real_middleware(
    patched_redis, effects,
):
    auth_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    api_key = "nxs_" + "a" * 48
    record = build_credential_record(
        "cred-fleet", "firekeep-internal",
        ["memory:write", "session:read", "eval:read", "eval:write",
         "session:read:workspace", "relay:write:service"],
        datetime.now(timezone.utc), None,
        workspace_id="ws-1", member_id="member-owner",
    )
    await auth_redis.hset(f"auth:key:{_hash_key(api_key)}", mapping=record)

    app = Starlette(
        routes=[Route("/tasks", routes_mod.route_post_task, methods=["POST"])],
        middleware=[Middleware(
            FirekeepKeyAuthMiddleware, enabled=True, redis_url="redis://unused/7",
            redis_client=auth_redis,
        )],
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        resp = await client.post(
            "/tasks",
            json={"title": "t", "assigner": "dashboard"},
            headers={"X-API-Key": api_key, "X-Agent-Id": "dashboard"},
        )
    assert resp.status_code == 201
    created_by = resp.json()["task"]["created_by"]
    assert created_by == {
        "workspace_id": "ws-1", "member_id": "member-owner",
        "credential_id": "cred-fleet", "authenticated": True,
    }
    stored = await get_task(patched_redis, resp.json()["task"]["id"])
    assert stored["created_by"] == created_by
    await auth_redis.aclose()


def test_a_middleware_identity_is_authenticated_and_scopes_are_not_stored():
    """The middleware's identity carries no `authenticated` key; the stamp
    derives it from WHERE the identity came from. Scopes are not audit data
    and are dropped."""
    from app.tasks import principal_stamp_from_scope
    stamp = principal_stamp_from_scope({"type": "http", "state": {"identity": dict(AGENT)}})
    assert stamp == _stamp(AGENT)
    assert json.loads(json.dumps(stamp)) == stamp


def test_auth_enabled_without_an_attached_identity_stamps_nothing(monkeypatch):
    from auth import config as auth_config
    from app.tasks import principal_stamp_from_scope
    monkeypatch.setattr(auth_config, "get_auth_settings", lambda: SimpleNamespace(ENABLED=True))
    assert principal_stamp_from_scope({"type": "http", "state": {}}) is None
