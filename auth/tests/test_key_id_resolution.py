"""Tests for key_id -> record resolution (2026-07-30 hardening spec).

Two credentials sharing a 16-hex key_id prefix cannot be produced by
create_key, so these tests write records into Redis directly. That is the
point: the defect is that resolution GUESSES, and the guess is only
observable when the guess has more than one candidate.
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=r, enabled=True)
    yield r
    await keys.init_auth(redis_client=None, enabled=False)
    await r.aclose()


async def _put(redis, key_hash: str, key_id: str, agent_id: str) -> None:
    """Write a credential record directly, bypassing create_key."""
    await redis.hset(
        f"auth:key:{key_hash}",
        mapping={
            "agent_id": agent_id,
            "scopes": json.dumps(["memory:read"]),
            "created_at": "2026-07-30T00:00:00+00:00",
            "key_id": key_id,
        },
    )
    await redis.zadd("auth:key_index", {key_id: 1.0})


SHARED = "a" * 16
HASH_A = SHARED + "1" * 48
HASH_B = SHARED + "2" * 48


class TestResolveKeyId:
    @pytest.mark.asyncio
    async def test_unknown_id_resolves_to_nothing(self, redis):
        assert await keys._resolve_key_id("deadbeefdeadbeef") == []

    @pytest.mark.asyncio
    async def test_single_record_resolves(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        found = await keys._resolve_key_id(SHARED)
        assert len(found) == 1
        assert found[0][0] == f"auth:key:{HASH_A}"
        assert found[0][1]["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_prefix_match_with_different_stored_id_is_not_a_match(self, redis):
        # Record's hash starts with SHARED, but its stored key_id says otherwise.
        # A prefix match is not an identity.
        await _put(redis, HASH_A, "ffffffffffffffff", "mallory")
        assert await keys._resolve_key_id(SHARED) == []

    @pytest.mark.asyncio
    async def test_collision_resolves_to_both(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        found = await keys._resolve_key_id(SHARED)
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_glob_metacharacters_resolve_to_nothing(self, redis):
        """A key_id arrives from a URL path parameter. '*' must not scan
        the whole keyspace and delete an arbitrary record."""
        await _put(redis, HASH_A, SHARED, "alice")
        for hostile in ("*", "?", "a*", "[a-z]", "a" * 65, "AAAA", "zz"):
            assert await keys._resolve_key_id(hostile) == [], hostile


class TestRevokeKey:
    @pytest.mark.asyncio
    async def test_revokes_single_match(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        assert await keys.revoke_key(SHARED) is True
        assert await redis.exists(f"auth:key:{HASH_A}") == 0
        assert await redis.zscore("auth:key_index", SHARED) is None

    @pytest.mark.asyncio
    async def test_unknown_id_returns_false(self, redis):
        assert await keys.revoke_key("deadbeefdeadbeef") is False

    @pytest.mark.asyncio
    async def test_ambiguity_deletes_nothing_and_raises(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        with pytest.raises(keys.AmbiguousKeyIdError) as exc:
            await keys.revoke_key(SHARED)
        assert exc.value.key_id == SHARED
        assert len(exc.value.matches) == 2
        # Neither deleted, index intact.
        assert await redis.exists(f"auth:key:{HASH_A}") == 1
        assert await redis.exists(f"auth:key:{HASH_B}") == 1
        assert await redis.zscore("auth:key_index", SHARED) is not None

    @pytest.mark.asyncio
    async def test_ambiguity_leaves_both_credentials_valid(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        with pytest.raises(keys.AmbiguousKeyIdError):
            await keys.revoke_key(SHARED)
        # validate_key looks up by FULL hash and must be unaffected.
        assert await keys.validate_key_by_hash(HASH_A) is not None
        assert await keys.validate_key_by_hash(HASH_B) is not None

    @pytest.mark.asyncio
    async def test_glob_metacharacter_id_revokes_nothing(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        assert await keys.revoke_key("*") is False
        assert await redis.exists(f"auth:key:{HASH_A}") == 1


class TestListKeys:
    @pytest.mark.asyncio
    async def test_lists_single_record(self, redis):
        await _put(redis, HASH_A, SHARED, "alice")
        rows = await keys.list_keys()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "alice"
        assert rows[0]["ambiguous"] is False

    @pytest.mark.asyncio
    async def test_collision_emits_every_record(self, redis):
        """A listing that hides a live credential is how one goes unnoticed."""
        await _put(redis, HASH_A, SHARED, "alice")
        await _put(redis, HASH_B, SHARED, "bob")
        rows = await keys.list_keys()
        assert len(rows) == 2
        assert {r["agent_id"] for r in rows} == {"alice", "bob"}
        assert all(r["ambiguous"] is True for r in rows)

    @pytest.mark.asyncio
    async def test_index_member_with_no_verified_record_is_skipped(self, redis):
        # Index entry whose record was deleted out from under it.
        await redis.zadd("auth:key_index", {"bbbbbbbbbbbbbbbb": 1.0})
        assert await keys.list_keys() == []
