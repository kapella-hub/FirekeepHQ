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
from auth.workspace import ensure_workspace


pytestmark = pytest.mark.skipif(
    not os.environ.get("FIREKEEP_TEST_REDIS_URL"),
    reason="set FIREKEEP_TEST_REDIS_URL to an isolated disposable Redis DB",
)


@pytest.mark.asyncio
async def test_atomic_consume_lifecycle_against_real_redis():
    redis = aioredis.from_url(os.environ["FIREKEEP_TEST_REDIS_URL"], decode_responses=True)
    await redis.flushdb()
    workspace = await ensure_workspace(redis)
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
            host="127.0.0.1", ssh_target="root@server",
            member_id=workspace.owner_member_id, now=now,
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
            host="127.0.0.1", ssh_target="root@server",
            member_id=workspace.owner_member_id, now=now - timedelta(days=2),
        )
        expired = await store.consume(
            ticket=expired_ticket, credential_hash="e" * 64,
            device_nonce="f" * 16, now=now,
        )
        assert expired[0] == "expired"

        scoped_ticket, scoped_tid, _ = await store.issue(
            agent_label="bad", transport="tunnel", kind="ports",
            host="127.0.0.1", ssh_target="root@server",
            member_id=workspace.owner_member_id, now=now,
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
            host="127.0.0.1", ssh_target="root@server",
            member_id=workspace.owner_member_id, now=now,
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


@pytest.mark.asyncio
async def test_regenerated_teammate_device_redeems_as_the_teammate(monkeypatch):
    """Dashboard Regenerate -> join code -> redemption, through the real script.

    The credential a regenerated teammate device receives must authenticate as
    that teammate. Before 2026-10-04 it authenticated as the deployment owner.
    """
    import httpx
    from fastapi import FastAPI

    from app.enroll.api import create_enroll_router
    from auth import keys

    redis = aioredis.from_url(os.environ["FIREKEEP_TEST_REDIS_URL"], decode_responses=True)
    await redis.flushdb()
    workspace = await ensure_workspace(redis)
    await keys.init_auth(redis_client=redis, enabled=True)
    store = EnrollmentStore(redis)
    try:
        admin = await keys.create_key("dashboard", ["*"])
        await redis.hset(
            "auth:member:member-bob",
            mapping={"member_id": "member-bob", "workspace_id": workspace.workspace_id,
                     "role": "member", "status": "active"},
        )
        bob_device = "b" * 16
        bob_old = await keys.create_key(bob_device, ["memory:read"])
        old_hash = await redis.get(f"auth:cred:{bob_old['credential_id']}")
        await redis.hset(f"auth:key:{old_hash}", "member_id", "member-bob")

        app = FastAPI()
        app.include_router(create_enroll_router(store=store, auth_enabled=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            invite = await http.post(
                "/enroll/invite",
                headers={"X-API-Key": admin["api_key"]},
                json={"device_id": bob_device, "transport": "tunnel",
                      "ssh_target": "root@server"},
            )
        assert invite.status_code == 200, invite.text
        # The ticket secret travels inside the join code as field "q".
        import base64
        import json

        body = invite.json()["code"].removeprefix("fk_join_").split(".", 1)[0]
        ticket = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["q"]
        new_secret = "nxs_" + "9" * 64
        new_hash = hashlib.sha256(new_secret.encode()).hexdigest()
        outcome, fields, _ = await store.consume(
            ticket=ticket, credential_hash=new_hash, device_nonce="9" * 16,
        )
        assert outcome == "ok", (outcome, fields)
        identity = await keys.validate_key(new_secret)
        assert identity is not None
        assert identity["member_id"] == "member-bob"
        assert identity["member_id"] != workspace.owner_member_id
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
async def test_redemption_refuses_inactive_members_and_unattributed_tickets():
    redis = aioredis.from_url(os.environ["FIREKEEP_TEST_REDIS_URL"], decode_responses=True)
    await redis.flushdb()
    workspace = await ensure_workspace(redis)
    store = EnrollmentStore(redis)
    now = datetime.now(timezone.utc)
    try:
        await redis.hset(
            "auth:member:member-bob",
            mapping={"member_id": "member-bob", "workspace_id": workspace.workspace_id,
                     "role": "member", "status": "removed"},
        )
        inactive_ticket, _, _ = await store.issue(
            transport="tunnel", kind="ports", host="127.0.0.1",
            ssh_target="root@server", member_id="member-bob", now=now,
        )
        inactive = await store.consume(
            ticket=inactive_ticket, credential_hash="7" * 64,
            device_nonce="8" * 16, now=now + timedelta(seconds=1),
        )
        assert inactive[0] == "member_inactive"
        assert not await redis.exists("auth:key:" + "7" * 64)

        # A ticket minted by a pre-2026-10-04 server carries no member at all.
        legacy_ticket, legacy_tid, _ = await store.issue(
            transport="tunnel", kind="ports", host="127.0.0.1",
            ssh_target="root@server", member_id=workspace.owner_member_id, now=now,
        )
        await redis.hdel(f"auth:enroll:{legacy_tid}", "member_id")
        legacy = await store.consume(
            ticket=legacy_ticket, credential_hash="a" * 64,
            device_nonce="b" * 16, now=now + timedelta(seconds=1),
        )
        assert legacy[0] == "unattributed"
        assert not await redis.exists("auth:key:" + "a" * 64)
        assert not await redis.hget(f"auth:enroll:{legacy_tid}", "used_at")
    finally:
        await redis.flushdb()
        await redis.aclose()
