# cortex/tests/test_trust_api.py
"""GET /autopilot/trust — admin-gated, deployment-global, additive.

Deviation from the plan's bare-app scaffold, forced by this repo: admin autopilot
routes are gated by `require_scope("admin")`, and the auth-DISABLED path still
scope-checks the anonymous identity and REFUSES `admin` (auth middleware, audit
blocker 7). `tests/conftest.py` puts the repo root on `sys.path`, so the import in
`create_autopilot_router` succeeds and `admin_dep` is populated — a keyless bare
app therefore returns 403, not 200. So the route is driven under real enforcement
with an admin key, the established pattern in `test_autopilot_api.py`. The plan's
assertions (shape, `window_days == 30`, `agents` is a list) are preserved exactly.
"""
from __future__ import annotations

import fakeredis.aioredis as fr
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.autopilot.api import create_autopilot_router
from auth import keys


@pytest_asyncio.fixture
async def admin_client():
    """Real enforcement, real admin key — module-global auth state restored on
    teardown so it does not leak into the rest of the process."""
    auth_redis = fr.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=auth_redis, enabled=True)
    admin = await keys.create_key("owner", ["admin"])
    r = fr.FakeRedis(decode_responses=True)
    app = FastAPI()
    app.include_router(create_autopilot_router(
        get_redis=lambda: r, get_replay_redis=lambda: r,
        get_vector=lambda: None, settings_fn=lambda: None))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                         headers={"X-API-Key": admin["api_key"]})
    try:
        yield client
    finally:
        await client.aclose()
        await keys.init_auth(redis_client=None, enabled=False)
        await auth_redis.aclose()


@pytest.mark.asyncio
async def test_trust_endpoint_shape(admin_client):
    resp = await admin_client.get("/autopilot/trust")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"agents", "window_days", "scanned",
                         "truncated", "invalid", "generated_at"}
    assert body["window_days"] == 30
    assert isinstance(body["agents"], list)
