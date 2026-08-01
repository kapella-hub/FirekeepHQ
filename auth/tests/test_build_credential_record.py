"""The credential field map is shared by manual keys and enrollment."""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis.aioredis
import pytest

from auth import keys


def test_build_credential_record_is_pure_and_complete():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    record = keys.build_credential_record(
        "0123456789abcdef",
        "fedcba9876543210",
        ["memory:read", "vault:read"],
        now,
        90,
        enrolled_via="abc123",
        device_label="Bob's laptop",
    )

    assert record == {
        "workspace_id": "workspace-local",
        "member_id": "member-owner",
        "device_id": "fedcba9876543210",
        "credential_id": "0123456789abcdef",
        "key_id": "0123456789abcdef",
        "scopes": '["memory:read", "vault:read"]',
        "created_at": "2026-07-31T12:00:00+00:00",
        "expires_at": "2026-10-29T12:00:00+00:00",
        "enrolled_via": "abc123",
        "enrolled_at": "2026-07-31T12:00:00+00:00",
        "device_label": "Bob's laptop",
    }


@pytest.mark.asyncio
async def test_create_key_stores_exactly_the_shared_field_map():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        created = await keys.create_key("manual-agent", ["memory:read"], expires_days=2)
        key_hash = keys._hash_key(created["api_key"])
        stored = await redis.hgetall(f"{keys._KEY_PREFIX}{key_hash}")
        expected = keys.build_credential_record(
            created["credential_id"],
            "manual-agent",
            ["memory:read"],
            datetime.fromisoformat(created["created_at"]),
            2,
        )
        assert stored == expected
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


@pytest.mark.asyncio
async def test_enrollment_provenance_is_listed():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    credential_id = "0123456789abcdef"
    key_hash = "f" * 64
    record = keys.build_credential_record(
        credential_id,
        "fedcba9876543210",
        ["memory:read"],
        datetime(2026, 7, 31, tzinfo=timezone.utc),
        90,
        enrolled_via="ticket123",
        device_label="Bob laptop",
    )
    try:
        await redis.hset(f"{keys._KEY_PREFIX}{key_hash}", mapping=record)
        await redis.set(f"{keys._CRED_PREFIX}{credential_id}", key_hash)
        await redis.zadd(keys._KEY_INDEX, {credential_id: 1})

        assert await keys.list_keys() == [{
            "key_id": credential_id,
            "credential_id": credential_id,
            "device_id": "fedcba9876543210",
            "device_label": "Bob laptop",
            "scopes": ["memory:read"],
            "created_at": "2026-07-31T00:00:00+00:00",
            "expires_at": "2026-10-29T00:00:00+00:00",
            "enrolled_via": "ticket123",
            "ambiguous": False,
        }]
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()
