"""A removed member's existing key gets 401 on Bridge (THREAT-MODEL §5.17).

Bridge validates every request through the shared FirekeepKeyAuthMiddleware
(auth/asgi.py -> auth.keys.validate_key), which reads Redis DB 7 per request
with no cache. This pins that `auth.members.remove_member` takes effect on
Bridge's real app (FastMCP http_app + its production skip paths) immediately:
the same key that listed its sessions a moment before is refused.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from starlette.middleware import Middleware

from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.config import AuthSettings
from auth.members import remove_member
from auth.workspace import MEMBER_INDEX, ensure_workspace

import app.mcp_server as mcp_mod

MEMBER = "member-leaver"


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
async def test_removed_members_key_is_refused_by_bridge(monkeypatch):
    import auth.asgi as asgi_module

    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))
    manager = AsyncMock()
    manager.list_sessions.return_value = []
    monkeypatch.setattr(mcp_mod, "_get_manager", AsyncMock(return_value=manager))

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        workspace = await ensure_workspace(redis)
        api_key = await _member_with_key(redis)
        app = mcp_mod.mcp.http_app(
            middleware=[Middleware(
                FirekeepKeyAuthMiddleware,
                enabled=True,
                redis_url="redis://unused/7",
                skip_paths=("/health", "/version"),  # bridge's production list
                redis_client=redis,
            )],
            stateless_http=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            before = await client.get("/sessions", headers={"X-API-Key": api_key})
            assert before.status_code == 200, before.text

            await remove_member(
                redis, MEMBER,
                workspace_id=workspace.workspace_id,
                owner_member_id=workspace.owner_member_id,
                removed_by="credential:test",
            )

            after = await client.get("/sessions", headers={"X-API-Key": api_key})
            assert after.status_code == 401
    finally:
        await redis.aclose()
