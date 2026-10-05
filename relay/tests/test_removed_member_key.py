"""A removed member's existing key gets 401 on Relay (THREAT-MODEL §5.17).

relay/tests/conftest.py stubs fastmcp, so -- as in test_mcp_auth.py -- this
mounts an endpoint on a Starlette app behind the REAL FirekeepKeyAuthMiddleware
with Relay's production skip paths. The middleware validates per request
against Redis DB 7 with no cache, so `auth.members.remove_member` takes
effect on the very next request.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import fakeredis.aioredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.members import remove_member
from auth.workspace import MEMBER_INDEX, ensure_workspace

RELAY_SKIP_PATHS = ("/health", "/.well-known/agent.json")
MEMBER = "member-leaver"


async def _whoami(request):
    return JSONResponse({"member_id": request.scope["state"]["identity"]["member_id"]})


async def _member_with_key(redis) -> str:
    await redis.hset(
        f"auth:member:{MEMBER}",
        mapping={"member_id": MEMBER, "workspace_id": "workspace-local",
                 "role": "member", "status": "active"},
    )
    await redis.zadd(MEMBER_INDEX, {MEMBER: 1})
    api_key = keys.generate_api_key()
    credential_id = secrets.token_hex(8)
    record = keys.build_credential_record(
        credential_id, secrets.token_hex(8), sorted(keys.ENROLLABLE_SCOPES),
        datetime.now(timezone.utc), None, enrolled_via="0123456789abcdef",
        member_id=MEMBER,
    )
    await redis.hset(f"auth:key:{keys._hash_key(api_key)}", mapping=record)
    await redis.set(f"auth:cred:{credential_id}", keys._hash_key(api_key))
    await redis.zadd("auth:key_index", {credential_id: 1})
    return api_key


@pytest.mark.asyncio
async def test_removed_members_key_is_refused_by_relay():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        workspace = await ensure_workspace(redis)
        api_key = await _member_with_key(redis)
        app = Starlette(
            routes=[Route("/mcp", _whoami, methods=["POST"])],
            middleware=[Middleware(
                FirekeepKeyAuthMiddleware,
                enabled=True,
                redis_url="redis://unused/7",
                skip_paths=RELAY_SKIP_PATHS,
                redis_client=redis,
            )],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            before = await client.post("/mcp", headers={"X-API-Key": api_key}, json={})
            assert before.status_code == 200 and before.json()["member_id"] == MEMBER

            await remove_member(
                redis, MEMBER,
                workspace_id=workspace.workspace_id,
                owner_member_id=workspace.owner_member_id,
                removed_by="credential:test",
            )

            after = await client.post("/mcp", headers={"X-API-Key": api_key}, json={})
            assert after.status_code == 401
    finally:
        await redis.aclose()
