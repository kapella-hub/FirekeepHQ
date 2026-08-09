"""Atomic member invite issue/accept operations on Redis DB 7.

Membership is unmetered: the WATCH transactions below exist for invite/record
consistency under concurrency, not for seat counting. (The seat-limit gate that
used to live inside them was removed with the single-product conversion — the
BUSL LICENSE's multi-member terms are legal, not enforced here.)
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.exceptions import WatchError

from auth.workspace import MEMBER_INDEX, MEMBER_PREFIX, Workspace

from app.enroll.store import TICKET_INDEX, TICKET_PREFIX, EnrollmentStore, ticket_id


INVITE_PREFIX = "auth:member_invite:"
INVITE_INDEX = "auth:member_invite_index"
INVITE_TTL_DAYS = 7
INVITE_TOMBSTONE_DAYS = 14


class MemberInviteError(RuntimeError):
    pass


class MemberStore:
    def __init__(self, redis_client, enrollment_store: EnrollmentStore) -> None:
        self.redis = redis_client
        self.enrollment = enrollment_store

    async def _prune_expired(self, now_epoch: float) -> None:
        await self.redis.zremrangebyscore(INVITE_INDEX, "-inf", now_epoch)

    async def issue(
        self,
        *,
        workspace: Workspace,
        label: str,
        email: str,
        issuer: str,
        connection: dict[str, str],
        now: datetime | None = None,
    ) -> tuple[str, str, dict[str, str]]:
        now = now or datetime.now(timezone.utc)
        expires = now + timedelta(days=INVITE_TTL_DAYS)
        await self._prune_expired(now.timestamp())

        for _ in range(5):
            secret = secrets.token_urlsafe(32)
            tid = ticket_id(secret)
            key = f"{INVITE_PREFIX}{tid}"
            member_id = f"member-{secrets.token_hex(16)}"
            record = {
                "ticket_hash": hashlib.sha256(secret.encode("ascii")).hexdigest(),
                "workspace_id": workspace.workspace_id,
                "member_id": member_id,
                "label": label,
                "email": email,
                "role": "member",
                "issuer": issuer,
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "expires_at_epoch": str(int(expires.timestamp())),
                **connection,
            }
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(MEMBER_INDEX, INVITE_INDEX, key)
                    if await pipe.exists(key):
                        await pipe.unwatch()
                        continue
                    pipe.multi()
                    pipe.hset(key, mapping=record)
                    pipe.expire(key, INVITE_TOMBSTONE_DAYS * 86400)
                    pipe.zadd(INVITE_INDEX, {tid: expires.timestamp()})
                    await pipe.execute()
                    return secret, tid, record
                except WatchError:
                    continue
        raise MemberInviteError("member invite changed concurrently; retry")

    async def accept(
        self,
        *,
        secret: str,
        workspace: Workspace,
        now: datetime | None = None,
    ) -> tuple[dict[str, str], dict[str, str], bool]:
        now = now or datetime.now(timezone.utc)
        try:
            tid = ticket_id(secret)
        except ValueError as exc:
            raise MemberInviteError("malformed member invite") from exc
        invite_key = f"{INVITE_PREFIX}{tid}"
        await self._prune_expired(now.timestamp())

        for _ in range(5):
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(invite_key, MEMBER_INDEX, f"{TICKET_PREFIX}{tid}")
                    invite = await pipe.hgetall(invite_key)
                    if not invite:
                        await pipe.unwatch()
                        raise MemberInviteError("member invite is unknown or no longer available")
                    if invite.get("ticket_hash") != hashlib.sha256(
                        secret.encode("ascii")
                    ).hexdigest():
                        await pipe.unwatch()
                        raise MemberInviteError("member invite does not match its server record")
                    if invite.get("workspace_id") != workspace.workspace_id:
                        await pipe.unwatch()
                        raise MemberInviteError("member invite belongs to another workspace")
                    if invite.get("used_at"):
                        enrollment = await pipe.hgetall(f"{TICKET_PREFIX}{tid}")
                        member = await pipe.hgetall(f"{MEMBER_PREFIX}{invite['member_id']}")
                        await pipe.unwatch()
                        if enrollment and member:
                            return member, enrollment, True
                        raise MemberInviteError(
                            "member invite was accepted but its device enrollment expired; "
                            "ask the workspace owner for a device invite"
                        )
                    if int(invite.get("expires_at_epoch", "0")) <= int(now.timestamp()):
                        pipe.multi()
                        pipe.hset(invite_key, mapping={"status": "expired"})
                        pipe.zrem(INVITE_INDEX, tid)
                        await pipe.execute()
                        raise MemberInviteError("member invite has expired")

                    member_id = invite["member_id"]
                    member = {
                        "member_id": member_id,
                        "workspace_id": workspace.workspace_id,
                        "label": invite.get("label", ""),
                        "email": invite.get("email", ""),
                        "role": "member",
                        "status": "active",
                        "created_at": now.isoformat(),
                    }
                    _, _, enrollment = self.enrollment.prepare_issue(
                        ticket=secret,
                        agent_label=invite.get("label", ""),
                        transport=invite["transport"],
                        kind=invite["kind"],
                        host=invite.get("host", ""),
                        base_url=invite.get("base_url", ""),
                        ca_pem=invite.get("ca_pem", ""),
                        ssh_target=invite.get("ssh_target", ""),
                        issuer=invite.get("issuer", "workspace owner"),
                        key_expires_days=int(invite.get("key_expires_days", "0")),
                        member_id=member_id,
                        now=now,
                    )
                    if invite.get("ca_mode"):
                        enrollment["ca_mode"] = invite["ca_mode"]
                    pipe.multi()
                    pipe.hset(f"{MEMBER_PREFIX}{member_id}", mapping=member)
                    pipe.zadd(MEMBER_INDEX, {member_id: now.timestamp()})
                    pipe.hset(
                        invite_key,
                        mapping={"used_at": now.isoformat(), "status": "accepted"},
                    )
                    pipe.zrem(INVITE_INDEX, tid)
                    pipe.hset(f"{TICKET_PREFIX}{tid}", mapping=enrollment)
                    pipe.expire(
                        f"{TICKET_PREFIX}{tid}",
                        self.enrollment.settings.tombstone_days * 86400,
                    )
                    pipe.zadd(TICKET_INDEX, {tid: now.timestamp()})
                    await pipe.execute()
                    return member, enrollment, False
                except WatchError:
                    continue
        raise MemberInviteError("member invite changed concurrently; retry")

    async def list_members(self) -> list[dict[str, Any]]:
        member_ids = await self.redis.zrange(MEMBER_INDEX, 0, -1)
        rows = []
        for member_id in member_ids:
            row = await self.redis.hgetall(f"{MEMBER_PREFIX}{member_id}")
            if row:
                rows.append(row)
        return rows

    async def list_outstanding(self) -> list[dict[str, Any]]:
        await self._prune_expired(datetime.now(timezone.utc).timestamp())
        tids = await self.redis.zrange(INVITE_INDEX, 0, -1)
        rows = []
        for tid in tids:
            row = await self.redis.hgetall(f"{INVITE_PREFIX}{tid}")
            if row:
                safe = {k: v for k, v in row.items() if k != "ticket_hash"}
                rows.append({"tid": tid, **safe})
        return rows

    async def anchor(self, tid: str) -> str | None:
        record = await self.redis.hgetall(f"{INVITE_PREFIX}{tid}")
        return record.get("ca_pem") if record else None

    async def cancel(self, tid: str) -> bool:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(f"{INVITE_PREFIX}{tid}")
            pipe.zrem(INVITE_INDEX, tid)
            deleted, _ = await pipe.execute()
        return bool(deleted)
