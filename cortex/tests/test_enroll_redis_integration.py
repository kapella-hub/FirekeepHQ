"""Real-Redis verification for the atomic ENROLL_CONSUME script.

Skipped in ordinary unit runs. CI or a release gate supplies an isolated DB via
FIREKEEP_TEST_REDIS_URL; the test flushes that DB.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis

from app.enroll.store import EnrollmentSettings, EnrollmentStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("FIREKEEP_TEST_REDIS_URL"),
    reason="set FIREKEEP_TEST_REDIS_URL to an isolated disposable Redis DB",
)


@pytest.mark.asyncio
async def test_atomic_consume_lifecycle_against_real_redis():
    redis = aioredis.from_url(os.environ["FIREKEEP_TEST_REDIS_URL"], decode_responses=True)
    await redis.flushdb()
    store = EnrollmentStore(
        redis,
        EnrollmentSettings(
            ticket_ttl_hours=24,
            tombstone_days=7,
            key_expires_days=90,
            max_attempts_per_hour=20,
        ),
    )
    now = datetime.now(timezone.utc)
    try:
        ticket, tid, _ = await store.issue(
            agent_label="bob", transport="tunnel", kind="ports",
            host="127.0.0.1", ssh_target="root@server", now=now,
        )
        secret = "nxs_" + "a" * 64
        credential_hash = hashlib.sha256(secret.encode()).hexdigest()
        first = await store.consume(
            ticket=ticket, credential_hash=credential_hash,
            device_nonce="b" * 16, now=now + timedelta(seconds=1),
        )
        assert first[0] == "ok"
        credential_id, device_id = first[1]
        assert await redis.get(f"auth:cred:{credential_id}") == credential_hash
        assert await redis.ttl(f"auth:cred:{credential_id}") > 0
        assert await redis.ttl(f"auth:key:{credential_hash}") > 0
        assert await redis.hget(f"auth:key:{credential_hash}", "device_label") == "bob"

        replay = await store.consume(
            ticket=ticket, credential_hash=credential_hash,
            device_nonce="b" * 16, now=now + timedelta(seconds=2),
        )
        assert replay[0] == "replay"
        assert replay[1][:2] == [credential_id, device_id]

        used = await store.consume(
            ticket=ticket, credential_hash="c" * 64,
            device_nonce="d" * 16, now=now + timedelta(seconds=3),
        )
        assert used[0] == "used"

        await redis.delete(f"auth:key:{credential_hash}")
        gone = await store.consume(
            ticket=ticket, credential_hash=credential_hash,
            device_nonce="b" * 16, now=now + timedelta(seconds=4),
        )
        assert gone[0] == "credential_gone"

        expired_ticket, _, _ = await store.issue(
            agent_label="old", transport="tunnel", kind="ports",
            host="127.0.0.1", ssh_target="root@server", now=now - timedelta(days=2),
        )
        expired = await store.consume(
            ticket=expired_ticket, credential_hash="e" * 64,
            device_nonce="f" * 16, now=now,
        )
        assert expired[0] == "expired"

        scoped_ticket, scoped_tid, _ = await store.issue(
            agent_label="bad", transport="tunnel", kind="ports",
            host="127.0.0.1", ssh_target="root@server", now=now,
        )
        await redis.hset(f"auth:enroll:{scoped_tid}", mapping={"scopes": '["admin"]'})
        scoped = await store.consume(
            ticket=scoped_ticket, credential_hash="1" * 64,
            device_nonce="2" * 16, now=now + timedelta(seconds=1),
        )
        assert scoped[0] == "scope_violation"
        assert not await redis.exists("auth:key:" + "1" * 64)

        object_scopes_ticket, object_scopes_tid, _ = await store.issue(
            agent_label="bad-shape", transport="tunnel", kind="ports",
            host="127.0.0.1", ssh_target="root@server", now=now,
        )
        await redis.hset(
            f"auth:enroll:{object_scopes_tid}", mapping={"scopes": '{"admin":true}'},
        )
        object_scopes = await store.consume(
            ticket=object_scopes_ticket, credential_hash="5" * 64,
            device_nonce="6" * 16, now=now + timedelta(seconds=1),
        )
        assert object_scopes[0] == "scope_violation"
        assert not await redis.exists("auth:key:" + "5" * 64)

        unknown_raw = bytes(reversed(range(32)))
        import base64
        unknown_ticket = base64.urlsafe_b64encode(unknown_raw).decode().rstrip("=")
        unknown = await store.consume(
            ticket=unknown_ticket, credential_hash="3" * 64,
            device_nonce="4" * 16, now=now + timedelta(seconds=1),
        )
        assert unknown[0] == "unknown"
        unknown_tid = hashlib.sha256(unknown_raw).hexdigest()[:16]
        assert not await redis.exists(f"auth:enroll:{unknown_tid}")
        assert not await redis.exists("auth:key:" + "3" * 64)
    finally:
        await redis.flushdb()
        await redis.aclose()
