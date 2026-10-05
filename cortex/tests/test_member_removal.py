"""DELETE /members/{id} and POST /members/{id}/restore (THREAT-MODEL §5.17).

There was no way to remove a member before 2026-10-05. These tests pin the
REST surface (admin-only, owner refused, idempotent), that the removed
member's existing key stops authenticating on Cortex, that their join codes
and member invite stop working, and that restore brings the SAME member back
with a fresh join code rather than a new identity.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timezone

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

import app.enroll.store as enroll_store
import app.members.store as members_store
import auth.members as auth_members
from app.enroll.store import EnrollmentStore
from app.members.api import create_members_router
from auth import keys
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.keys import create_key, init_auth
from auth.workspace import ensure_workspace


TUNNEL = {"transport": "tunnel", "kind": "ports", "host": "127.0.0.1", "ssh_target": "a@example"}


def _code_payload(code: str, prefix: str) -> dict:
    body = code.removeprefix(prefix).split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _client(redis, workspace):
    inner = FastAPI()
    inner.include_router(create_members_router(redis_client=redis, workspace=workspace))
    app = FirekeepKeyAuthMiddleware(
        inner,
        enabled=True,
        redis_url="redis://unused",
        skip_exact_paths=("/members/invites/accept", "/members/invites/anchor"),
        redis_client=redis,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _member_key(redis, member_id: str) -> str:
    """The key the member's own device would hold after redeeming a join code."""
    api_key = keys.generate_api_key()
    credential_id = secrets.token_hex(8)
    record = keys.build_credential_record(
        credential_id,
        secrets.token_hex(8),
        sorted(keys.ENROLLABLE_SCOPES),
        datetime.now(timezone.utc),
        None,
        enrolled_via="0123456789abcdef",
        member_id=member_id,
    )
    key_hash = keys._hash_key(api_key)
    await redis.hset(f"auth:key:{key_hash}", mapping=record)
    await redis.set(f"auth:cred:{credential_id}", key_hash)
    await redis.zadd("auth:key_index", {credential_id: 1})
    return api_key


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-removal")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-removal")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_auth(redis_client=redis, enabled=True)
    try:
        workspace = await ensure_workspace(redis)
        admin = await create_key("owner-device", ["*"])
        async with _client(redis, workspace) as client:
            headers = {"X-API-Key": admin["api_key"]}
            issued = await client.post(
                "/members/invites", headers=headers, json={"label": "Ada", **TUNNEL}
            )
            assert issued.status_code == 200, issued.text
            ticket = _code_payload(issued.json()["code"], "fk_member_")["m"]
            accepted = await client.post("/members/invites/accept", json={"ticket": ticket})
            assert accepted.status_code == 200, accepted.text
            yield {
                "redis": redis,
                "client": client,
                "admin": headers,
                "member_id": issued.json()["member_id"],
                "member_ticket": ticket,
                "join_code": accepted.json()["join_code"],
                "owner": workspace.owner_member_id,
            }
    finally:
        await init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def test_auth_members_key_names_match_the_cortex_stores():
    # auth/ cannot import app.*, so auth.members carries literals; they must
    # name exactly what enrollment and member invites write.
    assert auth_members.TICKET_PREFIX == enroll_store.TICKET_PREFIX
    assert auth_members.TICKET_INDEX == enroll_store.TICKET_INDEX
    assert auth_members.KEY_PREFIX == enroll_store.KEY_PREFIX
    assert auth_members.CRED_PREFIX == enroll_store.CREDENTIAL_PREFIX
    assert auth_members.KEY_INDEX == enroll_store.KEY_INDEX
    assert auth_members.INVITE_PREFIX == members_store.INVITE_PREFIX
    assert auth_members.INVITE_INDEX == members_store.INVITE_INDEX


async def test_removed_members_existing_key_gets_401_on_cortex(env):
    client, member_id = env["client"], env["member_id"]
    member_key = await _member_key(env["redis"], member_id)
    before = await client.get("/workspace", headers={"X-API-Key": member_key})
    assert before.status_code == 200 and before.json()["member_id"] == member_id

    removed = await client.delete(f"/members/{member_id}", headers=env["admin"])

    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["status"] == "removed" and body["already_removed"] is False
    assert len(body["credentials_revoked"]) == 1
    after = await client.get("/workspace", headers={"X-API-Key": member_key})
    assert after.status_code == 401


async def test_removal_is_idempotent_and_listing_keeps_the_row(env):
    client, member_id = env["client"], env["member_id"]
    first = await client.delete(f"/members/{member_id}", headers=env["admin"])
    second = await client.delete(f"/members/{member_id}", headers=env["admin"])
    assert first.status_code == second.status_code == 200
    assert second.json()["already_removed"] is True

    listing = (await client.get("/members", headers=env["admin"])).json()
    rows = {r["member_id"]: r for r in listing["members"]}
    assert rows[member_id]["status"] == "removed"
    assert listing["active_count"] == 1  # the owner; the removed row is not counted


async def test_deployment_owner_removal_is_refused(env):
    resp = await env["client"].delete(f"/members/{env['owner']}", headers=env["admin"])
    assert resp.status_code == 409
    assert "owner can never be removed" in resp.json()["detail"]


async def test_unknown_member_is_404(env):
    for member_id in ("member-nobody", "not%20an%20id"):
        resp = await env["client"].delete(f"/members/{member_id}", headers=env["admin"])
        assert resp.status_code == 404, member_id


async def test_removal_needs_admin(env):
    client, member_id = env["client"], env["member_id"]
    other = await create_key("teammate", sorted(keys.ENROLLABLE_SCOPES))
    resp = await client.delete(f"/members/{member_id}", headers={"X-API-Key": other["api_key"]})
    assert resp.status_code == 403
    assert (await client.delete(f"/members/{member_id}")).status_code == 401
    assert (await env["redis"].hget(f"auth:member:{member_id}", "status")) == "active"


async def test_removed_members_join_codes_are_cancelled(env):
    redis, member_id = env["redis"], env["member_id"]
    _, device_tid, _ = await EnrollmentStore(redis).issue(
        transport="tunnel", kind="ports", host="127.0.0.1", ssh_target="a@example",
        member_id=member_id,
    )
    join_tid = enroll_store.ticket_id(_code_payload(env["join_code"], "fk_join_")["q"])

    removed = await env["client"].delete(f"/members/{member_id}", headers=env["admin"])

    # Both unredeemed codes are gone: the redeem script's first check is
    # EXISTS, so each now answers "unknown" without spending anything.
    assert set(removed.json()["join_codes_cancelled"]) == {device_tid, join_tid}
    assert await redis.exists(f"auth:enroll:{device_tid}") == 0
    assert await redis.exists(f"auth:enroll:{join_tid}") == 0
    # Replaying the accepted member invite no longer hands a code back.
    replay = await env["client"].post(
        "/members/invites/accept", json={"ticket": env["member_ticket"]}
    )
    assert replay.status_code == 409
    assert "removed" in replay.json()["detail"]


async def test_removed_members_join_code_cannot_mint_a_credential(env):
    # Runs the real redeem script (fakeredis needs lupa for EVAL).
    pytest.importorskip("lupa")
    redis, member_id = env["redis"], env["member_id"]
    store = EnrollmentStore(redis)
    device_ticket, _, _ = await store.issue(
        transport="tunnel", kind="ports", host="127.0.0.1", ssh_target="a@example",
        member_id=member_id,
    )
    join_ticket = _code_payload(env["join_code"], "fk_join_")["q"]

    await env["client"].delete(f"/members/{member_id}", headers=env["admin"])

    for i, ticket in enumerate((device_ticket, join_ticket)):
        outcome, _, _ = await store.consume(
            ticket=ticket, credential_hash=f"{i}" * 64, device_nonce=f"{i}" * 16,
        )
        assert outcome == "unknown"
        assert await redis.exists(f"auth:key:{f'{i}' * 64}") == 0


async def test_a_code_minted_before_removal_but_not_swept_is_still_refused(env):
    # Belt and braces for a removal interrupted after the status flip: the
    # redeem script itself refuses an inactive member, code unspent.
    pytest.importorskip("lupa")
    redis, member_id = env["redis"], env["member_id"]
    store = EnrollmentStore(redis)
    ticket, tid, _ = await store.issue(
        transport="tunnel", kind="ports", host="127.0.0.1", ssh_target="a@example",
        member_id=member_id,
    )
    await redis.hset(f"auth:member:{member_id}", "status", "removed")

    outcome, fields, _ = await store.consume(
        ticket=ticket, credential_hash="e" * 64, device_nonce="3" * 16,
    )
    assert outcome == "member_inactive", fields
    assert await redis.exists(f"auth:key:{'e' * 64}") == 0
    assert not await redis.hget(f"auth:enroll:{tid}", "used_at")


async def test_restore_returns_the_same_member_with_a_fresh_join_code(env):
    client, redis, member_id = env["client"], env["redis"], env["member_id"]
    old_key = await _member_key(redis, member_id)
    await client.delete(f"/members/{member_id}", headers=env["admin"])

    restored = await client.post(
        f"/members/{member_id}/restore", headers=env["admin"], json=TUNNEL
    )

    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["member_id"] == member_id
    assert body["membership"]["status"] == "active"
    assert body["code"].startswith("fk_join_")
    assert "install_command_sh" in body and "install_command_powershell" in body
    ticket = await redis.hgetall(f"auth:enroll:{body['tid']}")
    assert ticket["member_id"] == member_id
    # Old credentials stay revoked.
    assert (await client.get("/workspace", headers={"X-API-Key": old_key})).status_code == 401
    # Restoring an active member is refused: this is not a way to mint codes
    # for active members.
    again = await client.post(f"/members/{member_id}/restore", headers=env["admin"], json=TUNNEL)
    assert again.status_code == 409


async def test_restored_members_new_code_mints_for_the_same_member(env):
    pytest.importorskip("lupa")
    client, redis, member_id = env["client"], env["redis"], env["member_id"]
    await client.delete(f"/members/{member_id}", headers=env["admin"])
    body = (
        await client.post(f"/members/{member_id}/restore", headers=env["admin"], json=TUNNEL)
    ).json()

    outcome, fields, _ = await EnrollmentStore(redis).consume(
        ticket=_code_payload(body["code"], "fk_join_")["q"],
        credential_hash="d" * 64,
        device_nonce="2" * 16,
    )
    assert outcome == "ok", fields
    assert await redis.hget(f"auth:key:{'d' * 64}", "member_id") == member_id


async def test_restore_needs_admin_and_a_known_member(env):
    client = env["client"]
    assert (await client.post("/members/member-nobody/restore", headers=env["admin"], json=TUNNEL)).status_code == 404
    other = await create_key("teammate", sorted(keys.ENROLLABLE_SCOPES))
    resp = await client.post(
        f"/members/{env['member_id']}/restore", headers={"X-API-Key": other["api_key"]}, json=TUNNEL
    )
    assert resp.status_code == 403
