"""Independent credential IDs must resolve exactly, never as hash prefixes."""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis.aioredis
import pytest

from auth import keys


@pytest.mark.asyncio
async def test_mapping_drives_listing_and_revocation_without_a_scan():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    credential_id = "1234567890abcdef"
    # Deliberately unrelated: the client controls this value during enrollment.
    key_hash = "a" * 64
    record = keys.build_credential_record(
        credential_id,
        "fedcba9876543210",
        ["memory:read"],
        datetime.now(timezone.utc),
        90,
        enrolled_via="ticket",
    )
    try:
        await redis.hset(f"{keys._KEY_PREFIX}{key_hash}", mapping=record)
        await redis.set(f"{keys._CRED_PREFIX}{credential_id}", key_hash)
        await redis.zadd(keys._KEY_INDEX, {credential_id: 1})

        original_scan = redis.scan_iter

        def fail_scan(*args, **kwargs):
            raise AssertionError("mapped credential resolution must not scan")

        redis.scan_iter = fail_scan
        try:
            listed = await keys.list_keys()
            assert listed[0]["credential_id"] == credential_id
            assert await keys.revoke_key(credential_id) is True
        finally:
            redis.scan_iter = original_scan

        assert not await redis.exists(f"{keys._KEY_PREFIX}{key_hash}")
        assert not await redis.exists(f"{keys._CRED_PREFIX}{credential_id}")
        assert await redis.zscore(keys._KEY_INDEX, credential_id) is None
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


@pytest.mark.asyncio
async def test_rename_changes_only_display_metadata():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    credential_id = "1234567890abcdef"
    key_hash = "b" * 64
    record = keys.build_credential_record(
        credential_id,
        "fedcba9876543210",
        ["memory:read"],
        datetime.now(timezone.utc),
        90,
        enrolled_via="ticket",
    )
    try:
        await redis.hset(f"{keys._KEY_PREFIX}{key_hash}", mapping=record)
        await redis.set(f"{keys._CRED_PREFIX}{credential_id}", key_hash)
        await redis.zadd(keys._KEY_INDEX, {credential_id: 1})
        assert await keys.rename_device(credential_id, "Morgan's laptop") is True
        changed = await redis.hgetall(f"{keys._KEY_PREFIX}{key_hash}")
        assert changed["device_label"] == "Morgan's laptop"
        assert changed["scopes"] == record["scopes"]
        assert changed["credential_id"] == credential_id
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()
