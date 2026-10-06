"""/memory/deprecate and /memory/confirm: scoped, budgeted, attributed (§5.19).

Before this, both routes had no scope dependency at all: any valid key -- a
relay-only one included -- could archive or deprecate any memory id, mark a
good memory ``superseded_by`` its own (creating a graph supersession edge),
and confirm its own poisoned memory to reset its decay and raise its
confidence. Three threat-5 levers, unattributed and unlimited.

Now both routes require ``memory:write``; every target id AND the
``superseded_by`` id must be a point the caller could recall (its workspace,
and a teammate's member-private point only for an operator); a foreign,
unknown or malformed id is skipped and not counted, with the same answer as a
missing one; a revert archive is untouchable except by an admin (or the undo
route); each requested id spends one unit of the caller's write budget; and the
verified actor is stamped on the point and in the audit trail.

Teammates still deprecate and confirm each other's workspace memories -- that
is the routes' purpose, and nothing in the threat model says otherwise.

Real VectorClient over in-memory Qdrant, real keys in fakeredis.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from auth import keys

from app.config import get_settings
from app.db.vector import VectorClient
from app.lifecycle import create_lifecycle_router

WORKSPACE = "workspace-local"
OTHER_WS = "workspace-other"
OWNER = "member-owner"
ALICE = "member-alice"
BOB = "member-bob"
COLLECTION = "firekeep_memory"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)
    settings = get_settings()
    monkeypatch.setattr(settings, "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 300)
    monkeypatch.setattr(settings, "RATE_LIMIT", "100000/minute")
    monkeypatch.setattr("app.main._replay_emit", AsyncMock())


def _pid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _point(name: str, *, ws: str | None = WORKSPACE, member: str = BOB,
           **extra) -> PointStruct:
    payload = {"text": f"memory {name}", "status": "active", "member_id": member,
               "timestamp": "2026-10-05T11:00:00+00:00", "confirmed_count": 0,
               "contradicted_count": 0, **extra}
    if ws is not None:
        payload["workspace_id"] = ws
    return PointStruct(id=_pid(name), vector=[0.1, 0.2, 0.3, 0.4], payload=payload)


def _seed() -> list[PointStruct]:
    return [
        _point("good"), _point("good-2"), _point("good-3"),
        _point("attacker", member=ALICE),
        _point("foreign", ws=OTHER_WS),
        _point("legacy", ws=None),
        _point("bob-private", visibility="member"),
        _point("reverted", status="archived", archive_source="revert",
               revert_id="ab" * 16, archived_from_status="active"),
    ]


@pytest_asyncio.fixture
async def vector():
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await client.upsert(collection_name=COLLECTION, points=_seed())
    v = VectorClient.__new__(VectorClient)
    v._client = client
    v._collection = COLLECTION
    yield v
    await client.close()


async def _payload(vector, name: str) -> dict:
    return (await vector._client.retrieve(COLLECTION, [_pid(name)], with_payload=True))[0].payload


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    for member in (OWNER, ALICE, BOB):
        await redis.hset(f"auth:member:{member}", mapping={
            "member_id": member, "workspace_id": WORKSPACE,
            "role": "owner" if member == OWNER else "member", "status": "active"})
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


async def _key(redis, scopes, member=ALICE) -> dict:
    created = await keys.create_key("k", scopes)
    await redis.hset(f"auth:key:{keys._hash_key(created['api_key'])}", mapping={
        "member_id": member, "workspace_id": WORKSPACE})
    return {"X-API-Key": created["api_key"], "credential_id": created["credential_id"]}


def _h(key: dict) -> dict:
    return {"X-API-Key": key["X-API-Key"]}


@pytest_asyncio.fixture
async def cortex_redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def graph():
    g = AsyncMock()
    g.create_supersession = AsyncMock()
    return g


@pytest_asyncio.fixture
async def client(vector, graph, cortex_redis):
    application = FastAPI()
    application.include_router(create_lifecycle_router(
        graph=graph, vector=vector, redis_client=cortex_redis))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://cortex") as c:
        yield c


def _deprecate(ids, status="deprecated", **over) -> dict:
    return {"memory_ids": ids, "status": status, "reason": "stale", **over}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_routes_require_memory_write(client, auth_on, vector):
    relay_only = await _key(auth_on, ["relay:read", "relay:write"])
    for path, body in (("/memory/deprecate", _deprecate([_pid("good")], "archived")),
                       ("/memory/confirm", {"memory_ids": [_pid("good")]})):
        assert (await client.post(path, json=body)).status_code == 401
        resp = await client.post(path, json=body, headers=_h(relay_only))
        assert resp.status_code == 403, resp.text
    assert (await _payload(vector, "good"))["status"] == "active"
    assert (await _payload(vector, "good"))["confirmed_count"] == 0


# ---------------------------------------------------------------------------
# Teammates may; outsiders and unknown ids are skipped, indistinguishably
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_teammate_may_deprecate_and_the_actor_is_stamped(
    client, auth_on, vector, cortex_redis
):
    alice = await _key(auth_on, ["memory:write"])
    resp = await client.post("/memory/deprecate",
                             json=_deprecate([_pid("good"), _pid("legacy")]),
                             headers=_h(alice))
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    payload = await _payload(vector, "good")
    assert payload["status"] == "deprecated"
    actor = payload["status_actor"]
    assert actor["credential_id"] == alice["credential_id"]
    assert actor["member_id"] == ALICE
    assert actor["at"]
    trail = [json.loads(e) for e in await cortex_redis.lrange("gc:eviction:log", 0, -1)]
    assert any(e["id"] == _pid("good") and e["action"] == "deprecated"
               and e["credential_id"] == alice["credential_id"]
               and e["member_id"] == ALICE for e in trail), trail


@pytest.mark.asyncio
async def test_foreign_unknown_and_malformed_ids_look_the_same_and_change_nothing(
    client, auth_on, vector
):
    alice = await _key(auth_on, ["memory:write"])
    answers = []
    for target in (_pid("foreign"), _pid("never-written"), "not-a-uuid"):
        resp = await client.post("/memory/deprecate",
                                 json=_deprecate([target], "archived"),
                                 headers=_h(alice))
        assert resp.status_code == 200, resp.text
        answers.append(resp.json())
        resp = await client.post("/memory/confirm", json={"memory_ids": [target]},
                                 headers=_h(alice))
        assert resp.status_code == 200, resp.text
        answers.append(resp.json())
    assert answers == [{"status": "updated", "updated": 0},
                       {"status": "confirmed", "confirmed": 0}] * 3
    foreign = await _payload(vector, "foreign")
    assert foreign["status"] == "active" and foreign["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_superseded_by_must_be_reachable_too(client, auth_on, vector, graph):
    alice = await _key(auth_on, ["memory:write"])
    for newer in (_pid("foreign"), _pid("never-written")):
        resp = await client.post(
            "/memory/deprecate",
            json=_deprecate([_pid("good")], "superseded", superseded_by=newer),
            headers=_h(alice))
        assert resp.json()["updated"] == 0
    assert (await _payload(vector, "good"))["status"] == "active"
    graph.create_supersession.assert_not_called()

    resp = await client.post(
        "/memory/deprecate",
        json=_deprecate([_pid("good")], "superseded", superseded_by=_pid("good-2")),
        headers=_h(alice))
    assert resp.json()["updated"] == 1
    graph.create_supersession.assert_awaited_once()
    assert graph.create_supersession.await_args.kwargs["newer_id"] == _pid("good-2")


@pytest.mark.asyncio
async def test_a_teammates_private_memory_is_reachable_only_by_an_operator(
    client, auth_on, vector
):
    alice = await _key(auth_on, ["memory:write"])
    resp = await client.post("/memory/confirm",
                             json={"memory_ids": [_pid("bob-private")]},
                             headers=_h(alice))
    assert resp.json()["confirmed"] == 0
    bob = await _key(auth_on, ["memory:write"], member=BOB)
    resp = await client.post("/memory/confirm",
                             json={"memory_ids": [_pid("bob-private")]},
                             headers=_h(bob))
    assert resp.json()["confirmed"] == 1
    payload = await _payload(vector, "bob-private")
    assert payload["last_confirmed_by"]["credential_id"] == bob["credential_id"]
    assert payload["last_confirmed_by"]["member_id"] == BOB


# ---------------------------------------------------------------------------
# A revert stays a revert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_cannot_touch_a_revert_archive(client, auth_on, vector):
    """Deprecating a reverted point would turn it from archived (excluded) to
    deprecated (recalled, ranked low) and drop it out of the undo selection;
    re-archiving it would overwrite archive_source and let any member restore
    it. Confirming it would re-arm its timestamp and confidence."""
    alice = await _key(auth_on, ["memory:write"])
    for status in ("deprecated", "archived", "superseded"):
        resp = await client.post("/memory/deprecate",
                                 json=_deprecate([_pid("reverted")], status),
                                 headers=_h(alice))
        assert resp.json()["updated"] == 0
    resp = await client.post("/memory/confirm",
                             json={"memory_ids": [_pid("reverted")]},
                             headers=_h(alice))
    assert resp.json()["confirmed"] == 0
    payload = await _payload(vector, "reverted")
    assert payload["status"] == "archived"
    assert payload["archive_source"] == "revert"
    assert payload["revert_id"] == "ab" * 16
    assert payload["confirmed_count"] == 0

    admin = await _key(auth_on, ["*"], member=OWNER)
    resp = await client.post("/memory/confirm",
                             json={"memory_ids": [_pid("reverted")]}, headers=_h(admin))
    assert resp.json()["confirmed"] == 1


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_requested_id_spends_the_write_budget(
    client, auth_on, vector, monkeypatch
):
    monkeypatch.setattr(get_settings(), "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 3)
    alice = await _key(auth_on, ["memory:write"])
    resp = await client.post("/memory/deprecate",
                             json=_deprecate([_pid("good"), _pid("good-2")]),
                             headers=_h(alice))
    assert resp.status_code == 200, resp.text
    resp = await client.post("/memory/confirm",
                             json={"memory_ids": [_pid("good-3"), _pid("legacy")]},
                             headers=_h(alice))
    assert resp.status_code == 429, resp.text
    assert "Retry-After" in resp.headers
    assert (await _payload(vector, "good-3"))["confirmed_count"] == 0
    # A refused batch spends nothing: one more unit is still available.
    resp = await client.post("/memory/confirm", json={"memory_ids": [_pid("good-3")]},
                             headers=_h(alice))
    assert resp.status_code == 200 and resp.json()["confirmed"] == 1


# ---------------------------------------------------------------------------
# Auth disabled: as before
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_still_deprecates_and_confirms(client, vector, cortex_redis):
    await keys.init_auth(redis_client=None, enabled=False)
    resp = await client.post("/memory/deprecate",
                             json=_deprecate([_pid("good")], "archived"))
    assert resp.status_code == 200 and resp.json()["updated"] == 1
    resp = await client.post("/memory/confirm", json={"memory_ids": [_pid("good-2")]})
    assert resp.status_code == 200 and resp.json()["confirmed"] == 1
    assert [k async for k in cortex_redis.scan_iter("memory:write_limit*")] == []


# ---------------------------------------------------------------------------
# /memory/restore: the same reachability, budget and actor trail
# ---------------------------------------------------------------------------

_ARCHIVED = {"status": "archived", "archive_source": "gc",
             "archived_from_status": "active"}


@pytest_asyncio.fixture
async def archived(vector):
    await vector._client.upsert(collection_name=COLLECTION, points=[
        _point("arch-good", **_ARCHIVED),
        _point("arch-good-2", **_ARCHIVED),
        _point("arch-foreign", ws=OTHER_WS, **_ARCHIVED),
        _point("arch-private", visibility="member", **_ARCHIVED),
    ])
    return vector


@pytest.mark.asyncio
async def test_restore_skips_what_the_caller_could_not_recall(
    client, auth_on, archived, cortex_redis
):
    alice = await _key(auth_on, ["memory:write"])
    answers = []
    for target in (_pid("arch-foreign"), _pid("arch-private"),
                   _pid("never-written"), "not-a-uuid"):
        resp = await client.post("/memory/restore", json={"memory_ids": [target]},
                                 headers=_h(alice))
        assert resp.status_code == 200, resp.text
        answers.append(resp.json())
    assert answers == [{"status": "restored", "restored": 0}] * 4
    assert (await _payload(archived, "arch-foreign"))["status"] == "archived"
    assert (await _payload(archived, "arch-private"))["status"] == "archived"

    resp = await client.post("/memory/restore",
                             json={"memory_ids": [_pid("arch-good")]}, headers=_h(alice))
    assert resp.json()["restored"] == 1
    trail = [json.loads(e) for e in await cortex_redis.lrange("gc:eviction:log", 0, -1)]
    assert [(e["id"], e["action"], e.get("credential_id"), e.get("member_id"))
            for e in trail] == [(_pid("arch-good"), "restored", alice["credential_id"], ALICE)]


@pytest.mark.asyncio
async def test_restore_spends_the_write_budget(client, auth_on, archived, monkeypatch):
    """Archive and restore both cost budget, so cycling a point is bounded."""
    monkeypatch.setattr(get_settings(), "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 1)
    alice = await _key(auth_on, ["memory:write"])
    resp = await client.post(
        "/memory/restore",
        json={"memory_ids": [_pid("arch-good"), _pid("arch-good-2")]},
        headers=_h(alice))
    assert resp.status_code == 429, resp.text
    assert (await _payload(archived, "arch-good"))["status"] == "archived"


@pytest.mark.asyncio
async def test_the_dashboard_key_still_restores_and_is_not_limited(
    client, auth_on, archived, cortex_redis, monkeypatch
):
    """The dashboard's Restore button presents DASHBOARD_API_KEY ("*")."""
    monkeypatch.setattr(get_settings(), "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 1)
    dashboard = await _key(auth_on, ["*"], member=OWNER)
    for name in ("arch-good", "arch-good-2", "arch-private", "reverted"):
        resp = await client.post("/memory/restore", json={"memory_ids": [_pid(name)]},
                                 headers=_h(dashboard))
        assert resp.status_code == 200 and resp.json()["restored"] == 1, (name, resp.text)
    assert [k async for k in cortex_redis.scan_iter("memory:write_limit*")] == []
