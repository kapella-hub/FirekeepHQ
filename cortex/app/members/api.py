"""Workspace, member-invite, and offline licence API."""

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

from auth.entitlements import (
    LICENCE_REDIS_KEY,
    load_entitlement,
    verify_licence,
)
from auth.middleware import require_scope
from auth.workspace import Workspace

from app.enroll.api import InviteRequest
from app.enroll.mint import ca_fingerprint, encode_prepared_join
from app.enroll.store import EnrollmentStore

from .store import MemberInviteError, MemberStore, SeatLimitError


_TID_RE = re.compile(r"^[0-9a-f]{16}$")


class MemberInviteRequest(InviteRequest):
    label: str = Field(..., min_length=1, max_length=100)
    email: str = Field(default="", max_length=254)


class MemberAcceptRequest(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=128)


class LicenceApplyRequest(BaseModel):
    document: str = Field(..., min_length=1, max_length=8192)


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
    ssh_target = req.ssh_target
    if req.transport == "tunnel" and not ssh_target:
        vps_ip = os.getenv("VPS_IP", "").strip()
        ssh_user = os.getenv("FIREKEEP_SSH_USER", "root").strip() or "root"
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
    return {
        "transport": req.transport,
        "kind": req.kind,
        "host": req.host,
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
        entitlement = await load_entitlement(redis_client, workspace.workspace_id)
        return {
            "workspace_id": workspace.workspace_id,
            "member_id": identity["member_id"],
            "credential_id": identity["credential_id"],
            "entitlement": entitlement.as_dict(),
        }

    @router.get("/licence")
    async def licence_status(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        return (await load_entitlement(redis_client, workspace.workspace_id)).as_dict()

    @router.post("/licence")
    async def apply_licence(
        request: LicenceApplyRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        if os.getenv("FIREKEEP_LICENCE", "").strip():
            raise HTTPException(
                status_code=409,
                detail=(
                    "FIREKEEP_LICENCE is set in the server environment and overrides "
                    "dashboard/admin licences; update that secret instead"
                ),
            )
        entitlement = verify_licence(
            request.document,
            workspace.workspace_id,
            source="redis",
        )
        if not entitlement.verified:
            raise HTTPException(status_code=400, detail=entitlement.reason)
        await redis_client.set(LICENCE_REDIS_KEY, request.document.strip())
        return entitlement.as_dict()

    @router.delete("/licence")
    async def remove_licence(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        if os.getenv("FIREKEEP_LICENCE", "").strip():
            raise HTTPException(
                status_code=409,
                detail="FIREKEEP_LICENCE is set in the server environment; remove it there",
            )
        await redis_client.delete(LICENCE_REDIS_KEY)
        return (await load_entitlement(redis_client, workspace.workspace_id)).as_dict()

    @router.get("/members")
    async def list_members(
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        entitlement = await load_entitlement(redis_client, workspace.workspace_id)
        members = await member_store.list_members()
        invites = await member_store.list_outstanding()
        return {
            "members": members,
            "invites": invites,
            "active_count": len(members),
            "outstanding_invite_count": len(invites),
            "entitlement": entitlement.as_dict(),
        }

    @router.post("/members/invites")
    async def issue_member_invite(
        request: MemberInviteRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        entitlement = await load_entitlement(redis_client, workspace.workspace_id)
        try:
            ticket, tid, record = await member_store.issue(
                workspace=workspace,
                entitlement=entitlement,
                label=request.label,
                email=request.email,
                issuer=f"credential:{identity['credential_id']}",
                connection=_connection(request),
            )
        except SeatLimitError as exc:
            raise HTTPException(status_code=403, detail=exc.detail()) from exc
        code = _member_code(ticket, record)
        dist = request.dist_base.rstrip("/")
        return {
            "code": code,
            "tid": tid,
            "member_id": record["member_id"],
            "expires_at": record["expires_at"],
            "install_command_sh": f"curl -fsSL {dist}/latest/install.sh | FIREKEEP_JOIN={code} sh",
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
        entitlement = await load_entitlement(redis_client, workspace.workspace_id)
        try:
            member, enrollment, replay = await member_store.accept(
                secret=request.ticket,
                workspace=workspace,
                entitlement=entitlement,
            )
        except SeatLimitError as exc:
            raise HTTPException(status_code=403, detail=exc.detail()) from exc
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
            "entitlement": entitlement.as_dict(),
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

    return router
