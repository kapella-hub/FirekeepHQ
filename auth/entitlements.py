"""Offline Ed25519 entitlement verification.

The server is the only evaluator. Invalid, absent, mismatched, or expired
documents always resolve to the built-in Solo entitlement; they never block
reads or disable runtime capabilities.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


LICENCE_REDIS_KEY = "auth:licence:document"
LICENCE_PREFIX = "fk_lic_v1"
EXPIRY_WARNING_DAYS = 30


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Entitlement:
    workspace_id: str
    customer: str
    plan: str
    max_members: int
    issued_at: str | None
    expires_at: str | None
    verified: bool
    source: str
    reason: str
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def solo_entitlement(
    workspace_id: str,
    *,
    reason: str = "no licence installed",
    source: str = "built-in",
) -> Entitlement:
    return Entitlement(
        workspace_id=workspace_id,
        customer="",
        plan="solo",
        max_members=1,
        issued_at=None,
        expires_at=None,
        verified=False,
        source=source,
        reason=reason,
    )


def sign_licence(payload: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
    """Return the canonical wire document. Used by release tooling and tests."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(body)
    return f"{LICENCE_PREFIX}.{_b64url(body)}.{_b64url(signature)}"


def verify_licence(
    document: str | None,
    workspace_id: str,
    *,
    public_key: str | None = None,
    now: datetime | None = None,
    source: str = "configured",
) -> Entitlement:
    """Verify one document, degrading every failure to Solo."""
    if not document:
        return solo_entitlement(workspace_id)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        prefix, encoded_payload, encoded_signature = document.strip().split(".")
        if prefix != LICENCE_PREFIX:
            raise ValueError("unsupported licence format")
        key_text = (public_key or os.getenv("FIREKEEP_LICENCE_PUBLIC_KEY", "")).strip()
        if not key_text:
            raise ValueError("licence verification key is not configured")
        key_bytes = _decode_b64url(key_text)
        if len(key_bytes) != 32:
            raise ValueError("licence verification key must be 32 raw Ed25519 bytes")
        payload_bytes = _decode_b64url(encoded_payload)
        signature = _decode_b64url(encoded_signature)
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, payload_bytes)
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            raise ValueError("licence payload must be an object")

        payload_workspace = str(payload["workspace_id"])
        customer = str(payload["customer"])
        plan = str(payload["plan"]).lower()
        max_members = payload["max_members"]
        issued_at = str(payload["issued_at"])
        expires_at = str(payload["expires_at"])
        if payload_workspace != workspace_id:
            raise ValueError("licence belongs to another workspace")
        if plan not in {"solo", "team"}:
            raise ValueError("plan must be solo or team")
        if isinstance(max_members, bool) or not isinstance(max_members, int) or max_members < 1:
            raise ValueError("max_members must be a positive integer")
        if plan == "solo" and max_members != 1:
            raise ValueError("Solo licences must have max_members=1")
        if plan == "team" and max_members < 2:
            raise ValueError("Team licences must have max_members>=2")
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        if expires <= issued:
            raise ValueError("licence expiry must be after issuance")
        if expires <= now:
            return solo_entitlement(
                workspace_id,
                reason=f"licence expired at {expires_at}; degraded to Solo",
                source=source,
            )

        warning = None
        if expires - now <= timedelta(days=EXPIRY_WARNING_DAYS):
            remaining = max(0, (expires - now).days)
            warning = f"licence expires in {remaining} day(s) at {expires_at}"
        return Entitlement(
            workspace_id=workspace_id,
            customer=customer,
            plan=plan,
            max_members=max_members,
            issued_at=issued_at,
            expires_at=expires_at,
            verified=True,
            source=source,
            reason="verified",
            warning=warning,
        )
    except Exception as exc:
        return solo_entitlement(
            workspace_id,
            reason=f"invalid licence ({exc}); degraded to Solo",
            source=source,
        )


async def load_entitlement(redis_client, workspace_id: str) -> Entitlement:
    """Load the environment override or the dashboard/admin-installed document."""
    environment = os.getenv("FIREKEEP_LICENCE", "").strip()
    if environment:
        return verify_licence(environment, workspace_id, source="environment")
    stored = await redis_client.get(LICENCE_REDIS_KEY)
    return verify_licence(stored, workspace_id, source="redis" if stored else "built-in")
