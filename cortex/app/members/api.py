"""Workspace and member-invite API.

Membership is identity and attribution, not metering: there is one Firekeep
product, and the number of members a workspace may enroll is not technically
limited. (The signed-entitlement system that used to meter seats here was
removed with the single-product conversion; the BUSL LICENSE's terms are the
only multi-member boundary, and they are legal, not enforced in code.)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.members import (
    STATUS_ACTIVE,
    STATUS_REMOVED,
    MemberRemovalError,
    remove_member,
    restore_member,
)
from auth.middleware import require_scope
from auth.workspace import MEMBER_PREFIX, Workspace

from app.enroll.advertise import advertised_host, resolve_connection
from app.enroll.api import InviteRequest
from app.enroll.mint import ca_fingerprint, encode_prepared_join, mint_invite
from app.enroll.store import EnrollmentStore

from .store import MemberInviteError, MemberStore


_TID_RE = re.compile(r"^[0-9a-f]{16}$")


class MemberInviteRequest(InviteRequest):
    label: str = Field(..., min_length=1, max_length=100)
    email: str = Field(default="", max_length=254)


class MemberAcceptRequest(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=128)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _member_code(ticket: str, record: dict[str, str]) -> str:
    payload: dict[str, Any] = {
        "v": 1,
        "t": record["transport"],
        "k": record["kind"],
        "x": datetime.fromisoformat(record["expires_at"]).strftime("%Y%m%dT%H%M%SZ"),
        "m": ticket,
    }
    if record["kind"] == "ports":
        payload["h"] = record["host"]
    else:
        payload["u"] = record["base_url"]
    if record["transport"] == "tls":
        payload["f"] = (
            "os" if record.get("ca_mode") == "os" else ca_fingerprint(record["ca_pem"])
        )
    if record["transport"] == "tunnel":
        payload["s"] = record["ssh_target"]
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    checksum = _b64url(hashlib.sha256(body.encode("ascii")).digest()[:3])
    return f"fk_member_{body}.{checksum}"


def _connection(req: MemberInviteRequest) -> dict[str, str]:
    # A member invite carries the same connection metadata as a device invite —
    # it becomes one — so it resolves an unnamed transport/host identically,
    # from what this server actually publishes rather than from an ssh tunnel.
    if not req.transport and req.kind != "ports":
        raise HTTPException(
            status_code=400, detail="kind=paths requires an explicit transport"
        )
    transport, host, server_chosen = resolve_connection(
        transport=req.transport, kind=req.kind, host=req.host
    )
    ssh_target = req.ssh_target
    if transport == "tunnel" and not ssh_target:
        vps_ip = os.getenv("VPS_IP", "").strip()
        ssh_user = os.getenv("FIREKEEP_SSH_USER", "root").strip() or "root"
        if vps_ip:
            ssh_target = f"{ssh_user}@{vps_ip}"
    if req.kind == "ports" and not host:
        raise HTTPException(
            status_code=400,
            detail=(
                "kind=ports requires host, and this server cannot name one: "
                f"{advertised_host().detail}"
            ),
        )
    if req.kind == "paths" and not req.base_url:
        raise HTTPException(status_code=400, detail="kind=paths requires base_url")
    if transport == "tls" and not (req.ca_pem or req.ca_mode == "os"):
        raise HTTPException(status_code=400, detail="t=tls requires ca_pem or ca_mode=os")
    if transport == "tunnel" and not ssh_target:
        raise HTTPException(status_code=400, detail="t=tunnel requires ssh_target")
    if transport == "http" and not (req.insecure_http or server_chosen):
        raise HTTPException(status_code=400, detail="plain HTTP requires insecure_http=true")
    return {
        "transport": transport,
        "kind": req.kind,
        "host": host,
        "base_url": req.base_url,
        "ca_pem": req.ca_pem,
        "ca_mode": req.ca_mode,
        "ssh_target": ssh_target,
        "key_expires_days": str(req.expires_days or 0),
        "dist_base": req.dist_base,
    }


def create_members_router(
    *,
    redis_client,
    workspace: Workspace,
    enrollment_store: EnrollmentStore | None = None,
) -> APIRouter:
    router = APIRouter(tags=["workspace"])
    member_store = MemberStore(
        redis_client,
        enrollment_store or EnrollmentStore(redis_client),
    )

    @router.get("/workspace")
    async def workspace_status(
        identity: dict = Depends(require_scope("memory:read")),
    ) -> dict[str, Any]:
        return {
            "workspace_id": workspace.workspace_id,
            "member_id": identity["member_id"],
            "credential_id": identity["credential_id"],
        }

    @router.get("/members")
    async def list_members(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        members = await member_store.list_members()
        invites = await member_store.list_outstanding()
        return {
            "members": members,
            "invites": invites,
            # A removed member's row stays listed (it is attribution history)
            # but is not an active member.
            "active_count": sum(
                1 for m in members if (m.get("status") or STATUS_ACTIVE) == STATUS_ACTIVE
            ),
            "outstanding_invite_count": len(invites),
        }

    @router.post("/members/invites")
    async def issue_member_invite(
        request: MemberInviteRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        ticket, tid, record = await member_store.issue(
            workspace=workspace,
            label=request.label,
            email=request.email,
            issuer=f"credential:{identity['credential_id']}",
            connection=_connection(request),
        )
        code = _member_code(ticket, record)
        dist = request.dist_base.rstrip("/")
        return {
            "code": code,
            "tid": tid,
            "member_id": record["member_id"],
            "expires_at": record["expires_at"],
            "install_command_sh": f"curl -fsSL {dist}/latest/install | FIREKEEP_JOIN={code} sh",
            "install_command_powershell": (
                f"$env:FIREKEEP_JOIN='{code}'; irm {dist}/latest/install.ps1 | iex"
            ),
        }

    @router.get("/members/invites/anchor")
    async def member_anchor(
        tid: str = Query(..., pattern="^[0-9a-f]{16}$"),
    ) -> dict[str, str]:
        ca_pem = await member_store.anchor(tid)
        if not ca_pem:
            raise HTTPException(status_code=404, detail="member invite anchor not found")
        return {"ca_pem": ca_pem}

    @router.post("/members/invites/accept")
    async def accept_member_invite(request: MemberAcceptRequest) -> dict[str, Any]:
        try:
            member, enrollment, replay = await member_store.accept(
                secret=request.ticket,
                workspace=workspace,
            )
        except MemberInviteError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        code = encode_prepared_join(
            request.ticket,
            enrollment,
            ca_mode=enrollment.get("ca_mode", ""),
        )
        return {
            "workspace_id": workspace.workspace_id,
            "membership": member,
            "join_code": code,
            "replay": replay,
        }

    @router.delete("/members/invites/{tid}")
    async def cancel_member_invite(
        tid: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, str]:
        if not _TID_RE.fullmatch(tid) or not await member_store.cancel(tid):
            raise HTTPException(status_code=404, detail="member invite not found")
        return {"status": "cancelled", "tid": tid}

    # Removal and restore (docs/THREAT-MODEL.md §5.17). The member comes from
    # the PATH, never from a body: InviteRequest deliberately has no member_id
    # field (enroll/api.py), and restore's body is exactly that model.
    # `/members/invites/{tid}` cannot collide: DELETE /members/invites (no tid)
    # would read "invites" as a member id and 404 like any unknown member.

    @router.delete("/members/{member_id}")
    async def remove_workspace_member(
        member_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        try:
            return await remove_member(
                redis_client,
                member_id,
                workspace_id=workspace.workspace_id,
                owner_member_id=workspace.owner_member_id,
                removed_by=f"credential:{identity.get('credential_id', 'admin')}",
                actor_member_id=identity.get("member_id"),
            )
        except MemberRemovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @router.post("/members/{member_id}/restore")
    async def restore_workspace_member(
        member_id: str,
        request: InviteRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Reactivate a REMOVED member and mint one device join code for them.

        Removal deleted every credential, so a restored member with no code
        could never authenticate again. The code registers its credential for
        the restored member, not for the admin who clicked. Only a removed
        member qualifies: this is not a way to mint codes for active members.
        """
        # Validate the connection before changing anything, so a bad request
        # never leaves a member restored without a code.
        connection = _connection(
            MemberInviteRequest(label="restore", **request.model_dump())
        )
        row = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
        if row and row.get("workspace_id") == workspace.workspace_id and (
            row.get("status") != STATUS_REMOVED
        ):
            raise HTTPException(
                status_code=409,
                detail=f"member {member_id} is not removed; only a removed member can be restored",
            )
        issuer = f"credential:{identity.get('credential_id', 'admin')}"
        try:
            membership = await restore_member(
                redis_client,
                member_id,
                workspace_id=workspace.workspace_id,
                restored_by=issuer,
            )
        except MemberRemovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        minted = await mint_invite(
            member_store.enrollment,
            agent_label=membership.get("label", ""),
            transport=connection["transport"],
            kind=connection["kind"],
            host=connection["host"],
            base_url=connection["base_url"],
            ca_pem=connection["ca_pem"],
            ca_mode=connection["ca_mode"],
            ssh_target=connection["ssh_target"],
            issuer=issuer,
            member_id=member_id,
            key_expires_days=request.expires_days,
            dist_base=connection["dist_base"],
        )
        return {"member_id": member_id, "membership": membership, **minted}

    return router
