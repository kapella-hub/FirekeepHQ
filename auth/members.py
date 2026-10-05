"""Removing and restoring a workspace member (Redis DB 7).

THREAT-MODEL §5.17 (2026-10-05). Until this module there was no way to remove
a member: the only lever was revoking credentials one device at a time, and
any join code still outstanding for that person could mint a new one. Since
2026-10-04 `validate_key` refuses every credential whose member row is not
`status: active` (auth/keys.py `_check_record`), so flipping the row is what
actually revokes access everywhere -- Cortex, Bridge, Relay and Sentinel all
validate per request through that one function, with no cache.

Removal, in this order (the order is the concurrency argument):

1. Flip ``auth:member:<id>`` to ``status: removed`` (row kept, never deleted:
   memories, sessions, replay traces and relay records stay attributed to the
   member_id they were written under). From this write on, every credential
   naming the member is refused, and the enrollment script refuses to redeem
   a join code for them (cortex/app/enroll/lua.py ``member_inactive``) -- so
   no credential can be registered for the member after this point.
2. Sweep: delete every ``auth:key:*`` record naming the member, its
   ``auth:cred:`` mapping and its ``auth:key_index`` entry. Scanned (not read
   from the index) so an unindexed record is found too. Anything registered
   before step 1 is found here; nothing can be registered after it.
3. Cancel the member's unredeemed join codes (``auth:enroll:<tid>``) and
   revoke their member invites (``auth:member_invite:<tid>``).

A crash between 1 and 2 leaves credentials that are already refused but still
listed; ``deploy/firekeep-admin keys audit`` reports them as "member not
active" and re-running the removal (idempotent) deletes them.

Refused: the deployment owner (``auth:workspace:current`` / FIREKEEP_OWNER_
MEMBER_ID) can never be removed; a caller cannot remove the member they are
acting as; and a removal that would leave no active admin-holder is refused.
Today every admin-capable credential belongs to the owner, so the last check
cannot fire -- it is defense in depth, not a live gap.

What removal does NOT touch, deliberately: member-private memories, Bridge
sessions, Relay rows and member-owned vault secrets stay where they are,
attributed to the removed member. Nothing is transferred to or re-stamped as
the owner. See docs/guides/auth-and-provenance.md "Removing and restoring a
member".

The enrollment/invite key names below are literals because auth/ must not
import cortex's app package; cortex/tests/test_member_removal.py pins them to
app.enroll.store and app.members.store.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from auth.workspace import MEMBER_INDEX, MEMBER_PREFIX


KEY_PREFIX = "auth:key:"
CRED_PREFIX = "auth:cred:"
KEY_INDEX = "auth:key_index"
TICKET_PREFIX = "auth:enroll:"
TICKET_INDEX = "auth:enroll:index"
INVITE_PREFIX = "auth:member_invite:"
INVITE_INDEX = "auth:member_invite_index"

MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_KEY_RE = re.compile(r"^auth:key:[0-9a-f]{64}$")
_TICKET_RE = re.compile(r"^auth:enroll:[0-9a-f]{16}$")
_INVITE_RE = re.compile(r"^auth:member_invite:[0-9a-f]{16}$")

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"


class MemberRemovalError(Exception):
    """The removal/restore was refused; nothing was changed."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _scopes(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


async def _load_member(redis_client, member_id: str, workspace_id: str) -> dict[str, str]:
    # One answer for "malformed", "unknown" and "another workspace": the route
    # must not become an oracle for which member ids exist elsewhere.
    if not MEMBER_ID_RE.fullmatch(member_id or ""):
        raise MemberRemovalError(404, f"member {member_id} not found in this workspace")
    row = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
    if not row or row.get("workspace_id") != workspace_id:
        raise MemberRemovalError(404, f"member {member_id} not found in this workspace")
    return row


async def _member_credentials(redis_client, member_id: str) -> list[tuple[str, dict[str, str]]]:
    found = []
    async for key in redis_client.scan_iter(f"{KEY_PREFIX}*", count=200):
        if not _KEY_RE.fullmatch(key):
            continue
        record = await redis_client.hgetall(key)
        if record and record.get("member_id") == member_id:
            found.append((key, record))
    return found


async def _admin_holders(redis_client, workspace_id: str) -> set[str]:
    """Active members who can administer this workspace.

    The owner role, or any credential whose scopes allow ``admin``.
    """
    holders: set[str] = set()
    for member_id in await redis_client.zrange(MEMBER_INDEX, 0, -1):
        row = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
        if (
            row.get("workspace_id") == workspace_id
            and row.get("status") == STATUS_ACTIVE
            and row.get("role") == "owner"
        ):
            holders.add(member_id)
    async for key in redis_client.scan_iter(f"{KEY_PREFIX}*", count=200):
        if not _KEY_RE.fullmatch(key):
            continue
        record = await redis_client.hgetall(key)
        member_id = record.get("member_id") if record else None
        if not member_id or record.get("workspace_id") != workspace_id:
            continue
        scopes = _scopes(record.get("scopes"))
        if "admin" not in scopes and "*" not in scopes:
            continue
        row = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
        if row.get("status") == STATUS_ACTIVE and row.get("workspace_id") == workspace_id:
            holders.add(member_id)
    return holders


async def remove_member(
    redis_client,
    member_id: str,
    *,
    workspace_id: str,
    owner_member_id: str,
    removed_by: str,
    actor_member_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove a member: refuse their credentials, then delete them.

    Idempotent: removing an already-removed member re-runs the sweep (which
    is how a removal interrupted after step 1 is finished) and reports
    ``already_removed: True``.
    """
    row = await _load_member(redis_client, member_id, workspace_id)
    if member_id == owner_member_id or row.get("role") == "owner":
        raise MemberRemovalError(
            409,
            "the deployment owner can never be removed: every legacy record and "
            "service credential belongs to the owner, and the workspace would be "
            "left without one",
        )
    if actor_member_id and actor_member_id == member_id:
        raise MemberRemovalError(
            409,
            "you cannot remove the member you are acting as; ask another admin",
        )
    already_removed = row.get("status") == STATUS_REMOVED
    if not already_removed:
        others = await _admin_holders(redis_client, workspace_id) - {member_id}
        if not others:
            raise MemberRemovalError(
                409,
                f"removing {member_id} would leave this workspace with no active "
                "admin; nothing was changed",
            )

    stamp = _now(now).isoformat()
    # Step 1 -- the revocation itself. Its own write, BEFORE the scan below.
    if not already_removed:
        await redis_client.hset(
            f"{MEMBER_PREFIX}{member_id}",
            mapping={"status": STATUS_REMOVED, "removed_at": stamp, "removed_by": removed_by},
        )

    # Step 2 -- credentials.
    revoked: list[str] = []
    for key, record in await _member_credentials(redis_client, member_id):
        credential_id = record.get("credential_id") or record.get("key_id") or ""
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            if credential_id:
                pipe.delete(f"{CRED_PREFIX}{credential_id}")
                pipe.zrem(KEY_INDEX, credential_id)
            await pipe.execute()
        revoked.append(credential_id or key.removeprefix(KEY_PREFIX)[:16])

    # Step 3 -- join codes and member invites. A redeemed ticket is left as the
    # tombstone it is (it is what tells a replaying client "already used").
    cancelled: list[str] = []
    async for key in redis_client.scan_iter(f"{TICKET_PREFIX}*", count=200):
        if not _TICKET_RE.fullmatch(key):
            continue
        ticket = await redis_client.hgetall(key)
        if not ticket or ticket.get("member_id") != member_id or ticket.get("used_at"):
            continue
        tid = key.removeprefix(TICKET_PREFIX)
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.zrem(TICKET_INDEX, tid)
            await pipe.execute()
        cancelled.append(tid)

    invites: list[str] = []
    async for key in redis_client.scan_iter(f"{INVITE_PREFIX}*", count=200):
        if not _INVITE_RE.fullmatch(key):
            continue
        invite = await redis_client.hgetall(key)
        if not invite or invite.get("member_id") != member_id:
            continue
        tid = key.removeprefix(INVITE_PREFIX)
        if invite.get("status") == "member_removed":
            continue
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"status": "member_removed"})
            pipe.zrem(INVITE_INDEX, tid)
            await pipe.execute()
        invites.append(tid)

    member = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
    return {
        "status": STATUS_REMOVED,
        "member_id": member_id,
        "already_removed": already_removed,
        "member": member,
        "credentials_revoked": revoked,
        "join_codes_cancelled": cancelled,
        "member_invites_revoked": invites,
    }


async def restore_member(
    redis_client,
    member_id: str,
    *,
    workspace_id: str,
    restored_by: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return a removed member to ``active``. Issues NO credential.

    The removal deleted every credential, so a restored member authenticates
    only after a new join code is minted for them and redeemed (the REST
    restore route and `firekeep-admin invite --member` do that). Restoring
    under the same member_id is the point: their member-private memories,
    sessions and history become theirs again, rather than orphaned under an
    id nobody can act as. Idempotent on an active member.
    """
    row = await _load_member(redis_client, member_id, workspace_id)
    if row.get("status") == STATUS_ACTIVE:
        return row
    if row.get("status") != STATUS_REMOVED:
        raise MemberRemovalError(
            409,
            f"member {member_id} has status {row.get('status')!r}; only a removed "
            "member can be restored",
        )
    key = f"{MEMBER_PREFIX}{member_id}"
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.hset(
            key,
            mapping={
                "status": STATUS_ACTIVE,
                "restored_at": _now(now).isoformat(),
                "restored_by": restored_by,
                "last_removed_at": row.get("removed_at", ""),
            },
        )
        pipe.hdel(key, "removed_at", "removed_by")
        await pipe.execute()
    return await redis_client.hgetall(key)
