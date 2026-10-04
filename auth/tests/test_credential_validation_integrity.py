"""A stored key record authenticates only as exactly who it says it is.

Ported in spirit from ae1f973 (agent/member-runtime-authz, never merged) onto
main's identity-v2 auth layer, 2026-10-04. Until then `validate_key_by_hash`:

* filled a missing `member_id` / `workspace_id` with the deployment owner, so
  any record written without attribution (a hand-rolled rescue key, a record
  from before workspaces) silently authenticated as the OWNER;
* never looked at the member at all, so a member whose status is not
  `active` kept authenticating;
* passed the stored `scopes` value through unchecked. A JSON object or string
  turned `scopes_allow`'s `"*" in scopes` into a key lookup or a SUBSTRING
  test, so `'"x*"'` was a wildcard grant; unknown strings rode along too.

Deliberately NOT ported from ae1f973's file: the `auth:cred` reverse-mapping
and `auth:key_index` membership requirements, and `resolve_credential_principal`
(see the commit message — the first two would lock out legacy records that
bootstrap-keys.sh refuses to map when ambiguous, and the third belongs to the
delegated-attribution work).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.workspace import ensure_workspace


HASH = "a" * 64
CREDENTIAL_ID = "0123456789abcdef"
OWNER = "member-owner"


async def _seed(redis, *, scopes=("memory:read",), member_id: str | None = None) -> None:
    await ensure_workspace(redis)
    record = keys.build_credential_record(
        CREDENTIAL_ID,
        "fedcba9876543210",
        list(scopes),
        datetime.now(timezone.utc),
        None,
        member_id=member_id,
    )
    await redis.hset(f"auth:key:{HASH}", mapping=record)
    await redis.set(f"auth:cred:{CREDENTIAL_ID}", HASH)
    await redis.zadd(keys._KEY_INDEX, {CREDENTIAL_ID: 1})


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def _identity(redis):
    return await keys.validate_key_by_hash(HASH, redis_client=redis)


@pytest.mark.asyncio
async def test_a_well_formed_record_still_authenticates(redis):
    await _seed(redis)
    assert await _identity(redis) == {
        "workspace_id": "workspace-local",
        "member_id": OWNER,
        "credential_id": CREDENTIAL_ID,
        "scopes": ["memory:read"],
        "authenticated": True,
    }


# --- (a) attribution is read, never invented ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["workspace_id", "member_id", "credential_id"])
async def test_a_record_missing_its_attribution_is_refused_not_given_to_the_owner(
    redis, missing
):
    await _seed(redis)
    await redis.hdel(f"auth:key:{HASH}", missing)
    assert await _identity(redis) is None


@pytest.mark.asyncio
async def test_a_record_from_another_workspace_is_refused(redis):
    await _seed(redis)
    await redis.hset(f"auth:key:{HASH}", "workspace_id", "workspace-elsewhere")
    assert await _identity(redis) is None


# --- (b) the member must exist and be active ---------------------------------


@pytest.mark.asyncio
async def test_a_member_who_is_not_active_stops_authenticating(redis):
    await _seed(redis)
    await redis.hset(f"auth:member:{OWNER}", "status", "removed")
    assert await _identity(redis) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value",
    [("member_id", "member-someone-else"), ("workspace_id", "workspace-elsewhere")],
)
async def test_a_member_row_that_does_not_describe_this_member_is_refused(
    redis, field, value
):
    await _seed(redis)
    await redis.hset(f"auth:member:{OWNER}", field, value)
    assert await _identity(redis) is None


@pytest.mark.asyncio
async def test_a_credential_for_a_member_with_no_row_is_refused(redis):
    await _seed(redis, member_id="member-ghost")
    assert await _identity(redis) is None


@pytest.mark.asyncio
async def test_a_teammate_credential_authenticates_as_the_teammate(redis):
    await _seed(redis, member_id="member-bob")
    await redis.hset(
        "auth:member:member-bob",
        mapping={"member_id": "member-bob", "workspace_id": "workspace-local",
                 "role": "member", "status": "active"},
    )
    identity = await _identity(redis)
    assert identity is not None and identity["member_id"] == "member-bob"


# --- expiry: unparseable is not "never" --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_at",
    [
        "not-a-date",
        # Naive: comparing it with an aware now() raised TypeError, which was
        # caught and treated as "never expires".
        (datetime.now() + timedelta(days=1)).replace(tzinfo=None).isoformat(),
    ],
)
async def test_an_unreadable_expiry_refuses_instead_of_never_expiring(redis, expires_at):
    await _seed(redis)
    await redis.hset(f"auth:key:{HASH}", "expires_at", expires_at)
    assert await _identity(redis) is None


# --- (c) the stored scope document --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        '{"memory:read": true}',  # `"*" in dict` / `"x" in dict` tests keys
        '"memory:read,*"',        # `"*" in str` is a SUBSTRING test: wildcard
        '["memory:read", 7]',
        "not json",
        "null",
    ],
)
async def test_a_malformed_scope_document_is_refused(redis, stored):
    await _seed(redis)
    await redis.hset(f"auth:key:{HASH}", "scopes", stored)
    assert await _identity(redis) is None


@pytest.mark.asyncio
async def test_a_string_scope_document_no_longer_grants_the_wildcard(redis):
    """The concrete escalation the shape check closes."""
    await _seed(redis)
    await redis.hset(f"auth:key:{HASH}", "scopes", '"x*"')
    identity = await _identity(redis)
    assert identity is None or not keys.scopes_allow(identity["scopes"], "admin")


@pytest.mark.asyncio
async def test_unknown_scope_strings_are_dropped_not_fatal(redis):
    """A pre-2026-07 teammate key still carries the retired `twin:read`.

    Refusing the whole key for it would lock out real credentials on upgrade,
    so unknown strings are dropped from the identity and the rest stand.
    """
    await _seed(redis, scopes=("memory:read", "twin:read", "made:up"))
    identity = await _identity(redis)
    assert identity is not None
    assert identity["scopes"] == ["memory:read"]


@pytest.mark.asyncio
async def test_wildcard_and_admin_keys_keep_their_scopes(redis):
    await _seed(redis, scopes=("*",))
    assert (await _identity(redis))["scopes"] == ["*"]
    await redis.hset(f"auth:key:{HASH}", "scopes", '["admin"]')
    assert (await _identity(redis))["scopes"] == ["admin"]


@pytest.mark.asyncio
async def test_enrolled_ceiling_union_still_applies_after_filtering(redis):
    await _seed(redis, scopes=("memory:read", "twin:read"))
    await redis.hset(f"auth:key:{HASH}", "enrolled_via", "0" * 16)
    identity = await _identity(redis)
    assert "twin:read" not in identity["scopes"]
    assert set(identity["scopes"]) == set(keys.ENROLLABLE_SCOPES)


# --- the 401 body names the reason -------------------------------------------


@pytest.mark.asyncio
async def test_the_refusal_explains_an_unattributed_record(redis, monkeypatch):
    await _seed(redis)
    await redis.hdel(f"auth:key:{HASH}", "member_id")
    monkeypatch.setattr(keys, "_hash_key", lambda _plaintext: HASH)
    detail = await keys.invalid_credential_detail("nxs_x", redis_client=redis)
    assert "no recorded member" in detail
    assert "bootstrap-keys.sh" in detail


@pytest.mark.asyncio
async def test_the_refusal_explains_an_inactive_member(redis, monkeypatch):
    await _seed(redis)
    await redis.hset(f"auth:member:{OWNER}", "status", "removed")
    monkeypatch.setattr(keys, "_hash_key", lambda _plaintext: HASH)
    detail = await keys.invalid_credential_detail("nxs_x", redis_client=redis)
    assert "not active" in detail


@pytest.mark.asyncio
async def test_an_unknown_key_is_still_just_unknown(redis):
    assert await keys.invalid_credential_detail("nxs_nope", redis_client=redis) == (
        "Unknown API key"
    )
