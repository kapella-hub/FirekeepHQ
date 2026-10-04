"""SERVICE_ONLY_SCOPES (D8e): eval:grade is mintable only through bootstrap.

create_key (the admin-facing /auth/keys path) must reject it outright, and
GET /auth/scopes must list it separately from the mintable set — an admin key
can list what exists without being able to mint it.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.api import create_auth_router


@pytest_asyncio.fixture
async def auth_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=r, enabled=True)
    yield r
    await keys.init_auth(redis_client=None, enabled=False)
    await r.aclose()


@pytest.mark.asyncio
async def test_create_key_rejects_service_only_scopes(auth_redis):
    with pytest.raises(ValueError, match="service-only"):
        await keys.create_key(
            agent_id="mallory", scopes=["memory:write", "eval:grade"])


@pytest.mark.asyncio
async def test_scopes_endpoint_separates_service_scopes():
    route = next(
        route for route in create_auth_router().routes
        if route.path == "/auth/scopes" and "GET" in route.methods)
    body = await route.endpoint(identity={"scopes": ["admin"]})
    assert body["service_only"] == [
        "eval:grade", "memory:write:delegated", "relay:write:service",
        "session:read:workspace"]
    assert "eval:grade" not in body["scopes"]
    assert "session:read:workspace" not in body["scopes"]


@pytest.mark.asyncio
async def test_create_key_rejects_workspace_session_read(auth_redis):
    """A member key that could read every teammate's Bridge sessions would undo
    the F3 ownership fix (2026-10-01) — it is bootstrap-only, like eval:grade."""
    with pytest.raises(ValueError, match="service-only"):
        await keys.create_key(
            agent_id="mallory", scopes=["session:read", "session:read:workspace"])


def test_workspace_session_read_is_never_enrollable_or_anonymous():
    assert "session:read:workspace" in keys.SCOPES
    assert "session:read:workspace" not in keys.ENROLLABLE_SCOPES
    assert "session:read:workspace" not in keys.ANONYMOUS_SCOPES


@pytest.mark.asyncio
async def test_create_key_rejects_delegated_memory_write(auth_redis):
    """A member key that could attribute memories to OTHER members would let any
    teammate write as anyone (2026-10-04) -- bootstrap-only, like eval:grade."""
    with pytest.raises(ValueError, match="service-only"):
        await keys.create_key(
            agent_id="mallory", scopes=["memory:write", "memory:write:delegated"])


def test_delegated_memory_write_is_never_enrollable_or_anonymous():
    assert "memory:write:delegated" in keys.SCOPES
    assert "memory:write:delegated" in keys.SERVICE_ONLY_SCOPES
    assert "memory:write:delegated" not in keys.ENROLLABLE_SCOPES
    assert "memory:write:delegated" not in keys.ANONYMOUS_SCOPES


@pytest.mark.asyncio
async def test_create_key_rejects_relay_service_writes(auth_redis):
    """relay:write:service authorizes FIREKEEP_INTERNAL_KEY's alert broadcast
    and fleet enqueue (2026-10-04); a member credential must never carry it."""
    with pytest.raises(ValueError, match="service-only"):
        await keys.create_key(
            agent_id="mallory", scopes=["relay:write", "relay:write:service"])


def test_relay_service_write_is_never_enrollable_or_anonymous():
    assert "relay:write:service" in keys.SCOPES
    assert "relay:write:service" not in keys.ENROLLABLE_SCOPES
    assert "relay:write:service" not in keys.ANONYMOUS_SCOPES
