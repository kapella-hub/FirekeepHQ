"""Members consume seats; credentials, devices, and runtimes do not."""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis.aioredis
import pytest

from app.enroll.store import EnrollmentStore, TICKET_PREFIX
from app.members.store import MemberStore, SeatLimitError
from auth.entitlements import Entitlement, solo_entitlement
from auth.keys import build_credential_record
from auth.workspace import MEMBER_INDEX, MEMBER_PREFIX, ensure_workspace


def _team(workspace_id: str, seats: int = 2) -> Entitlement:
    return Entitlement(
        workspace_id=workspace_id,
        customer="Acme",
        plan="team",
        max_members=seats,
        issued_at="2026-07-01T00:00:00+00:00",
        expires_at="2027-07-01T00:00:00+00:00",
        verified=True,
        source="test",
        reason="verified",
    )


def _connection() -> dict[str, str]:
    return {
        "transport": "tunnel",
        "kind": "ports",
        "host": "127.0.0.1",
        "base_url": "",
        "ca_pem": "",
        "ca_mode": "",
        "ssh_target": "root@example",
        "key_expires_days": "90",
        "dist_base": "https://firekeep.ai",
    }


@pytest.mark.asyncio
async def test_solo_refuses_member_invite_with_actionable_counts(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-solo")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-solo")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    workspace = await ensure_workspace(redis)
    store = MemberStore(redis, EnrollmentStore(redis))
    try:
        with pytest.raises(SeatLimitError) as caught:
            await store.issue(
                workspace=workspace,
                entitlement=solo_entitlement(workspace.workspace_id),
                label="Teammate",
                email="team@example.com",
                issuer="owner",
                connection=_connection(),
            )
        detail = caught.value.detail()
        assert "Solo" in detail
        assert "1 active" in detail
        assert "allows 1" in detail
        assert "firekeep.ai/pricing" in detail
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_rechecks_seat_after_invite_was_issued(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-race")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-race")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    workspace = await ensure_workspace(redis)
    store = MemberStore(redis, EnrollmentStore(redis))
    try:
        ticket, _, _ = await store.issue(
            workspace=workspace,
            entitlement=_team(workspace.workspace_id),
            label="Invited",
            email="invited@example.com",
            issuer="owner",
            connection=_connection(),
        )
        await redis.hset(
            f"{MEMBER_PREFIX}member-other",
            mapping={"member_id": "member-other", "status": "active"},
        )
        await redis.zadd(MEMBER_INDEX, {"member-other": 2})
        with pytest.raises(SeatLimitError) as caught:
            await store.accept(
                secret=ticket,
                workspace=workspace,
                entitlement=_team(workspace.workspace_id),
            )
        assert caught.value.active_members == 2
        assert "2 active" in caught.value.detail()
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_team_accept_atomically_creates_member_and_bound_device_ticket(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-team")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-team")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    workspace = await ensure_workspace(redis)
    enrollment = EnrollmentStore(redis)
    store = MemberStore(redis, enrollment)
    try:
        ticket, tid, invite = await store.issue(
            workspace=workspace,
            entitlement=_team(workspace.workspace_id),
            label="Ada",
            email="ada@example.com",
            issuer="owner",
            connection=_connection(),
        )
        member, device_ticket, replay = await store.accept(
            secret=ticket,
            workspace=workspace,
            entitlement=_team(workspace.workspace_id),
        )
        assert replay is False
        assert member["member_id"] == invite["member_id"]
        assert device_ticket["member_id"] == member["member_id"]
        assert await redis.hgetall(f"{TICKET_PREFIX}{tid}") == device_ticket
        assert await redis.zscore(MEMBER_INDEX, member["member_id"]) is not None

        replay_member, replay_ticket, replay = await store.accept(
            secret=ticket,
            workspace=workspace,
            entitlement=_team(workspace.workspace_id),
        )
        assert replay is True
        assert replay_member == member
        assert replay_ticket == device_ticket
    finally:
        await redis.aclose()


def test_second_device_credential_for_same_member_has_no_seat_input():
    first = build_credential_record(
        "cred-a",
        "device-a",
        ["memory:read"],
        datetime(2026, 7, 31, tzinfo=timezone.utc),
        None,
        workspace_id="workspace-a",
        member_id="member-a",
    )
    second = build_credential_record(
        "cred-b",
        "device-b",
        ["memory:read"],
        datetime(2026, 7, 31, tzinfo=timezone.utc),
        None,
        workspace_id="workspace-a",
        member_id="member-a",
    )
    assert first["member_id"] == second["member_id"]
    assert first["device_id"] != second["device_id"]
    assert "plan" not in first and "plan" not in second
