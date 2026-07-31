"""Public enrollment redemption and admin ticket-management routes."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth.config import get_auth_settings
from auth.middleware import require_scope

from app.config import get_settings
from app.version import VERSION

from .mint import mint_invite
from .store import EnrollmentSettings, EnrollmentStore, ticket_id

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
_TID_RE = re.compile(r"^[0-9a-f]{16}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

AUTH_OFF_DETAIL = (
    "this server enforces no authentication (AUTH_ENABLED=false), so there is no "
    "key to issue. Run: firekeep install --host <host>. To require keys, set "
    "AUTH_ENABLED=true on the server and reissue the code."
)


class EnrollRequest(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=128)
    credential_hash: str
    device_nonce: str
    hostname: str | None = Field(default=None, max_length=200)


class InviteRequest(BaseModel):
    agent: str = Field(default="", max_length=64)
    expires_days: int | None = Field(default=None, ge=0, le=365)
    transport: str = Field(default="tunnel", pattern="^(tls|tunnel|http)$")
    kind: str = Field(default="ports", pattern="^(ports|paths)$")
    host: str = "127.0.0.1"
    base_url: str = ""
    ca_pem: str = ""
    ca_mode: str = Field(default="", pattern="^(|os)$")
    ssh_target: str = ""
    insecure_http: bool = False
    device_id: str = Field(default="", pattern="^$|^[0-9a-f]{16}$")
    dist_base: str = "https://firekeep.ai"


def _enrollment_settings() -> EnrollmentSettings:
    settings = get_settings()
    return EnrollmentSettings(
        ticket_ttl_hours=settings.ENROLL_TICKET_TTL_HOURS,
        tombstone_days=settings.ENROLL_TOMBSTONE_DAYS,
        key_expires_days=settings.ENROLL_KEY_EXPIRES_DAYS,
        max_attempts_per_hour=settings.ENROLL_MAX_ATTEMPTS_PER_HOUR,
    )


def _make_store():
    import redis.asyncio as aioredis

    client = aioredis.from_url(get_auth_settings().REDIS_URL, decode_responses=True)
    return EnrollmentStore(client, _enrollment_settings())


def _detail(
    outcome: str,
    fields: list[str],
    ticket: dict[str, str] | None,
    *,
    tid: str = "",
) -> tuple[int, str]:
    issuer = (ticket or {}).get("issuer", "the issuer")
    if outcome == "unknown":
        ticket_ref = f" (ticket {tid})" if tid else ""
        return 404, (
            f"the server does not recognise this join code{ticket_ref}. Either it was used more "
            f"than {get_settings().ENROLL_TOMBSTONE_DAYS} days ago, or it was mistyped. "
            f"Ask {issuer} for a new one."
        )
    if outcome == "used":
        used_at = fields[0] if fields else "an unknown time"
        credential_id = fields[1] if len(fields) > 1 else "unknown"
        return 409, (
            f"this join code was already redeemed at {used_at} by a different device, "
            f"issuing credential {credential_id}. Join codes are single-use. If that "
            f"was not you, tell {issuer} — that credential should be revoked: "
            f"deploy/firekeep-admin keys revoke {credential_id}"
        )
    if outcome == "expired":
        expires_at = fields[0] if fields else (ticket or {}).get("expires_at", "unknown")
        return 410, (
            f"this join code expired at {expires_at}. Join codes are valid for "
            f"{get_settings().ENROLL_TICKET_TTL_HOURS}h. Ask {issuer} for a new one."
        )
    if outcome == "cred_exists":
        return 409, (
            "that credential is already registered on this server. Your join code "
            "was not spent."
        )
    if outcome == "credential_gone":
        credential_id = fields[1] if len(fields) > 1 else "unknown"
        return 409, (
            "this join code was redeemed and the credential it issued is no longer "
            f"present on the server (credential {credential_id}; revoked, or expired "
            f"and reaped). Ask {issuer} for a new code."
        )
    if outcome == "rate":
        return 429, (
            "the server is refusing enrollment attempts right now (rate limit). Your "
            "code was NOT used. Retry in a minute."
        )
    if outcome == "scope_violation":
        return 500, (
            "this join code asks for privileges the server refuses to enroll. Nothing "
            f"was issued. Tell {issuer}: the ticket was hand-edited or minted by a "
            "mismatched tool version."
        )
    return 409, "the enrollment ticket changed while it was being redeemed; retry"


def _suggested_agent_id(ticket: dict[str, str], hostname: str | None) -> str | None:
    if not hostname or not _HOSTNAME_RE.fullmatch(hostname):
        return None
    short_host = hostname.split(".", 1)[0].lower()
    label = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", ticket.get("agent_label", "").strip()
    ).strip("-._").lower()
    suggested = f"{label}-{short_host}" if label else short_host
    return suggested[:64].rstrip("-._")


def create_enroll_router(
    *,
    store: EnrollmentStore | None = None,
    auth_enabled: bool | None = None,
    limiter=None,
) -> APIRouter:
    """Create both public redeem routes and admin invite-management routes."""
    router = APIRouter(tags=["enrollment"])
    enrollment_store = store or _make_store()
    enabled = get_auth_settings().ENABLED if auth_enabled is None else auth_enabled

    async def redeem(request: Request, req: EnrollRequest) -> dict[str, Any]:
        if not enabled:
            raise HTTPException(status_code=409, detail=AUTH_OFF_DETAIL)
        if not _HASH_RE.fullmatch(req.credential_hash):
            raise HTTPException(
                status_code=400,
                detail=(
                    "this client sent a malformed credential fingerprint. Nothing "
                    "was redeemed. Run: firekeep update — and if it persists, report it."
                ),
            )
        if not _NONCE_RE.fullmatch(req.device_nonce):
            raise HTTPException(
                status_code=400,
                detail="this client sent a malformed device nonce. Nothing was redeemed.",
            )
        try:
            tid = ticket_id(req.ticket)
            outcome, fields, ticket = await enrollment_store.consume(
                ticket=req.ticket,
                credential_hash=req.credential_hash,
                device_nonce=req.device_nonce,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="malformed enrollment ticket")

        if outcome not in {"ok", "replay"}:
            status, detail = _detail(outcome, fields, ticket, tid=tid)
            if outcome == "scope_violation":
                logger.critical("Enrollment scope ceiling violated: %s", fields)
            raise HTTPException(status_code=status, detail=detail)

        if not ticket:
            raise HTTPException(status_code=500, detail="enrollment metadata disappeared")
        credential_id = fields[0]
        device_id = fields[1]
        response: dict[str, Any] = {
            "device_id": device_id,
            "credential_id": credential_id,
            "suggested_agent_id": _suggested_agent_id(ticket, req.hostname),
            "scopes": json.loads(ticket.get("scopes", "[]")),
            "kind": ticket["kind"],
            "credential_expires_at": ticket.get("issued_credential_expires_at"),
            "server_version": VERSION,
            "replay": outcome == "replay",
        }
        if ticket["kind"] == "ports":
            response["host"] = ticket["host"]
        else:
            response["base_url"] = ticket["base_url"]
        if ticket.get("ca_pem"):
            response["ca_pem"] = ticket["ca_pem"]
        return response

    # slowapi requires the Request argument on the decorated endpoint. Keep the
    # router independently testable by omitting the decorator when no limiter is
    # supplied.
    redeem_endpoint = (
        limiter.limit(get_settings().ENROLL_RATE_LIMIT)(redeem) if limiter else redeem
    )
    router.add_api_route("/enroll", redeem_endpoint, methods=["POST"])

    @router.get("/enroll/anchor")
    async def anchor(tid: str = Query(..., pattern="^[0-9a-f]{16}$")) -> dict[str, str]:
        ca_pem = await enrollment_store.anchor(tid)
        if not ca_pem:
            raise HTTPException(status_code=404, detail="enrollment anchor not found")
        return {"ca_pem": ca_pem}

    @router.post("/enroll/invite")
    async def invite(
        req: InviteRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        if not enabled:
            raise HTTPException(status_code=409, detail=AUTH_OFF_DETAIL)
        ssh_target = req.ssh_target
        if req.transport == "tunnel" and not ssh_target:
            vps_ip = os.environ.get("VPS_IP", "").strip()
            ssh_user = os.environ.get("FIREKEEP_SSH_USER", "root").strip() or "root"
            if vps_ip:
                ssh_target = f"{ssh_user}@{vps_ip}"
        if req.kind == "ports" and not req.host:
            raise HTTPException(status_code=400, detail="kind=ports requires host")
        if req.kind == "paths" and not req.base_url:
            raise HTTPException(status_code=400, detail="kind=paths requires base_url")
        if req.transport == "tls" and not (req.ca_pem or req.ca_mode == "os"):
            raise HTTPException(status_code=400, detail="t=tls requires ca_pem or ca_mode=os")
        if req.transport == "tunnel" and not ssh_target:
            raise HTTPException(status_code=400, detail="t=tunnel requires ssh_target")
        if req.transport == "http" and not req.insecure_http:
            raise HTTPException(status_code=400, detail="plain HTTP requires insecure_http=true")
        return await mint_invite(
            enrollment_store,
            agent_label=req.agent,
            transport=req.transport,
            kind=req.kind,
            host=req.host,
            base_url=req.base_url,
            ca_pem=req.ca_pem,
            ca_mode=req.ca_mode,
            ssh_target=ssh_target,
            issuer=f"credential:{identity.get('credential_id', 'admin')}",
            key_expires_days=req.expires_days,
            device_id=req.device_id,
            dist_base=req.dist_base,
        )

    @router.get("/enroll/invites")
    async def list_invites(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        invites = await enrollment_store.list_outstanding()
        return {"invites": invites, "count": len(invites)}

    @router.delete("/enroll/invites/{tid}")
    async def cancel_invite(
        tid: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, str]:
        if not _TID_RE.fullmatch(tid):
            raise HTTPException(status_code=404, detail="enrollment invite not found")
        if not await enrollment_store.cancel(tid):
            raise HTTPException(status_code=404, detail="enrollment invite not found")
        return {"status": "cancelled", "tid": tid}

    return router
