"""Ordered, idempotent single-workspace migration guards."""

from __future__ import annotations

import json
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from app.workspace_migration import migrate_single_workspace
from auth.workspace import CREDENTIAL_MIGRATION_KEY, MEMORY_MIGRATION_KEY, ensure_workspace


class _Qdrant:
    def __init__(self, points):
        self.points = points
        self.indexes = []

    async def scroll(self, **_kwargs):
        return self.points, None

    async def set_payload(self, *, payload, points, **_kwargs):
        wanted = set(points)
        for point in self.points:
            if point.id in wanted:
                point.payload.update(payload)

    async def create_payload_index(self, **kwargs):
        self.indexes.append(kwargs["field_name"])


class _Vector:
    def __init__(self, points):
        self._client = _Qdrant(points)
        self._collection = "test"


@pytest.mark.asyncio
async def test_workspace_backfill_is_ordered_idempotent_and_preserves_counts(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-a")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-a")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    credential_id = "0123456789abcdef"
    key_hash = credential_id + "a" * 48
    await redis.hset(
        f"auth:key:{key_hash}",
        mapping={
            "agent_id": "legacy-agent",
            "key_id": credential_id,
            "scopes": json.dumps(["memory:read"]),
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    await redis.set(f"auth:cred:{credential_id}", key_hash)
    await redis.zadd("auth:key_index", {credential_id: 1})
    points = [
        SimpleNamespace(id="p1", payload={"text": "one"}),
        SimpleNamespace(id="p2", payload={"text": "two"}),
    ]
    vector = _Vector(points)
    try:
        first = await migrate_single_workspace(redis, vector)
        second = await migrate_single_workspace(redis, vector)
        assert first == second
        record = await redis.hgetall(f"auth:key:{key_hash}")
        assert record["workspace_id"] == "workspace-a"
        assert record["member_id"] == "member-owner-a"
        assert record["device_id"] == "legacy-agent"
        assert "agent_id" not in record
        assert len(points) == 2
        assert all(p.payload["workspace_id"] == "workspace-a" for p in points)
        assert all(p.payload["member_id"] == "member-owner-a" for p in points)
        assert (await redis.get(CREDENTIAL_MIGRATION_KEY)).startswith("complete:")
        assert (await redis.get(MEMORY_MIGRATION_KEY)) == "complete:workspace-a:2"
        assert "workspace_id" in vector._client.indexes
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_memory_marker_is_not_written_when_memory_backfill_fails(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-b")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-b")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _BrokenQdrant(_Qdrant):
        async def scroll(self, **_kwargs):
            raise RuntimeError("qdrant unavailable")

    vector = _Vector([])
    vector._client = _BrokenQdrant([])
    try:
        with pytest.raises(RuntimeError, match="qdrant unavailable"):
            await migrate_single_workspace(redis, vector)
        assert await redis.get(MEMORY_MIGRATION_KEY) is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_workspace_startup_repairs_a_missing_owner_record(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-c")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-c")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.hset(
        "auth:workspace:current",
        mapping={"workspace_id": "workspace-c", "owner_member_id": "member-owner-c"},
    )
    try:
        await ensure_workspace(redis)
        owner = await redis.hgetall("auth:member:member-owner-c")
        assert owner == {
            "member_id": "member-owner-c",
            "workspace_id": "workspace-c",
            "role": "owner",
            "status": "active",
            "created_at": owner["created_at"],
        }
        assert await redis.zscore("auth:member_index", "member-owner-c") is not None
    finally:
        await redis.aclose()
