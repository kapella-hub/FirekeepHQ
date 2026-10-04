"""/audit/* exposes another member's recall queries to nobody but an admin.

A memory_read replay event carries the recall query text. Before 2026-10-01
`/audit/memory` returned every such event to any valid key. Ported from
ae1f973's workspace filter and extended to the member boundary main needs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.audit import create_audit_router, get_memory_access_summary, get_memory_audit
from auth import keys
from auth.principal import deployment_owner_member_id, deployment_workspace_id

WS = deployment_workspace_id()


def _stream(owner: str) -> list[tuple[str, dict]]:
    return [
        ("5-0", {"event_type": "memory_read", "workspace_id": "workspace-foreign",
                 "member_id": "member-foreign", "agent_id": "x",
                 "payload": '{"query": "foreign secret"}'}),
        ("4-0", {"event_type": "memory_read", "workspace_id": WS, "member_id": "member-alice",
                 "agent_id": "claude", "payload": '{"query": "alice private query"}'}),
        ("3-0", {"event_type": "memory_write", "workspace_id": WS, "member_id": owner,
                 "agent_id": "codex", "payload": '{"action_summary": "owner wrote"}'}),
        # Pre-attribution: no workspace, no member.
        ("2-0", {"event_type": "memory_read", "agent_id": "legacy",
                 "payload": '{"query": "legacy query"}'}),
        ("1-0", {"event_type": "session_start", "workspace_id": WS, "member_id": "member-alice",
                 "agent_id": "claude", "payload": "{}"}),
    ]


def _redis(owner: str):
    r = AsyncMock()
    r.xrevrange = AsyncMock(return_value=_stream(owner))
    return r


@pytest.mark.asyncio
async def test_member_filter_returns_only_that_members_attributed_events():
    events = await get_memory_audit(
        _redis("member-owner"), workspace_id=WS, member_id="member-alice")
    assert [e["payload"].get("query") for e in events] == ["alice private query"]
    assert events[0]["member_id"] == "member-alice"


@pytest.mark.asyncio
async def test_unfiltered_member_sees_workspace_and_legacy_but_never_a_foreign_workspace():
    events = await get_memory_audit(_redis("member-owner"), workspace_id=WS, member_id=None)
    texts = [e["payload"].get("query") or e["payload"].get("action_summary") for e in events]
    assert texts == ["alice private query", "owner wrote", "legacy query"]


@pytest.mark.asyncio
async def test_an_empty_member_id_sees_nothing_rather_than_everything():
    """Fail closed: a caller whose member cannot be determined sees no events."""
    assert await get_memory_audit(_redis("member-owner"), workspace_id=WS, member_id="") == []


@pytest.mark.asyncio
async def test_summary_is_scoped_like_the_event_list():
    summary = await get_memory_access_summary(
        _redis("member-owner"), workspace_id=WS, member_id="member-alice")
    assert summary["total_reads"] == 1
    assert summary["total_writes"] == 0
    assert summary["agents"] == ["claude"]


# --- the routes, with real minted keys --------------------------------------


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def _app(replay) -> FastAPI:
    application = FastAPI()
    application.include_router(create_audit_router(lambda: replay))
    return application


def _client(application):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://c")


async def _member_key(redis, device: str, member_id: str) -> dict:
    """A teammate credential: create_key mints for the owner, so re-stamp the
    member on the stored record the way enrollment writes it."""
    minted = await keys.create_key(device, sorted(keys.ENROLLABLE_SCOPES))
    await redis.hset(f"{keys._KEY_PREFIX}{keys._hash_key(minted['api_key'])}",
                     "member_id", member_id)
    return minted


@pytest.mark.asyncio
async def test_bob_cannot_read_alices_recall_queries(auth_on):
    owner = deployment_owner_member_id()
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")
    async with _client(_app(_redis(owner))) as client:
        bob_view = await client.get("/audit/memory", headers={"X-API-Key": bob["api_key"]})
        alice_view = await client.get("/audit/memory", headers={"X-API-Key": alice["api_key"]})
        bob_summary = await client.get(
            "/audit/memory/summary", headers={"X-API-Key": bob["api_key"]})

    assert bob_view.status_code == 200
    assert bob_view.json()["events"] == []
    assert bob_summary.json()["total_reads"] == 0
    assert [e["payload"]["query"] for e in alice_view.json()["events"]] == ["alice private query"]


@pytest.mark.asyncio
async def test_admin_keys_see_the_whole_workspace(auth_on):
    owner = deployment_owner_member_id()
    dashboard = await keys.create_key("dashboard", ["*"])
    async with _client(_app(_redis(owner))) as client:
        resp = await client.get("/audit/memory", headers={"X-API-Key": dashboard["api_key"]})
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 3  # foreign workspace still excluded


@pytest.mark.asyncio
async def test_audit_requires_replay_read(auth_on):
    relay = await keys.create_key("relay", ["session:write"])
    async with _client(_app(_redis("member-owner"))) as client:
        resp = await client.get("/audit/memory", headers={"X-API-Key": relay["api_key"]})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_auth_disabled_owner_keeps_the_whole_history(monkeypatch):
    """One principal, no member boundary: pre-upgrade events stay visible."""
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    async with _client(_app(_redis("member-owner"))) as client:
        resp = await client.get("/audit/memory")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 3


@pytest.mark.asyncio
async def test_the_deployment_owner_member_sees_unattributed_events_without_admin():
    """Brief rule 5 (the policy Bridge's session_owned_by and every replay read
    use): an event with no recorded member belongs to the deployment OWNER
    member — including the owner's enrolled, non-admin runtime keys — and to
    no other member. #45 hid it from every non-admin, owner included."""
    owner = deployment_owner_member_id()
    events = await get_memory_audit(_redis(owner), workspace_id=WS, member_id=owner)
    texts = [e["payload"].get("query") or e["payload"].get("action_summary") for e in events]
    assert texts == ["owner wrote", "legacy query"]
    # Another workspace's member with the owner's id does not inherit it.
    assert await get_memory_audit(
        _redis(owner), workspace_id="workspace-other", member_id=owner) == []
