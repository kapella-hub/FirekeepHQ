"""POST /tasks — the REST twin of relay_task_post.

Cortex's nightly fleet pass creates tasks server-side with FIREKEEP_INTERNAL_KEY,
which carries NO relay scope (deploy/bootstrap-keys.sh:197) and cannot be
migrated on deployed keys — so, like every other /tasks, /dm and /presence
route, this one relies on the blanket key middleware and carries no per-route
scope gate (spec decision 4). Parity with the MCP tool is three side effects,
not one: create, the tasks-channel broadcast, the replay emit.
"""
import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import app.routes as routes_mod
from app.routes import route_post_task
from app.tasks import list_tasks


def _make_request(body, *, raw: bytes | None = None) -> Request:
    body_bytes = raw if raw is not None else json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/tasks",
             "headers": [(b"content-type", b"application/json")],
             "path_params": {}, "query_string": b"", "state": {}}
    return Request(scope, receive)


@pytest.fixture
def patched_redis(monkeypatch, redis):
    async def _fake_get_redis():
        return redis
    monkeypatch.setattr(routes_mod, "_get_redis", _fake_get_redis)
    return redis


@pytest.fixture
def effects(monkeypatch):
    """Record the two non-store side effects instead of running them."""
    import app.pubsub as pubsub_mod
    import app.mcp_server as mcp_mod
    bcast, emit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(pubsub_mod, "broadcast", bcast)
    monkeypatch.setattr(mcp_mod, "_replay_emit", emit)
    return bcast, emit


@pytest.mark.asyncio
async def test_creates_task_and_fires_both_side_effects(patched_redis, effects):
    bcast, emit = effects
    resp = await route_post_task(_make_request({
        "title": "reauthor_stale_skill", "assigner": "cortex-fleet",
        "description": "skill_id=s1 workspace_id=ws", "context": "{\"skill_id\": \"s1\"}",
    }))
    assert resp.status_code == 201
    body = json.loads(resp.body)
    assert body["status"] == "created"
    task = body["task"]
    assert task["title"] == "reauthor_stale_skill" and task["status"] == "pending"
    assert task["assigner"] == "cortex-fleet"
    stored = await list_tasks(patched_redis, title="reauthor_stale_skill")
    assert [t["id"] for t in stored] == [task["id"]]
    bcast.assert_awaited_once()
    assert bcast.call_args.args[1] == "tasks"
    assert "New task: reauthor_stale_skill" in bcast.call_args.args[2]
    emit.assert_awaited_once()
    assert emit.call_args.args[0] == "coordination"
    assert emit.call_args.args[1]["action"] == "task_created"
    assert emit.call_args.args[1]["task_id"] == task["id"]


@pytest.mark.asyncio
async def test_assigned_task_broadcasts_the_assignee(patched_redis, effects):
    bcast, _ = effects
    resp = await route_post_task(_make_request({"title": "t", "assignee": "agent-b"}))
    assert resp.status_code == 201
    assert bcast.call_args.args[2] == "Task for agent-b: t"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 501},
                                  {"title": "ok", "files": "not-a-list"}])
async def test_bad_bodies_are_400_and_touch_no_store(body, monkeypatch, effects):
    spy = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_redis", spy)
    resp = await route_post_task(_make_request(body))
    assert resp.status_code == 400
    assert "error" in json.loads(resp.body)
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_is_400(monkeypatch, effects):
    spy = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_redis", spy)
    resp = await route_post_task(_make_request(None, raw=b"{not json"))
    assert resp.status_code == 400
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_tool_and_route_share_one_helper(monkeypatch, redis):
    """Parity is structural: both paths call handle_post_task, so the three
    side effects cannot drift apart."""
    import app.mcp_server as mcp_mod
    calls = []

    async def fake_helper(r, **kw):
        calls.append(kw)
        return {"id": "task-1", "title": kw["title"], "status": "pending"}

    monkeypatch.setattr(routes_mod, "handle_post_task", fake_helper)

    async def _r():
        return redis
    monkeypatch.setattr(mcp_mod, "get_redis", _r)
    out = await mcp_mod.relay_task_post(title="via-tool", assigner="a1")
    assert out["status"] == "created" and calls[-1]["title"] == "via-tool"
    assert calls[-1]["assigner"] == "a1"
