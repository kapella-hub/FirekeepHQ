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
    assert body["service_only"] == ["eval:grade"]
    assert "eval:grade" not in body["scopes"]
