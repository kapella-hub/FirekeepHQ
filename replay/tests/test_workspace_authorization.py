"""Replay reads are scoped to the verified caller, not to whoever holds replay:read.

Every member key carries ``replay:read`` (it is enrollable), and before
2026-10-04 every ``/replay/*`` route accepted that identity and ignored it:
Bob could read Alice's session timeline (recall query text, memory content
snippets, file paths), inspect her events, reconstruct her context snapshots
and run narrowing over her session.

The rule these tests pin (``replay/authz.py``):

* events carry the WRITER's verified ``workspace_id`` / ``member_id`` (stamped
  by the emit sites that run inside a request);
* a non-admin caller sees only events stamped with its own member, inside its
  own workspace;
* an admin (``admin`` or ``*``: the owner's key, the dashboard) sees the whole
  workspace;
* an UNATTRIBUTED event (emitted before attribution, or by a background
  emitter with no principal) belongs to the deployment owner member — visible
  to that member and to admins, to no one else;
* with auth disabled nothing changes: the single anonymous principal is the
  deployment owner and sees everything, exactly as before.

Ported from ae1f973's tests/test_workspace_authorization.py, adapted to main:
main's ``require_scope`` validates a real ``X-API-Key`` (so these tests mint
keys rather than injecting ``request.state.identity``), and main keeps the raw
``rp:session_idx:{sid}`` layout — scoping is per event, not per index.
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from auth import keys
from auth.principal import deployment_owner_member_id, deployment_workspace_id
from replay.api import create_replay_router
from replay.config import ReplaySettings
from replay.emitter import (
    _STREAM_KEY,
    close_emitter,
    emit,
    init_emitter,
    store_context_snapshot,
)

WS = deployment_workspace_id()
OWNER = deployment_owner_member_id()


@pytest_asyncio.fixture
async def replay_redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_emitter(
        redis_client=redis,
        settings=ReplaySettings(ENABLED=True, REDIS_URL="redis://fake"),
    )
    yield redis
    await close_emitter()
    await redis.aclose()


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
    async def get_replay_redis():
        return replay

    application = FastAPI()
    application.include_router(create_replay_router(get_replay_redis))
    return application


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://c",
    )


async def _member_key(auth_redis, device: str, member_id: str,
                      workspace_id: str = WS) -> str:
    """A teammate credential: create_key mints for the owner, so re-stamp the
    member the way enrollment writes it."""
    minted = await keys.create_key(device, sorted(keys.ENROLLABLE_SCOPES))
    record = f"{keys._KEY_PREFIX}{keys._hash_key(minted['api_key'])}"
    await auth_redis.hset(record, mapping={
        "member_id": member_id, "workspace_id": workspace_id,
    })
    return minted["api_key"]


async def _event_id(redis, stream_id: str) -> str:
    entries = await redis.xrange(_STREAM_KEY, min=stream_id, max=stream_id)
    assert len(entries) == 1
    return entries[0][1]["id"]


async def _seed_shared_session(redis) -> dict[str, str]:
    """One client session id, four writers. Returns {owner_label: event_id}."""
    ids = {}
    for label, ws, member, agent in (
        ("alice", WS, "member-alice", "claude"),
        ("bob", WS, "member-bob", "codex"),
        ("foreign", "workspace-foreign", "member-mallory", "kiro"),
        ("legacy", None, None, "legacy-agent"),
    ):
        stream_id = await emit(
            "memory_read", "shared", agent, {"owner": label},
            workspace_id=ws, member_id=member,
        )
        ids[label] = await _event_id(redis, stream_id)
    return ids


def _owners(response: httpx.Response) -> list[str]:
    return [e["payload"]["owner"] for e in response.json()["events"]]


# --- timeline -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_timeline_shows_only_that_members_events(replay_redis, auth_on):
    await _seed_shared_session(replay_redis)
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")

    async with _client(_app(replay_redis)) as client:
        alice_view = await client.get(
            "/replay/sessions/shared/events",
            # Self-asserted provenance is a display label, never a gate.
            headers={"X-API-Key": alice, "X-Agent-Id": "codex",
                     "X-Member-Id": "member-bob", "X-Workspace-Id": "workspace-foreign"},
        )
        bob_view = await client.get(
            "/replay/sessions/shared/events", headers={"X-API-Key": bob},
        )

    assert alice_view.status_code == 200
    assert _owners(alice_view) == ["alice"]
    # total/has_more describe what the caller may see, not the raw index size.
    assert alice_view.json()["total"] == 1
    assert alice_view.json()["has_more"] is False
    assert _owners(bob_view) == ["bob"]


@pytest.mark.asyncio
async def test_owner_member_sees_unattributed_events_without_admin(replay_redis, auth_on):
    await _seed_shared_session(replay_redis)
    owner_runtime = await _member_key(auth_on, "owner-laptop", OWNER)

    async with _client(_app(replay_redis)) as client:
        view = await client.get(
            "/replay/sessions/shared/events", headers={"X-API-Key": owner_runtime},
        )

    assert view.status_code == 200
    assert _owners(view) == ["legacy"]


@pytest.mark.asyncio
async def test_admin_sees_the_workspace_and_legacy_but_never_a_foreign_workspace(
    replay_redis, auth_on,
):
    await _seed_shared_session(replay_redis)
    dashboard = (await keys.create_key("dashboard", ["*"]))["api_key"]

    async with _client(_app(replay_redis)) as client:
        view = await client.get(
            "/replay/sessions/shared/events", headers={"X-API-Key": dashboard},
        )
        summary = await client.get(
            "/replay/sessions/shared/summary", headers={"X-API-Key": dashboard},
        )

    assert _owners(view) == ["alice", "bob", "legacy"]
    assert view.json()["total"] == 3
    assert summary.json()["event_count"] == 3


@pytest.mark.asyncio
async def test_timeline_pages_over_visible_events_only(replay_redis, auth_on):
    for i in range(3):
        await emit("memory_read", "paged", "claude", {"owner": f"alice-{i}"},
                   workspace_id=WS, member_id="member-alice")
        await emit("memory_read", "paged", "codex", {"owner": f"bob-{i}"},
                   workspace_id=WS, member_id="member-bob")
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")

    async with _client(_app(replay_redis)) as client:
        first = await client.get("/replay/sessions/paged/events",
                                 params={"limit": 2}, headers={"X-API-Key": alice})
        second = await client.get("/replay/sessions/paged/events",
                                  params={"limit": 2, "offset": 2},
                                  headers={"X-API-Key": alice})

    assert _owners(first) == ["alice-0", "alice-1"]
    assert first.json()["total"] == 3
    assert first.json()["has_more"] is True
    assert _owners(second) == ["alice-2"]
    assert second.json()["has_more"] is False


# --- single events, summary ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_foreign_event_is_indistinguishable_from_a_missing_one(replay_redis, auth_on):
    ids = await _seed_shared_session(replay_redis)
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")

    async with _client(_app(replay_redis)) as client:
        own = await client.get(f"/replay/events/{ids['alice']}",
                               headers={"X-API-Key": alice})
        teammate = await client.get(f"/replay/events/{ids['bob']}",
                                    headers={"X-API-Key": alice})
        foreign = await client.get(f"/replay/events/{ids['foreign']}",
                                   headers={"X-API-Key": alice})
        legacy = await client.get(f"/replay/events/{ids['legacy']}",
                                  headers={"X-API-Key": alice})
        missing = await client.get("/replay/events/" + "f" * 32,
                                   headers={"X-API-Key": alice})

    assert own.status_code == 200
    assert own.json()["payload"]["owner"] == "alice"
    for response in (teammate, foreign, legacy, missing):
        assert response.status_code == 404
        assert response.json() == missing.json()


@pytest.mark.asyncio
async def test_summary_counts_and_labels_only_visible_events(replay_redis, auth_on):
    await _seed_shared_session(replay_redis)
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")

    async with _client(_app(replay_redis)) as client:
        summary = await client.get("/replay/sessions/shared/summary",
                                   headers={"X-API-Key": alice})

    assert summary.status_code == 200
    assert summary.json()["event_count"] == 1
    assert summary.json()["agents"] == ["claude"]
    assert summary.json()["event_type_counts"] == {"memory_read": 1}


# --- context snapshots, narrowing ------------------------------------------------


@pytest.mark.asyncio
async def test_context_at_never_returns_another_members_snapshot(replay_redis, auth_on):
    snapshot = await store_context_snapshot("bob's private shadow")
    bob_stream = await emit(
        "ctx_update", "shared", "codex", {}, context_ref=snapshot,
        workspace_id=WS, member_id="member-bob",
    )
    bob_event = await _event_id(replay_redis, bob_stream)
    alice_stream = await emit(
        "memory_read", "shared", "claude", {},
        workspace_id=WS, member_id="member-alice",
    )
    alice_event = await _event_id(replay_redis, alice_stream)
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")

    async with _client(_app(replay_redis)) as client:
        direct = await client.get(
            f"/replay/sessions/shared/context-at/{bob_event}",
            headers={"X-API-Key": alice})
        walked = await client.get(
            f"/replay/sessions/shared/context-at/{alice_event}",
            headers={"X-API-Key": alice})
        bobs_own = await client.get(
            f"/replay/sessions/shared/context-at/{bob_event}",
            headers={"X-API-Key": bob})

    assert "bob's private shadow" not in direct.text
    assert direct.json().get("context") is None
    # The backward walk from Alice's event must skip Bob's snapshot.
    assert "bob's private shadow" not in walked.text
    assert walked.json()["snapshot_type"] == "none"
    assert bobs_own.json()["context"] == "bob's private shadow"


@pytest.mark.asyncio
async def test_narrowing_never_walks_into_another_members_events(replay_redis, auth_on):
    bob_stream = await emit(
        "ctx_update", "shared", "codex", {"secret": "bob"},
        workspace_id=WS, member_id="member-bob",
    )
    bob_event = await _event_id(replay_redis, bob_stream)
    bob_failure_stream = await emit(
        "memory_write", "shared", "codex", {}, outcome="failure",
        workspace_id=WS, member_id="member-bob",
    )
    bob_failure = await _event_id(replay_redis, bob_failure_stream)
    alice_failure_stream = await emit(
        "memory_write", "shared", "claude", {}, outcome="failure",
        workspace_id=WS, member_id="member-alice",
        trace_links=[{
            "target_event_id": bob_event, "link_type": "declared",
            "relationship": "informed_by", "confidence": 1.0,
        }],
    )
    alice_failure = await _event_id(replay_redis, alice_failure_stream)
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")

    async with _client(_app(replay_redis)) as client:
        own = await client.post(
            "/replay/sessions/shared/narrow",
            params={"failure_event_id": alice_failure},
            headers={"X-API-Key": alice})
        foreign = await client.post(
            "/replay/sessions/shared/narrow",
            params={"failure_event_id": bob_failure},
            headers={"X-API-Key": alice})

    assert own.status_code == 200
    assert own.json()["failure_event_found"] is True
    suspect_ids = {s["event_id"] for s in own.json()["suspects"]}
    assert bob_event not in suspect_ids
    assert bob_failure not in suspect_ids
    # Bob's failure reads exactly like an unknown id: no existence oracle.
    assert foreign.status_code == 200
    assert foreign.json()["failure_event_found"] is False
    assert foreign.json()["suspects"] == []


# --- scope gate, auth-disabled compatibility -----------------------------------


@pytest.mark.asyncio
async def test_replay_read_scope_is_still_required(replay_redis, auth_on):
    relay = (await keys.create_key("relay", ["session:write"]))["api_key"]
    async with _client(_app(replay_redis)) as client:
        response = await client.get("/replay/sessions/shared/events",
                                    headers={"X-API-Key": relay})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_auth_disabled_single_user_sees_everything_as_before(replay_redis, monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    ids = await _seed_shared_session(replay_redis)

    async with _client(_app(replay_redis)) as client:
        timeline = await client.get("/replay/sessions/shared/events")
        teammate = await client.get(f"/replay/events/{ids['bob']}")

    assert _owners(timeline) == ["alice", "bob", "foreign", "legacy"]
    assert timeline.json()["total"] == 4
    assert teammate.status_code == 200
