"""Undo a credential's writes since T (THREAT-MODEL §5.19, part 2).

Every memory point has carried the verified ``metadata.credential_id`` of its
latest writer since 2026-10-04 (§5.12), but nothing could act on it: once a
compromised key was found, its writes stayed recallable until someone found
and archived them one id at a time. ``POST /admin/memory/revert`` selects the
points one credential wrote in a window, inside the caller's workspace, shows
them (dry run, the default) and -- with ``apply`` -- archives them through the
ordinary lifecycle, which recall already excludes and ``restore`` undoes.
``POST /admin/memory/revert/undo`` restores one revert's points as a batch.

Real VectorClient lifecycle code over an in-memory Qdrant (the local mode
test_identity_migration.py uses), real keys in fakeredis.
"""

from __future__ import annotations

import json
import uuid

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from auth import keys

from app.db.vector import VectorClient
from app.main import app, get_redis, get_vector

WORKSPACE = "workspace-local"
OTHER_WS = "workspace-other"
OWNER = "member-owner"
MALLORY = "member-mallory"
ALICE = "member-alice"
BAD = "bad0000000000bad"
GOOD = "a11ce0000000a11c"
COLLECTION = "firekeep_memory"
SINCE = "2026-10-05T10:00:00+00:00"
UNTIL = "2026-10-05T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.main._replay_emit", AsyncMock())


def _pid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _point(name: str, *, cred: str = BAD, ts: str = "2026-10-05T11:00:00+00:00",
           ws: str | None = WORKSPACE, status: str = "active", **extra) -> PointStruct:
    payload = {
        "text": f"memory {name}", "source": "action_log", "status": status,
        "timestamp": ts, "created_at": ts, "namespace": "default",
        "member_id": MALLORY if cred == BAD else ALICE,
        "metadata": {"credential_id": cred, "runtime_label": "claude"},
        **extra,
    }
    if ws is not None:
        payload["workspace_id"] = ws
    return PointStruct(id=_pid(name), vector=[0.1, 0.2, 0.3, 0.4], payload=payload)


BULK = 300  # more than one scroll page


def _seed() -> list[PointStruct]:
    return [
        _point("in-window"),
        _point("before", ts="2026-10-05T09:59:59+00:00"),
        _point("after-until", ts="2026-10-05T12:00:00+00:00"),
        _point("teammate", cred=GOOD),
        _point("other-workspace", ws=OTHER_WS),
        _point("gc-archived", status="archived", archive_source="gc",
               archived_from_status="active", purge_eligible_at="2027-01-01"),
        _point("superseded", status="superseded"),
        _point("bad-timestamp", ts="not a time"),
        _point("legacy-no-workspace", ws=None),
        _point("naive-timestamp", ts="2026-10-05T11:30:00"),
        *[_point(f"bulk-{i}", ts="2026-10-05T11:15:00+00:00") for i in range(BULK)],
    ]


# Matched with until=UNTIL: in-window, superseded, legacy-no-workspace,
# naive-timestamp (read as UTC), and the bulk points.
EXPECTED = 4 + BULK


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
    points = await vector._client.retrieve(COLLECTION, [_pid(name)], with_payload=True)
    return points[0].payload


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    for member in (OWNER, MALLORY, ALICE):
        await redis.hset(f"auth:member:{member}", mapping={
            "member_id": member, "workspace_id": WORKSPACE,
            "role": "owner" if member == OWNER else "member", "status": "active"})
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


async def _mint(redis, scopes, member=OWNER) -> dict:
    created = await keys.create_key("k", scopes)
    key_hash = keys._hash_key(created["api_key"])
    await redis.hset(f"auth:key:{key_hash}", mapping={
        "member_id": member, "workspace_id": WORKSPACE})
    return created


@pytest_asyncio.fixture
async def cortex_redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def client(vector, cortex_redis):
    app.dependency_overrides[get_vector] = lambda: vector
    app.dependency_overrides[get_redis] = lambda: cortex_redis
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cortex") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(auth_on):
    return {"X-API-Key": (await _mint(auth_on, ["*"]))["api_key"]}


def _body(**over) -> dict:
    body = {"credential_id": BAD, "since": SINCE, "until": UNTIL}
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_changes_nothing(client, admin, vector):
    resp = await client.post("/admin/memory/revert", json=_body(), headers=admin)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied"] is False
    assert data["matched"] == EXPECTED
    assert data["archived"] == 0
    assert data["unparsable_timestamps"] == 1
    assert data["workspace_id"] == WORKSPACE
    assert 0 < len(data["sample"]) <= 10
    assert {"id", "timestamp", "member_id", "status", "text"} <= set(data["sample"][0])
    assert (await _payload(vector, "in-window"))["status"] == "active"


@pytest.mark.asyncio
async def test_without_until_the_window_runs_to_now(client, admin):
    resp = await client.post("/admin/memory/revert",
                             json=_body(until=None), headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched"] == EXPECTED + 1      # after-until joins


# ---------------------------------------------------------------------------
# Apply, then undo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_archives_exactly_the_window_reversibly(
    client, admin, vector, cortex_redis
):
    resp = await client.post("/admin/memory/revert", json=_body(apply=True),
                             headers=admin)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied"] is True
    assert data["archived"] == EXPECTED
    revert_id = data["revert_id"]
    assert revert_id

    hit = await _payload(vector, "in-window")
    assert hit["status"] == "archived"
    assert hit["archive_source"] == "revert"
    assert hit["revert_id"] == revert_id
    assert hit["purge_eligible_at"] is None             # never auto-purged
    assert BAD in hit["archive_reason"]
    assert SINCE in hit["archive_reason"] and UNTIL in hit["archive_reason"]
    assert (await _payload(vector, "superseded"))["archived_from_status"] == "superseded"
    assert (await _payload(vector, "legacy-no-workspace"))["status"] == "archived"

    for untouched in ("before", "after-until", "teammate", "other-workspace",
                      "bad-timestamp"):
        assert (await _payload(vector, untouched))["status"] == "active", untouched
    gc = await _payload(vector, "gc-archived")
    assert gc["archive_source"] == "gc"                 # an existing archive is left alone
    assert gc["purge_eligible_at"] == "2027-01-01"

    entries = [json.loads(e) for e in await cortex_redis.lrange("gc:eviction:log", 0, -1)]
    assert any(e.get("action") == "reverted" and e.get("revert_id") == revert_id
               and e.get("credential_id") == BAD and e.get("count") == EXPECTED
               for e in entries), entries

    # A second apply finds nothing left to archive.
    again = await client.post("/admin/memory/revert", json=_body(apply=True),
                              headers=admin)
    assert again.json()["matched"] == 0

    # Undo: dry run first, then restore every point to where it was.
    dry = await client.post("/admin/memory/revert/undo",
                            json={"revert_id": revert_id}, headers=admin)
    assert dry.status_code == 200, dry.text
    assert dry.json()["matched"] == EXPECTED and dry.json()["restored"] == 0
    assert (await _payload(vector, "in-window"))["status"] == "archived"

    undo = await client.post("/admin/memory/revert/undo",
                             json={"revert_id": revert_id, "apply": True},
                             headers=admin)
    assert undo.status_code == 200, undo.text
    assert undo.json()["restored"] == EXPECTED
    assert (await _payload(vector, "in-window"))["status"] == "active"
    assert (await _payload(vector, "in-window")).get("revert_id") is None
    assert (await _payload(vector, "superseded"))["status"] == "superseded"
    assert (await _payload(vector, "gc-archived"))["status"] == "archived"


# ---------------------------------------------------------------------------
# Who may, and what is refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_key_cannot_revert(client, auth_on, vector):
    member = await _mint(auth_on, sorted(keys.ENROLLABLE_SCOPES), member=ALICE)
    for path, body in (("/admin/memory/revert", _body(apply=True)),
                       ("/admin/memory/revert/undo", {"revert_id": "ab" * 16})):
        resp = await client.post(path, json=body,
                                 headers={"X-API-Key": member["api_key"]})
        assert resp.status_code == 403, resp.text
    assert (await _payload(vector, "in-window"))["status"] == "active"


@pytest.mark.asyncio
async def test_refused_when_auth_is_disabled(client, vector):
    """Without auth every write is the one anonymous credential; there is
    nothing to attribute a revert to."""
    await keys.init_auth(redis_client=None, enabled=False)
    resp = await client.post("/admin/memory/revert", json=_body(apply=True))
    assert resp.status_code == 403
    assert (await _payload(vector, "in-window"))["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"since": "2026-10-05T10:00:00"},            # naive: ambiguous, refused
    {"until": "2026-10-05T09:00:00+00:00"},      # until before since
    {"credential_id": "not-hex"},
    {"credential_id": "anonymous"},
])
async def test_malformed_requests_are_refused(client, admin, over):
    resp = await client.post("/admin/memory/revert", json=_body(**over), headers=admin)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_a_member_key_cannot_restore_a_reverted_memory(client, admin, auth_on, vector):
    """POST /memory/restore needs only memory:write and takes any id; point ids
    are derived from the text, so the poisoner knows its own. A reverted point
    comes back only through an admin (the undo route, or restore with admin)."""
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from app.lifecycle import create_lifecycle_router

    applied = await client.post("/admin/memory/revert", json=_body(apply=True),
                                headers=admin)
    assert applied.json()["archived"] == EXPECTED
    member = await _mint(auth_on, sorted(keys.ENROLLABLE_SCOPES), member=ALICE)

    lifecycle = FastAPI()
    lifecycle.include_router(create_lifecycle_router(
        graph=MagicMock(), vector=vector, redis_client=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lifecycle),
                                 base_url="http://cortex") as lc:
        resp = await lc.post("/memory/restore",
                             json={"memory_ids": [_pid("in-window")]},
                             headers={"X-API-Key": member["api_key"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["restored"] == 0
        assert (await _payload(vector, "in-window"))["status"] == "archived"

        # An ordinary (non-revert) archive is still restorable by a member.
        await vector.update_status(memory_id=_pid("teammate"), status="archived",
                                   reason="manual")
        resp = await lc.post("/memory/restore",
                             json={"memory_ids": [_pid("teammate")]},
                             headers={"X-API-Key": member["api_key"]})
        assert resp.json()["restored"] == 1

        resp = await lc.post("/memory/restore",
                             json={"memory_ids": [_pid("in-window")]}, headers=admin)
        assert resp.json()["restored"] == 1
        assert (await _payload(vector, "in-window"))["status"] == "active"


@pytest.mark.asyncio
async def test_selection_in_a_non_deployment_workspace_is_a_plain_match(vector):
    """workspace_condition's other branch (no legacy IsEmpty arm). Unreachable
    through the route on a single-workspace deployment -- a key from another
    workspace does not authenticate -- so the selection is exercised directly."""
    from datetime import datetime

    from app.memory_revert import select_credential_writes

    points, unparsable = await select_credential_writes(
        vector, workspace_id=OTHER_WS, credential_id=BAD,
        since=datetime.fromisoformat(SINCE), until=datetime.fromisoformat(UNTIL))
    assert [str(p.id) for p in points] == [_pid("other-workspace")]
    assert unparsable == 0
