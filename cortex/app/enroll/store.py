"""Redis DB-7 storage for enrollment tickets and atomic redemption."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from auth.keys import ENROLLABLE_SCOPES, build_credential_record

from .lua import ENROLL_CONSUME

TICKET_PREFIX = "auth:enroll:"
TICKET_INDEX = "auth:enroll:index"
RATE_PREFIX = "auth:enroll:rate:"
KEY_PREFIX = "auth:key:"
CREDENTIAL_PREFIX = "auth:cred:"
KEY_INDEX = "auth:key_index"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def ticket_id(ticket: str) -> str:
    """Return the non-secret lookup ID for a 32-byte ticket secret."""
    try:
        raw = base64.urlsafe_b64decode(ticket + "=" * (-len(ticket) % 4))
    except Exception as exc:
        raise ValueError("ticket is not valid base64url") from exc
    if len(raw) != 32 or _b64url(raw) != ticket:
        raise ValueError("ticket must encode exactly 32 bytes")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class EnrollmentSettings:
    ticket_ttl_hours: int = 24
    tombstone_days: int = 7
    key_expires_days: int = 90
    max_attempts_per_hour: int = 60


class EnrollmentStore:
    def __init__(self, redis_client, settings: EnrollmentSettings | None = None) -> None:
        self.redis = redis_client
        self.settings = settings or EnrollmentSettings()

    def prepare_issue(
        self,
        *,
        agent_label: str = "",
        transport: str,
        kind: str,
        host: str = "",
        base_url: str = "",
        ca_pem: str = "",
        ssh_target: str = "",
        issuer: str = "dashboard",
        key_expires_days: int | None = None,
        device_id: str = "",
        member_id: str | None = None,
        ticket: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str, dict[str, str]]:
        """Build one enrollment ticket without writing it."""
        now = now or datetime.now(timezone.utc)
        expires = now + timedelta(hours=self.settings.ticket_ttl_hours)
        ticket = ticket or _b64url(secrets.token_bytes(32))
        tid = ticket_id(ticket)
        expires_days = (
            self.settings.key_expires_days
            if key_expires_days is None
            else key_expires_days
        )
        record = {
            "agent_label": agent_label,
            "scopes": json.dumps(sorted(ENROLLABLE_SCOPES)),
            "transport": transport,
            "kind": kind,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "expires_at_epoch": str(int(expires.timestamp())),
            "key_expires_days": str(expires_days),
            "issuer": issuer,
        }
        if host:
            record["host"] = host
        if base_url:
            record["base_url"] = base_url
        if ca_pem:
            record["ca_pem"] = ca_pem
        if ssh_target:
            record["ssh_target"] = ssh_target
        if device_id:
            record["device_id"] = device_id

        if member_id:
            record["member_id"] = member_id
        return ticket, tid, record

    async def issue(
        self,
        *,
        agent_label: str = "",
        transport: str,
        kind: str,
        host: str = "",
        base_url: str = "",
        ca_pem: str = "",
        ssh_target: str = "",
        issuer: str = "dashboard",
        key_expires_days: int | None = None,
        device_id: str = "",
        member_id: str | None = None,
        ticket: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str, dict[str, str]]:
        """Mint and persist one ticket, returning (secret, tid, record)."""
        ticket, tid, record = self.prepare_issue(
            agent_label=agent_label,
            transport=transport,
            kind=kind,
            host=host,
            base_url=base_url,
            ca_pem=ca_pem,
            ssh_target=ssh_target,
            issuer=issuer,
            key_expires_days=key_expires_days,
            device_id=device_id,
            member_id=member_id,
            ticket=ticket,
            now=now,
        )

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(f"{TICKET_PREFIX}{tid}", mapping=record)
            pipe.expire(
                f"{TICKET_PREFIX}{tid}",
                self.settings.tombstone_days * 86400,
            )
            created_epoch = datetime.fromisoformat(record["created_at"]).timestamp()
            pipe.zadd(TICKET_INDEX, {tid: created_epoch})
            await pipe.execute()
        return ticket, tid, record

    async def get_ticket(self, tid: str) -> dict[str, str] | None:
        record = await self.redis.hgetall(f"{TICKET_PREFIX}{tid}")
        return record or None

    async def anchor(self, tid: str) -> str | None:
        record = await self.get_ticket(tid)
        return record.get("ca_pem") if record else None

    async def consume(
        self,
        *,
        ticket: str,
        credential_hash: str,
        device_nonce: str,
        now: datetime | None = None,
    ) -> tuple[str, list[str], dict[str, str] | None]:
        """Redeem in one EVAL; return (outcome, fields, ticket snapshot)."""
        now = now or datetime.now(timezone.utc)
        tid = ticket_id(ticket)
        ticket_key = f"{TICKET_PREFIX}{tid}"

        # build_credential_record is the sole field-map definition. This read is
        # advisory only: the script revalidates the immutable fields before it
        # writes or spends anything.
        snapshot = await self.get_ticket(tid)
        scopes_json = snapshot.get("scopes", "[]") if snapshot else "[]"
        try:
            decoded_scopes = json.loads(scopes_json)
        except (json.JSONDecodeError, TypeError):
            decoded_scopes = None
        scope_shape_ok = isinstance(decoded_scopes, list) and all(
            isinstance(scope, str) for scope in decoded_scopes
        )
        scopes = decoded_scopes if scope_shape_ok else []
        key_expires_raw = snapshot.get("key_expires_days", "0") if snapshot else "0"
        try:
            key_expires_days = int(key_expires_raw)
        except ValueError:
            key_expires_days = 0

        credential_id = secrets.token_hex(8)
        existing_device_id = snapshot.get("device_id", "") if snapshot else ""
        device_id = existing_device_id or secrets.token_hex(8)
        metadata = build_credential_record(
            credential_id,
            device_id,
            scopes,
            now,
            key_expires_days or None,
            enrolled_via=tid,
            device_label=(snapshot.get("agent_label") or None) if snapshot else None,
            member_id=(snapshot.get("member_id") or None) if snapshot else None,
        )
        key_ttl = key_expires_days * 86400 if key_expires_days else 0
        rate_key = f"{RATE_PREFIX}{now.strftime('%Y%m%d%H')}"

        raw = await self.redis.eval(
            ENROLL_CONSUME,
            5,
            ticket_key,
            rate_key,
            f"{KEY_PREFIX}{credential_hash}",
            f"{CREDENTIAL_PREFIX}{credential_id}",
            KEY_INDEX,
            str(self.settings.max_attempts_per_hour),
            str(int(now.timestamp())),
            now.isoformat(),
            credential_hash,
            device_nonce,
            credential_id,
            json.dumps(metadata, separators=(",", ":")),
            json.dumps(sorted(ENROLLABLE_SCOPES), separators=(",", ":")),
            scopes_json,
            key_expires_raw,
            str(key_ttl),
            device_id,
            "1" if scope_shape_ok else "0",
        )
        fields = [str(item) for item in raw]
        outcome = fields[0] if fields else "error"
        # Fetch after a successful/replayed claim so response fields reflect the
        # record the script actually consumed, not merely the earlier snapshot.
        if outcome in {"ok", "replay", "used", "credential_gone", "expired"}:
            snapshot = await self.get_ticket(tid)
        if outcome in {"ok", "replay"} and snapshot:
            issued_hash = snapshot.get("issued_key_hash", credential_hash)
            credential = await self.redis.hgetall(f"{KEY_PREFIX}{issued_hash}")
            if credential.get("expires_at"):
                snapshot["issued_credential_expires_at"] = credential["expires_at"]
        return outcome, fields[1:], snapshot

    async def list_outstanding(self, limit: int = 100) -> list[dict[str, Any]]:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        tids = await self.redis.zrevrange(TICKET_INDEX, 0, limit - 1)
        rows: list[dict[str, Any]] = []
        for tid in tids:
            record = await self.get_ticket(tid)
            if not record or record.get("used_at"):
                continue
            if int(record.get("expires_at_epoch", "0")) < now_epoch:
                continue
            rows.append({"tid": tid, **record})
        return rows

    async def cancel(self, tid: str) -> bool:
        # Drop the index entry with the record. list_outstanding reads only the
        # newest `limit` tids and then filters, so tids left behind by a cancel
        # consume that window and can push live invites out of the listing.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(f"{TICKET_PREFIX}{tid}")
            pipe.zrem(TICKET_INDEX, tid)
            deleted, _ = await pipe.execute()
        return bool(deleted)
