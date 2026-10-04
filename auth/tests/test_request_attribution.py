"""Write provenance: verified actor fields plus a display label (2026-10-04).

``X-Agent-Id`` is chosen by the client, so it may label a write but never
decide whose write it is. The verified principal decides the member and the
credential; the label only names the runtime, and the runtime's correlation id
is namespaced by the verified credential so two members both calling
themselves ``claude`` never collapse into one actor.

The delegated contract lets ONE service (Bridge's background distiller) write
on behalf of the member whose session it distils. The service key authorizes
the write; the member it names must be an active member of the service's own
workspace, and a credential it names must resolve to that same member.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.principal import (
    DELEGATED_CREDENTIAL_HEADER,
    DELEGATED_MEMBER_HEADER,
    DelegatedAttributionError,
    delegated_attribution,
    has_delegated_attribution_headers,
    request_attribution,
    runtime_id_for,
)

WORKSPACE = "workspace-local"
SERVICE = {
    "workspace_id": WORKSPACE,
    "member_id": "member-owner",
    "credential_id": "b0b0b0b0b0b0b0b0",
    "scopes": ["memory:write", "memory:write:delegated"],
    "authenticated": True,
}
ALICE_CRED = "a11ce0000000a11c"
ALICE_SECRET = "nxs_" + "1" * 48


def _request(headers: dict[str, str], identity: dict | None = None):
    state = {"identity": identity} if identity is not None else {}
    lowered = {k.lower(): v for k, v in headers.items()}
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda name, default=None: lowered.get(name.lower(), default)),
        scope={"state": state},
    )


@pytest_asyncio.fixture
async def auth_redis(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.hset("auth:member:member-owner", mapping={
        "member_id": "member-owner", "workspace_id": WORKSPACE,
        "role": "owner", "status": "active"})
    await redis.hset("auth:member:member-alice", mapping={
        "member_id": "member-alice", "workspace_id": WORKSPACE,
        "role": "member", "status": "active"})
    key_hash = keys._hash_key(ALICE_SECRET)
    await redis.hset(f"auth:key:{key_hash}", mapping=keys.build_credential_record(
        ALICE_CRED, "dev-alice", ["memory:write"],
        datetime(2026, 10, 1, tzinfo=timezone.utc), None,
        workspace_id=WORKSPACE, member_id="member-alice"))
    await redis.set(f"auth:cred:{ALICE_CRED}", key_hash)
    try:
        yield redis
    finally:
        await redis.aclose()


def test_runtime_id_is_namespaced_by_the_verified_credential():
    assert runtime_id_for("c1", "claude") == runtime_id_for("c1", "claude")
    assert runtime_id_for("c1", "claude") != runtime_id_for("c2", "claude")
    assert runtime_id_for("c1", "claude") != runtime_id_for("c1", "codex")
    assert runtime_id_for("c1", "claude").startswith("runtime-")
    # No verified credential, no correlation id -- never a label-only id that
    # two members sharing a label would collide on.
    assert runtime_id_for("", "claude") == ""


def test_request_attribution_takes_actor_from_principal_and_label_from_header():
    identity = {
        "workspace_id": WORKSPACE, "member_id": "member-alice",
        "credential_id": ALICE_CRED, "scopes": ["memory:write"],
        "authenticated": True,
    }
    attr = request_attribution(_request(
        {"X-Agent-Id": "member-owner", "X-Session-Id": "s-1"}, identity))
    # A label that LOOKS like another member changes nothing about who wrote it.
    assert attr["member_id"] == "member-alice"
    assert attr["credential_id"] == ALICE_CRED
    assert attr["runtime_label"] == "member-owner"
    assert attr["session_id"] == "s-1"
    assert attr["runtime_id"] == runtime_id_for(ALICE_CRED, "member-owner")
    assert attr["delegated_by_credential_id"] is None


def test_request_attribution_defaults_absent_labels_to_unknown():
    identity = {**SERVICE}
    attr = request_attribution(_request({}, identity))
    assert attr["runtime_label"] == "unknown"
    assert attr["session_id"] == "unknown"


def test_delegation_headers_are_detected():
    assert has_delegated_attribution_headers(_request({DELEGATED_MEMBER_HEADER: "m"}))
    assert has_delegated_attribution_headers(_request({DELEGATED_CREDENTIAL_HEADER: "c"}))
    assert not has_delegated_attribution_headers(_request({"X-Agent-Id": "x"}))


@pytest.mark.asyncio
async def test_delegated_attribution_records_the_member_and_its_credential(auth_redis):
    attr = await delegated_attribution(
        _request({DELEGATED_MEMBER_HEADER: "member-alice",
                  DELEGATED_CREDENTIAL_HEADER: ALICE_CRED,
                  "X-Agent-Id": "codex", "X-Session-Id": "s-9"}),
        SERVICE, redis_client=auth_redis)
    assert attr["workspace_id"] == WORKSPACE
    assert attr["member_id"] == "member-alice"
    assert attr["credential_id"] == ALICE_CRED
    assert attr["runtime_id"] == runtime_id_for(ALICE_CRED, "codex")
    assert attr["runtime_label"] == "codex"
    assert attr["session_id"] == "s-9"
    assert attr["delegated_by_credential_id"] == SERVICE["credential_id"]


@pytest.mark.asyncio
async def test_delegated_attribution_without_credential_is_member_only(auth_redis):
    attr = await delegated_attribution(
        _request({DELEGATED_MEMBER_HEADER: "member-alice"}), SERVICE,
        redis_client=auth_redis)
    assert attr["member_id"] == "member-alice"
    assert attr["credential_id"] == ""
    assert attr["runtime_id"] == ""


@pytest.mark.asyncio
async def test_revoked_credential_is_dropped_not_trusted(auth_redis):
    """A device key revoked between session start and distillation must not
    strand the member's distillate -- the member is still verified -- but the
    write must not carry a credential nobody can verify any more."""
    attr = await delegated_attribution(
        _request({DELEGATED_MEMBER_HEADER: "member-alice",
                  DELEGATED_CREDENTIAL_HEADER: "dead0000dead0000"}),
        SERVICE, redis_client=auth_redis)
    assert attr["member_id"] == "member-alice"
    assert attr["credential_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {},                                                         # no member named
    {DELEGATED_MEMBER_HEADER: "member-mallory"},                # not a member
    {DELEGATED_MEMBER_HEADER: "bad id!"},                       # malformed
    {DELEGATED_MEMBER_HEADER: "member-alice",
     DELEGATED_CREDENTIAL_HEADER: "not-hex"},                   # malformed cred
    {DELEGATED_MEMBER_HEADER: "member-owner",
     DELEGATED_CREDENTIAL_HEADER: ALICE_CRED},                  # cred is Alice's
])
async def test_unverifiable_delegation_is_refused(auth_redis, headers):
    with pytest.raises(DelegatedAttributionError):
        await delegated_attribution(_request(headers), SERVICE, redis_client=auth_redis)


@pytest.mark.asyncio
async def test_member_of_another_workspace_is_refused(auth_redis):
    await auth_redis.hset("auth:member:member-eve", mapping={
        "member_id": "member-eve", "workspace_id": "workspace-other",
        "status": "active"})
    with pytest.raises(DelegatedAttributionError):
        await delegated_attribution(
            _request({DELEGATED_MEMBER_HEADER: "member-eve"}), SERVICE,
            redis_client=auth_redis)


@pytest.mark.asyncio
async def test_inactive_member_is_refused(auth_redis):
    await auth_redis.hset("auth:member:member-alice", "status", "removed")
    with pytest.raises(DelegatedAttributionError):
        await delegated_attribution(
            _request({DELEGATED_MEMBER_HEADER: "member-alice"}), SERVICE,
            redis_client=auth_redis)


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["memory:write"], ["*"], ["admin"]])
async def test_delegation_needs_the_literal_service_scope(auth_redis, scopes):
    """The dashboard key is ``*`` and nginx injects it into every browser
    request: a wildcard must not be able to name another member as author."""
    with pytest.raises(DelegatedAttributionError):
        await delegated_attribution(
            _request({DELEGATED_MEMBER_HEADER: "member-alice"}),
            {**SERVICE, "scopes": scopes}, redis_client=auth_redis)


@pytest.mark.asyncio
async def test_no_auth_store_fails_closed():
    with pytest.raises(DelegatedAttributionError):
        await delegated_attribution(
            _request({DELEGATED_MEMBER_HEADER: "member-alice"}), SERVICE,
            redis_client=None)
