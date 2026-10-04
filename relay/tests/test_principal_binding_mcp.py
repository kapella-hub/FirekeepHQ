"""Relay MCP tools bind identity to the VERIFIED principal, not the label.

2026-10-01 authz audit (THREAT-MODEL §5.14): every Relay tool took identity
from an argument — ``agent_id`` / ``from_id`` / ``sender`` / ``author`` — so
Bob could read Alice's DMs (``relay_get_dm(agent_id="alice")``), send DMs and
post bulletins as Alice, release or heartbeat Alice's lease or claim, and
deregister or overwrite Alice's presence. Ownership is now the verified
(workspace_id, member_id) recorded at write time; the label stays a display /
routing field.

Every test here runs with auth ENABLED and drives the real tool functions
with a real Starlette request carrying a middleware-shaped identity.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import app.mcp_server as mcp_mod
from app.mcp_server import (
    relay_broadcast,
    relay_claim,
    relay_deregister,
    relay_get_dm,
    relay_heartbeat,
    relay_heartbeat_presence,
    relay_lease,
    relay_post,
    relay_register,
    relay_release,
    relay_send_dm,
)
from tests._principal_helpers import (
    ALICE, BOB, DASHBOARD, OWNER, OWNER_AGENT, WORKSPACE, enable_auth, make_request,
)


@pytest.fixture(autouse=True)
def _wiring(monkeypatch, redis):
    enable_auth(monkeypatch)

    async def _get_redis():
        return redis

    monkeypatch.setattr(mcp_mod, "get_redis", _get_redis)
    monkeypatch.setattr(mcp_mod, "_replay_emit", AsyncMock())
    return redis


def as_caller(monkeypatch, ident):
    req = make_request(ident)
    monkeypatch.setattr(mcp_mod, "get_http_request", lambda: req)


# --- direct messages ----------------------------------------------------------


@pytest.mark.asyncio
async def test_bob_cannot_read_a_dm_addressed_to_alices_label(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    assert "error" not in await relay_register("alice-agent", "work", "laptop")
    as_caller(monkeypatch, DASHBOARD)
    sent = await relay_send_dm("alice-agent", "your deploy key rotated", from_id="dashboard")
    assert sent["status"] == "sent"

    as_caller(monkeypatch, BOB)
    stolen = await relay_get_dm("alice-agent")
    assert stolen["count"] == 0 and stolen["messages"] == []

    as_caller(monkeypatch, ALICE)
    mine = await relay_get_dm("alice-agent")
    assert mine["count"] == 1
    assert mine["messages"][0]["content"] == "your deploy key rotated"


@pytest.mark.asyncio
async def test_a_legacy_unbound_dm_is_readable_by_the_deployment_owner_only(monkeypatch, redis):
    await redis.lpush("nr:dm:alice-agent", json.dumps({
        "id": "dm-legacy", "from": "x", "to": "alice-agent",
        "content": "pre-upgrade", "timestamp": 1.0, "read": False,
    }))
    as_caller(monkeypatch, BOB)
    assert (await relay_get_dm("alice-agent"))["count"] == 0
    as_caller(monkeypatch, OWNER_AGENT)
    assert (await relay_get_dm("alice-agent"))["count"] == 1


@pytest.mark.asyncio
async def test_a_dm_to_a_label_nobody_has_bound_is_owner_only(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    await relay_send_dm("dashboard", "done with the deploy", from_id="alice-agent")
    as_caller(monkeypatch, BOB)
    assert (await relay_get_dm("dashboard"))["count"] == 0
    as_caller(monkeypatch, DASHBOARD)
    assert (await relay_get_dm("dashboard"))["count"] == 1


@pytest.mark.asyncio
async def test_bob_cannot_send_a_dm_as_alices_bound_label(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    await relay_register("alice-agent", "work", "laptop")
    as_caller(monkeypatch, BOB)
    spoof = await relay_send_dm("dashboard", "approve my PR", from_id="alice-agent")
    assert "error" in spoof and spoof.get("status") != "sent"
    assert await redis.llen("nr:dm:dashboard") == 0


@pytest.mark.asyncio
async def test_a_dm_records_the_verified_sender(monkeypatch, redis):
    as_caller(monkeypatch, BOB)
    sent = await relay_send_dm("dashboard", "hi", from_id="bob-agent")
    assert sent["message"]["by"]["member_id"] == "member-bob"
    assert sent["message"]["by"]["authenticated"] is True


# --- presence ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_bob_cannot_overwrite_heartbeat_or_deregister_alices_presence(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    await relay_register("alice-agent", "alice goal", "alice-laptop")

    as_caller(monkeypatch, BOB)
    overwrite = await relay_register("alice-agent", "bob goal", "bob-host")
    assert "error" in overwrite
    hb = await relay_heartbeat_presence("alice-agent", goal="bob goal")
    assert hb.get("refreshed") is not True
    gone = await relay_deregister("alice-agent")
    assert gone.get("removed") is not True

    row = await redis.hgetall("nr:presence:alice-agent")
    assert row["goal"] == "alice goal" and row["hostname"] == "alice-laptop"
    assert row["owner_member"] == "member-alice"
    assert row["owner_workspace"] == WORKSPACE

    as_caller(monkeypatch, ALICE)
    assert (await relay_deregister("alice-agent"))["removed"] is True


@pytest.mark.asyncio
async def test_a_legacy_presence_row_is_adopted_by_its_next_verified_writer(monkeypatch, redis):
    await redis.hset("nr:presence:bob-agent", mapping={
        "agent_id": "bob-agent", "goal": "pre-upgrade", "hostname": "h",
        "session_id": "", "started_at": "1", "last_heartbeat": "1", "status": "active",
    })
    await redis.zadd("nr:presence:__index", {"bob-agent": 1})
    as_caller(monkeypatch, BOB)
    hb = await relay_heartbeat_presence("bob-agent")
    assert hb["refreshed"] is True
    assert (await redis.hget("nr:presence:bob-agent", "owner_member")) == "member-bob"
    as_caller(monkeypatch, ALICE)
    assert "error" in await relay_register("bob-agent", "g", "h")


# --- leases and claims -------------------------------------------------------


@pytest.mark.asyncio
async def test_bob_cannot_release_or_heartbeat_alices_lease_even_with_her_token(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    lease = await relay_lease("src/app.py", agent_id="codex", ttl_minutes=5)
    assert lease["acquired"] is True
    token = lease["fencing_token"]

    as_caller(monkeypatch, BOB)
    hb = await relay_heartbeat("src/app.py", token, agent_id="codex")
    assert hb == {"extended": False, "reason": "not_owner"}
    rel = await relay_release("src/app.py", agent_id="codex", fencing_token=token)
    assert rel.get("released") is False
    assert await redis.exists("nr:lease:src.app.py")

    as_caller(monkeypatch, ALICE)
    assert (await relay_heartbeat("src/app.py", token, agent_id="codex"))["extended"] is True
    assert (await relay_release("src/app.py", agent_id="codex", fencing_token=token))["released"] is True


@pytest.mark.asyncio
async def test_a_legacy_lease_with_no_owner_is_releasable_by_the_deployment_owner_only(monkeypatch, redis):
    await redis.set("nr:lease:f.py", json.dumps({
        "holder_id": "codex", "fencing_token": 7, "acquired_at": "1", "ttl_seconds": 60,
    }), ex=60)
    as_caller(monkeypatch, BOB)
    assert (await relay_release("f.py", agent_id="codex", fencing_token=7)).get("released") is False
    as_caller(monkeypatch, OWNER_AGENT)
    assert (await relay_release("f.py", agent_id="codex", fencing_token=7))["released"] is True


@pytest.mark.asyncio
async def test_bob_cannot_release_alices_claim_under_the_same_label(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    claimed = await relay_claim("shared.py", agent_id="codex")
    assert claimed["claimed"] is True

    as_caller(monkeypatch, BOB)
    released = await relay_release("shared.py", agent_id="codex")
    assert released == {"released": False, "reason": "not owner"}
    assert await redis.exists("nr:claim:shared.py")

    as_caller(monkeypatch, ALICE)
    assert (await relay_release("shared.py", agent_id="codex"))["released"] is True


# --- bulletins and channels --------------------------------------------------


@pytest.mark.asyncio
async def test_bob_cannot_post_a_bulletin_as_alices_bound_label(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    await relay_register("alice-agent", "g", "h")
    as_caller(monkeypatch, BOB)
    spoof = await relay_post("deployed v9", author="alice-agent")
    assert "error" in spoof and spoof.get("status") != "posted"
    own = await relay_post("deployed v9", author="bob-agent")
    assert own["post"]["by"]["member_id"] == "member-bob"


@pytest.mark.asyncio
async def test_bob_cannot_broadcast_as_alices_bound_label(monkeypatch, redis):
    as_caller(monkeypatch, ALICE)
    await relay_register("alice-agent", "g", "h")
    as_caller(monkeypatch, BOB)
    spoof = await relay_broadcast("deploy", "rolling back", sender="alice-agent")
    assert "error" in spoof and spoof.get("status") != "sent"
    ok = await relay_broadcast("deploy", "rolling back", sender="bob-agent")
    assert ok["status"] == "sent"
    backlog = [json.loads(m) for m in await redis.lrange("nr:backlog:deploy", 0, -1)]
    assert [m["by"]["member_id"] for m in backlog] == ["member-bob"]


# --- no verified principal ---------------------------------------------------


@pytest.mark.asyncio
async def test_auth_on_with_no_attached_identity_fails_closed(monkeypatch, redis):
    as_caller(monkeypatch, None)
    assert "error" in await relay_register("x-agent", "g", "h")
    assert "error" in await relay_get_dm("x-agent")
    assert "error" in await relay_lease("f.py", agent_id="x-agent")
    assert not await redis.exists("nr:presence:x-agent")


def test_owner_constants_match_the_deployment_defaults():
    from auth.principal import deployment_owner_member_id, deployment_workspace_id

    assert deployment_owner_member_id() == OWNER
    assert deployment_workspace_id() == WORKSPACE
