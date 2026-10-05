"""Relay's own replay events carry the verified principal (§5.18, 2026-10-05).

Since #56 a replay event is visible to a non-admin key only when it is stamped
with that key's member; an unstamped event belongs to the deployment owner
alone (replay/authz.py). Relay emitted every coordination / claim / release
event unstamped, so a teammate's own task, DM, lease and presence events were
invisible in her own timeline. Each emit now passes the ``workspace_id`` /
``member_id`` of the verified caller — never a label (``agent_id``, ``from_id``,
``assigner``) — and only when that caller was actually authenticated: with
auth off the stream is exactly what it was.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.mcp_server as mcp_mod
import app.routes as routes_mod
from app.mcp_server import (
    relay_broadcast,
    relay_claim,
    relay_deregister,
    relay_lease,
    relay_post,
    relay_register,
    relay_release,
    relay_send_dm,
    relay_task_delete,
    relay_task_post,
    relay_task_update,
)
from app.routes import route_post_task
from tests._principal_helpers import ALICE, WORKSPACE, enable_auth, identity, make_request

SERVICE = identity("member-owner", "cred-owner-laptop", ("relay:write:service",))  # an owner-member service key


@pytest.fixture
def emitted(monkeypatch, redis):
    """Every call that reaches replay.emitter.emit, through the real _replay_emit."""
    import replay.emitter as emitter

    async def _get_redis():
        return redis

    monkeypatch.setattr(mcp_mod, "get_redis", _get_redis)
    monkeypatch.setattr(routes_mod, "_get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "_ensure_replay", AsyncMock())
    emit = AsyncMock(return_value="1-0")
    monkeypatch.setattr(emitter, "emit", emit)
    monkeypatch.setattr(emitter, "is_enabled", lambda: True)
    return emit


def as_caller(monkeypatch, ident):
    req = make_request(ident)
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: req)


async def _drive_every_emitting_tool():
    await relay_register("alice-agent", "goal", "host")
    await relay_broadcast("general", "hello", sender="alice-agent")
    await relay_post("a bulletin", author="alice-agent")
    await relay_send_dm("bob-agent", "hi", from_id="alice-agent")
    await relay_claim("src/a.py", agent_id="alice-agent")
    await relay_release("src/a.py", agent_id="alice-agent")
    leased = await relay_lease("src/b.py", agent_id="alice-agent")
    assert leased.get("acquired"), leased
    await relay_release("src/b.py", agent_id="alice-agent", fencing_token=leased["fencing_token"])
    created = await relay_task_post("do a thing", assignee="bob-agent", assigner="spoofed-assigner")
    task_id = created["task"]["id"]
    await relay_task_update(task_id, status="in-progress")
    await relay_task_delete(task_id)
    await relay_deregister("alice-agent")


def _events(emit: AsyncMock) -> list[str]:
    out = []
    for call in emit.call_args_list:
        payload = call.kwargs.get("payload") or {}
        out.append(f"{call.args[0]}:{payload.get('action') or payload.get('channel') or ''}")
    return out


@pytest.mark.asyncio
async def test_every_relay_emit_is_stamped_with_the_verified_caller(monkeypatch, emitted):
    enable_auth(monkeypatch)
    as_caller(monkeypatch, ALICE)
    await _drive_every_emitting_tool()

    events = _events(emitted)
    for expected in (
        "coordination:presence_register", "coordination:general", "coordination:bulletin",
        "coordination:dm_sent", "claim:", "release:", "coordination:task_created",
        "coordination:task_updated", "coordination:task_deleted",
        "coordination:presence_deregister",
    ):
        assert expected in events, (expected, events)
    for call in emitted.call_args_list:
        assert call.kwargs.get("workspace_id") == WORKSPACE, (call, events)
        assert call.kwargs.get("member_id") == "member-alice", (call, events)


@pytest.mark.asyncio
async def test_the_stamp_is_the_principal_never_the_label(monkeypatch, emitted):
    enable_auth(monkeypatch)
    as_caller(monkeypatch, ALICE)
    await relay_task_post("t", assignee="bob-agent", assigner="member-bob")
    call = emitted.call_args_list[-1]
    assert call.kwargs["agent_id"] == "member-bob"  # the label is still the display field
    assert call.kwargs["member_id"] == "member-alice"


@pytest.mark.asyncio
async def test_rest_task_post_is_stamped_with_the_posting_service_key(monkeypatch, emitted):
    enable_auth(monkeypatch)
    resp = await route_post_task(make_request(
        SERVICE, path="/tasks", body={"title": "fleet job", "assigner": "cortex"}))
    assert resp.status_code == 201
    call = emitted.call_args_list[-1]
    assert call.kwargs["payload"]["action"] == "task_created"
    assert call.kwargs["workspace_id"] == WORKSPACE
    assert call.kwargs["member_id"] == "member-owner"


@pytest.mark.asyncio
async def test_auth_off_events_are_emitted_exactly_as_before(monkeypatch, emitted):
    import auth.config as auth_config
    from auth.config import AuthSettings

    monkeypatch.setattr(auth_config, "_settings", AuthSettings(ENABLED=False))
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: (_ for _ in ()).throw(RuntimeError()))
    await _drive_every_emitting_tool()

    assert emitted.call_args_list
    for call in emitted.call_args_list:
        assert "workspace_id" not in call.kwargs and "member_id" not in call.kwargs, call
