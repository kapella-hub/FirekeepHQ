"""create_key must write the credential and its index member in ONE transaction.

Redis MULTI/EXEC does not roll back on a command error. What it guarantees is
that every command is queued before any of them applies — so a process death
part-way through applies NONE of them. That is precisely the failure this
pins, and sequential awaits left two windows:

  HSET without ZADD   a credential validate_key accepts (it looks up the full
                      hash) that list_keys can never show (it walks the index).
                      Invisible to every enumeration path and unrevocable by
                      key_id — the same invariant TestSubsystemInvariant in
                      test_key_id_resolution.py pins for revoke sequences,
                      reached through the write path instead.

  HSET without EXPIRE a credential that was meant to expire and never will.
                      Permanent when it was meant to be temporary — which
                      silently defeats any expiry-based revocation story.

Atomicity is not observable from the outcome of a successful call, so this
test pins the structure rather than pretending to observe the property.
"""
from __future__ import annotations

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


class TestCreateKeyIsAtomic:
    @pytest.mark.asyncio
    async def test_all_writes_go_through_one_transaction(self, redis):
        """No direct hset/zadd on the client; exactly one transactional pipeline."""
        seen: dict[str, object] = {"hset": 0, "zadd": 0, "expire": 0, "pipelines": []}
        orig_hset, orig_zadd, orig_expire = redis.hset, redis.zadd, redis.expire
        orig_pipeline = redis.pipeline

        async def spy_hset(*a, **k):
            seen["hset"] += 1
            return await orig_hset(*a, **k)

        async def spy_zadd(*a, **k):
            seen["zadd"] += 1
            return await orig_zadd(*a, **k)

        async def spy_expire(*a, **k):
            seen["expire"] += 1
            return await orig_expire(*a, **k)

        def spy_pipeline(*a, **k):
            seen["pipelines"].append(k.get("transaction", a[0] if a else None))
            return orig_pipeline(*a, **k)

        redis.hset, redis.zadd, redis.expire = spy_hset, spy_zadd, spy_expire
        redis.pipeline = spy_pipeline
        try:
            result = await keys.create_key("atomic-agent", ["memory:read"], expires_days=1)
        finally:
            redis.hset, redis.zadd, redis.expire = orig_hset, orig_zadd, orig_expire
            redis.pipeline = orig_pipeline

        assert seen["pipelines"] == [True], (
            "create_key must open exactly one pipeline with transaction=True; "
            f"saw {seen['pipelines']!r}"
        )
        assert seen["hset"] == 0 and seen["zadd"] == 0 and seen["expire"] == 0, (
            "no write may bypass the transaction — a direct call is a window where a "
            f"crash leaves a half-created credential; saw {seen!r}"
        )

        # The outcome still holds: the credential is both valid and enumerable.
        key_id = result["key_id"]
        assert await keys.validate_key(result["api_key"]) is not None
        assert await redis.zscore(keys._KEY_INDEX, key_id) is not None
        assert any(r["key_id"] == key_id for r in await keys.list_keys())

    @pytest.mark.asyncio
    async def test_ttl_lands_in_the_same_transaction(self, redis):
        """A key created with an expiry must have that expiry, not merely a record."""
        result = await keys.create_key("expiring-agent", ["memory:read"], expires_days=1)
        redis_key = f"{keys._KEY_PREFIX}{keys._hash_key(result['api_key'])}"
        assert await redis.ttl(redis_key) > 0, "expiry did not land with the record"

    @pytest.mark.asyncio
    async def test_no_ttl_when_none_requested(self, redis):
        """The conditional EXPIRE must stay conditional — -1 means no TTL."""
        result = await keys.create_key("permanent-agent", ["memory:read"])
        redis_key = f"{keys._KEY_PREFIX}{keys._hash_key(result['api_key'])}"
        assert await redis.ttl(redis_key) == -1
