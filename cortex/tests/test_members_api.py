"""Customer-facing workspace and member-invite API behavior.

Membership is unmetered (single-product conversion): invites and acceptance
must work with no licence document, no plan identity, and no seat counting —
and their responses must not leak any entitlement-era keys. The tests here
also pin the auth boundary that DID survive: admin routes need an API key,
acceptance is invite-authenticated and deliberately keyless.
"""

from __future__ import annotations

import base64
import json

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from app.members.api import create_members_router
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.keys import create_key, init_auth
from auth.workspace import ensure_workspace


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
async def test_member_invite_and_public_accept_need_no_licence(monkeypatch):
    """The whole flow — invite, accept, list — with nothing but an admin key.

    Before the single-product conversion this exact flow required applying a
    signed Team licence first; a bare workspace refused the second member with
    a 403 seat error. The absence of any licence step here IS the invariant.
    """
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-api")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-api")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_auth(redis_client=redis, enabled=True)
    try:
        workspace = await ensure_workspace(redis)
        admin = await create_key("owner-device", ["*"])
        app = await _app(redis, workspace)
        headers = {"X-API-Key": admin["api_key"]}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
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
            assert "entitlement" not in accepted.json()

            members = await client.get("/members", headers=headers)
            assert members.status_code == 200
            assert members.json()["active_count"] == 2
            assert members.json()["outstanding_invite_count"] == 0
            assert "entitlement" not in members.json()

            workspace_view = await client.get("/workspace", headers=headers)
            assert workspace_view.status_code == 200
            assert "entitlement" not in workspace_view.json()
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()


@pytest.mark.asyncio
async def test_many_members_join_without_any_limit(monkeypatch):
    """No seat ceiling exists anymore: N invites all issue and all accept.

    The old system refused the second member of an unlicensed workspace; this
    guard fails if any future change reintroduces a count-based refusal on the
    invite or accept path.
    """
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-api-many")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-api-many")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_auth(redis_client=redis, enabled=True)
    try:
        workspace = await ensure_workspace(redis)
        admin = await create_key("owner-device", ["*"])
        app = await _app(redis, workspace)
        headers = {"X-API-Key": admin["api_key"]}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(5):
                issued = await client.post(
                    "/members/invites",
                    headers=headers,
                    json={
                        "label": f"Member {i}",
                        "transport": "tunnel",
                        "kind": "ports",
                        "host": "127.0.0.1",
                        "ssh_target": f"m{i}@example",
                    },
                )
                assert issued.status_code == 200, issued.text
                accepted = await client.post(
                    "/members/invites/accept",
                    json={"ticket": _member_ticket(issued.json()["code"])},
                )
                assert accepted.status_code == 200, accepted.text

            members = await client.get("/members", headers=headers)
            # Owner + the five invited members.
            assert members.json()["active_count"] == 6
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()


@pytest.mark.asyncio
async def test_admin_routes_stay_protected(monkeypatch):
    """Removing the seat gate must not loosen AUTH: inviting still needs the
    admin key, and the retired /licence endpoints are genuinely gone (404),
    not silently open."""
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-api-auth")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-api-auth")
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

            headers = {"X-API-Key": admin["api_key"]}
            for method, path in (
                ("GET", "/licence"),
                ("POST", "/licence"),
                ("DELETE", "/licence"),
            ):
                gone = await client.request(method, path, headers=headers)
                assert gone.status_code == 404, (method, path, gone.status_code)
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()
