"""Removing a member revokes them everywhere; the owner can never be removed.

THREAT-MODEL §5.17 (2026-10-05). There was no removal path at all: since
2026-10-04 `validate_key` refuses a credential whose member row is not
`status: active`, but nothing ever wrote any other status, so the only lever
was revoking a person's devices one at a time while any join code still
outstanding for them could mint a new one.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.members import MemberRemovalError, remove_member, restore_member
from auth.workspace import MEMBER_INDEX, ensure_workspace


WORKSPACE = "workspace-local"
OWNER = "member-owner"
ALICE = "member-alice"
BOB = "member-bob"


async def seed_member(redis, member_id: str, *, role: str = "member") -> None:
    await redis.hset(
        f"auth:member:{member_id}",
        mapping={
            "member_id": member_id,
            "workspace_id": WORKSPACE,
            "role": role,
            "status": "active",
            "created_at": "2026-10-01T00:00:00+00:00",
        },
    )
    await redis.zadd(MEMBER_INDEX, {member_id: 1})


async def mint(redis, member_id: str, scopes=None, *, indexed: bool = True) -> tuple[str, str]:
    """A credential for `member_id`, written the way enrollment writes one."""
    api_key = keys.generate_api_key()
    key_hash = keys._hash_key(api_key)
    credential_id = secrets.token_hex(8)
    record = keys.build_credential_record(
        credential_id,
        secrets.token_hex(8),
        sorted(scopes or keys.ENROLLABLE_SCOPES),
        datetime.now(timezone.utc),
        None,
        enrolled_via="0123456789abcdef",
        member_id=member_id,
    )
    await redis.hset(f"auth:key:{key_hash}", mapping=record)
    if indexed:
        await redis.set(f"auth:cred:{credential_id}", key_hash)
        await redis.zadd("auth:key_index", {credential_id: 1})
    return api_key, credential_id


async def remove(redis, member_id: str, **kwargs):
    return await remove_member(
        redis,
        member_id,
        workspace_id=WORKSPACE,
        owner_member_id=OWNER,
        removed_by="credential:test",
        **kwargs,
    )


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_workspace(client)
    await seed_member(client, ALICE)
    await seed_member(client, BOB)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_removed_members_existing_keys_stop_authenticating(redis):
    key, cred = await mint(redis, ALICE)
    unindexed_key, unindexed_cred = await mint(redis, ALICE, indexed=False)
    bob_key, _ = await mint(redis, BOB)
    assert await keys.validate_key(key, redis) is not None

    result = await remove(redis, ALICE)

    assert await keys.validate_key(key, redis) is None
    assert await keys.validate_key(unindexed_key, redis) is None
    # Every record naming her is gone, not merely refused: mapping and index too.
    assert await redis.exists(f"auth:key:{keys._hash_key(key)}") == 0
    assert await redis.exists(f"auth:key:{keys._hash_key(unindexed_key)}") == 0
    assert await redis.get(f"auth:cred:{cred}") is None
    assert await redis.zscore("auth:key_index", cred) is None
    assert set(result["credentials_revoked"]) == {cred, unindexed_cred}
    # Nobody else is touched.
    assert await keys.validate_key(bob_key, redis) is not None


@pytest.mark.asyncio
async def test_member_row_is_kept_for_attribution_and_marked_removed(redis):
    await remove(redis, ALICE)
    row = await redis.hgetall(f"auth:member:{ALICE}")
    assert row["status"] == "removed"
    assert row["member_id"] == ALICE and row["workspace_id"] == WORKSPACE
    assert row["removed_by"] == "credential:test" and row["removed_at"]
    assert await redis.zscore(MEMBER_INDEX, ALICE) is not None


@pytest.mark.asyncio
async def test_deployment_owner_can_never_be_removed(redis):
    owner_key, _ = await mint(redis, OWNER, ["*"])
    with pytest.raises(MemberRemovalError) as exc:
        await remove(redis, OWNER)
    assert exc.value.status == 409
    assert "owner" in exc.value.detail
    assert (await redis.hgetall(f"auth:member:{OWNER}"))["status"] == "active"
    assert await keys.validate_key(owner_key, redis) is not None


@pytest.mark.asyncio
async def test_a_member_with_the_owner_role_is_refused_too(redis):
    await seed_member(redis, "member-co-owner", role="owner")
    with pytest.raises(MemberRemovalError) as exc:
        await remove(redis, "member-co-owner")
    assert exc.value.status == 409


@pytest.mark.asyncio
async def test_a_caller_cannot_remove_the_member_it_acts_as(redis):
    with pytest.raises(MemberRemovalError) as exc:
        await remove(redis, ALICE, actor_member_id=ALICE)
    assert exc.value.status == 409
    assert (await redis.hgetall(f"auth:member:{ALICE}"))["status"] == "active"


@pytest.mark.asyncio
async def test_removing_the_last_admin_holder_is_refused(redis):
    # Unreachable on a healthy store (the owner row is always active and is an
    # admin holder); reproduced by corrupting it, to pin the guard.
    await redis.hset(f"auth:member:{OWNER}", "status", "suspended")
    alice_admin, _ = await mint(redis, ALICE, ["*"])
    with pytest.raises(MemberRemovalError) as exc:
        await remove(redis, ALICE)
    assert exc.value.status == 409
    assert "no active admin" in exc.value.detail
    assert await keys.validate_key(alice_admin, redis) is not None


@pytest.mark.asyncio
async def test_unknown_malformed_and_foreign_members_are_one_404(redis):
    await redis.hset(
        "auth:member:member-elsewhere",
        mapping={"member_id": "member-elsewhere", "workspace_id": "other", "status": "active"},
    )
    for member_id in ("member-nobody", "member-elsewhere", "bad id*", ""):
        with pytest.raises(MemberRemovalError) as exc:
            await remove(redis, member_id)
        assert exc.value.status == 404
        assert "not found in this workspace" in exc.value.detail


@pytest.mark.asyncio
async def test_outstanding_join_codes_are_cancelled_redeemed_ones_kept(redis):
    await redis.hset("auth:enroll:aaaaaaaaaaaaaaaa", mapping={"member_id": ALICE, "kind": "ports"})
    await redis.zadd("auth:enroll:index", {"aaaaaaaaaaaaaaaa": 1})
    await redis.hset(
        "auth:enroll:bbbbbbbbbbbbbbbb",
        mapping={"member_id": ALICE, "used_at": "2026-10-01T00:00:00+00:00"},
    )
    await redis.hset("auth:enroll:cccccccccccccccc", mapping={"member_id": BOB})
    await redis.zadd("auth:enroll:index", {"cccccccccccccccc": 2})
    # The hourly rate counter shares the prefix and is a string: never touched.
    await redis.set("auth:enroll:rate:2026100500", "3")
    await redis.hset(
        "auth:member_invite:dddddddddddddddd",
        mapping={"member_id": ALICE, "status": "accepted", "used_at": "x"},
    )
    await redis.zadd("auth:member_invite_index", {"dddddddddddddddd": 1})

    result = await remove(redis, ALICE)

    assert result["join_codes_cancelled"] == ["aaaaaaaaaaaaaaaa"]
    assert await redis.exists("auth:enroll:aaaaaaaaaaaaaaaa") == 0
    assert await redis.zscore("auth:enroll:index", "aaaaaaaaaaaaaaaa") is None
    assert await redis.exists("auth:enroll:bbbbbbbbbbbbbbbb") == 1
    assert await redis.exists("auth:enroll:cccccccccccccccc") == 1
    assert await redis.get("auth:enroll:rate:2026100500") == "3"
    assert result["member_invites_revoked"] == ["dddddddddddddddd"]
    assert await redis.hget("auth:member_invite:dddddddddddddddd", "status") == "member_removed"
    assert await redis.zscore("auth:member_invite_index", "dddddddddddddddd") is None


@pytest.mark.asyncio
async def test_removal_is_idempotent_and_a_rerun_finishes_an_interrupted_sweep(redis):
    first = await remove(redis, ALICE)
    assert first["already_removed"] is False
    # A crash after the status flip leaves a refused-but-present record.
    straggler, straggler_cred = await mint(redis, ALICE)
    assert await keys.validate_key(straggler, redis) is None

    second = await remove(redis, ALICE)

    assert second["already_removed"] is True
    assert second["credentials_revoked"] == [straggler_cred]
    assert await redis.exists(f"auth:key:{keys._hash_key(straggler)}") == 0
    assert (await redis.hgetall(f"auth:member:{ALICE}"))["removed_by"] == "credential:test"


@pytest.mark.asyncio
async def test_restore_reactivates_the_same_member_without_issuing_a_credential(redis):
    old_key, _ = await mint(redis, ALICE)
    await remove(redis, ALICE)

    row = await restore_member(redis, ALICE, workspace_id=WORKSPACE, restored_by="credential:test")

    assert row["status"] == "active"
    assert row["member_id"] == ALICE
    assert "removed_at" not in row and row["last_removed_at"]
    # The removed credentials stay gone: a restored member needs a new join code.
    assert await keys.validate_key(old_key, redis) is None
    fresh, _ = await mint(redis, ALICE)
    assert (await keys.validate_key(fresh, redis))["member_id"] == ALICE
    # Restoring an active member is a no-op.
    again = await restore_member(redis, ALICE, workspace_id=WORKSPACE, restored_by="x")
    assert again["status"] == "active" and again["restored_by"] == "credential:test"


@pytest.mark.asyncio
async def test_restore_refuses_unknown_members(redis):
    with pytest.raises(MemberRemovalError) as exc:
        await restore_member(redis, "member-nobody", workspace_id=WORKSPACE, restored_by="x")
    assert exc.value.status == 404
