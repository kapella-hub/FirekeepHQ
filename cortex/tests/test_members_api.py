"""Customer-facing workspace, licence, and member-invite API behavior."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from app.members.api import create_members_router
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.entitlements import sign_licence
from auth.keys import create_key, init_auth
from auth.workspace import ensure_workspace


def _licence(workspace_id: str):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    public_text = base64.urlsafe_b64encode(public).decode().rstrip("=")
    now = datetime.now(timezone.utc)
    document = sign_licence(
        {
            "workspace_id": workspace_id,
            "customer": "Acme",
            "plan": "team",
            "max_members": 2,
            "issued_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=365)).isoformat(),
        },
        private,
    )
    return public_text, document


def _member_ticket(code: str) -> str:
    body = code.removeprefix("fk_member_").split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return payload["m"]


async def _app(redis, workspace):
    inner = FastAPI()
    inner.include_router(create_members_router(redis_client=redis, workspace=workspace))
    wrapped = FirekeepKeyAuthMiddleware(
        inner,
        enabled=True,
        redis_url="redis://unused",
        skip_exact_paths=("/members/invites/accept", "/members/invites/anchor"),
        redis_client=redis,
    )
    return wrapped


@pytest.mark.asyncio
async def test_team_licence_member_invite_and_public_accept(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-api")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-api")
    monkeypatch.delenv("FIREKEEP_LICENCE", raising=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_auth(redis_client=redis, enabled=True)
    try:
        workspace = await ensure_workspace(redis)
        admin = await create_key("owner-device", ["*"])
        public, document = _licence(workspace.workspace_id)
        monkeypatch.setenv("FIREKEEP_LICENCE_PUBLIC_KEY", public)
        app = await _app(redis, workspace)
        headers = {"X-API-Key": admin["api_key"]}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            applied = await client.post(
                "/licence", json={"document": document}, headers=headers
            )
            assert applied.status_code == 200, applied.text
            assert applied.json()["plan"] == "team"

            issued = await client.post(
                "/members/invites",
                headers=headers,
                json={
                    "label": "Ada",
                    "email": "ada@example.com",
                    "transport": "tunnel",
                    "kind": "ports",
                    "host": "127.0.0.1",
                    "ssh_target": "ada@example",
                },
            )
            assert issued.status_code == 200, issued.text
            code = issued.json()["code"]
            assert code.startswith("fk_member_")

            # Acceptance is invite-authenticated and deliberately has no API key.
            accepted = await client.post(
                "/members/invites/accept",
                json={"ticket": _member_ticket(code)},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["membership"]["email"] == "ada@example.com"
            assert accepted.json()["join_code"].startswith("fk_join_")

            members = await client.get("/members", headers=headers)
            assert members.status_code == 200
            assert members.json()["active_count"] == 2
            assert members.json()["outstanding_invite_count"] == 0
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()

@pytest.mark.asyncio
async def test_solo_issue_refusal_is_actionable_and_admin_routes_stay_protected(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-api-solo")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-api-solo")
    monkeypatch.delenv("FIREKEEP_LICENCE", raising=False)
    monkeypatch.delenv("FIREKEEP_LICENCE_PUBLIC_KEY", raising=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_auth(redis_client=redis, enabled=True)
    try:
        workspace = await ensure_workspace(redis)
        admin = await create_key("owner-device", ["*"])
        app = await _app(redis, workspace)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthenticated = await client.post(
                "/members/invites",
                json={"label": "Ada", "ssh_target": "ada@example"},
            )
            assert unauthenticated.status_code == 401

            refused = await client.post(
                "/members/invites",
                headers={"X-API-Key": admin["api_key"]},
                json={"label": "Ada", "ssh_target": "ada@example"},
            )
            assert refused.status_code == 403
            detail = refused.json()["detail"]
            assert "Solo" in detail and "1 active" in detail
            assert "firekeep.ai/pricing" in detail
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()
