"""Verified principal contract for credentials and auth-disabled deployments."""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis.aioredis
import pytest

from auth import keys
from auth.principal import principal_from_scope


@pytest.mark.asyncio
async def test_credential_resolves_to_workspace_member_credential_and_no_agent():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    secret = "nxs_" + "a" * 64
    key_hash = keys._hash_key(secret)
    record = keys.build_credential_record(
        "0123456789abcdef",
        "fedcba9876543210",
        ["memory:read"],
        datetime(2026, 7, 31, tzinfo=timezone.utc),
        None,
        workspace_id="workspace-test",
        member_id="member-alice",
    )
    try:
        await redis.hset(f"auth:key:{key_hash}", mapping=record)
        identity = await keys.validate_key(secret, redis_client=redis)
        assert identity == {
            "workspace_id": "workspace-test",
            "member_id": "member-alice",
            "credential_id": "0123456789abcdef",
            "scopes": ["memory:read"],
            "authenticated": True,
        }
        assert await keys.validate_key("nxs_unknown", redis_client=redis) is None
    finally:
        await redis.aclose()


def test_x_agent_id_cannot_influence_scope_principal(monkeypatch):
    from auth import config
    from auth.config import AuthSettings

    old = config._settings
    config._settings = AuthSettings(ENABLED=False)
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-test")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-test")
    try:
        principal = principal_from_scope({
            "state": {},
            "headers": [(b"x-agent-id", b"someone-else")],
        })
    finally:
        config._settings = old

    assert principal["workspace_id"] == "workspace-test"
    assert principal["member_id"] == "member-owner-test"
    assert principal["credential_id"] == "anonymous"
    assert "agent_id" not in principal
