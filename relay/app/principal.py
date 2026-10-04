"""The verified principal behind a Relay request, and who owns what.

THREAT-MODEL §5.14 (2026-10-04). Relay's tools and routes used to take
identity from an argument — ``agent_id``, ``from_id``, ``sender``, ``author``
— so any key could read another member's DMs, release her lease, or
deregister her presence. Ownership is now the verified ``(workspace_id,
member_id)`` FirekeepKeyAuthMiddleware attached to the request, recorded on
every presence row, DM, lease, claim, bulletin, broadcast and scope session
at write time. The label stays a display / routing field.

Rules, in one place:

- **Auth disabled** (personal mode): every caller is the anonymous deployment
  owner and ownership is not enforced at all, so that mode behaves exactly as
  it did before this change.
- **Auth enabled, no identity attached**: no caller (``None``) — every gated
  operation refuses. A wiring fault fails closed.
- **Legacy records** (written before owners were recorded) belong to the
  deployment owner member alone (``auth.principal.deployment_owner_member_id``)
  — the policy Bridge's ``session_owned_by`` uses. Presence is the one
  deliberate exception; see ``app.presence``.
- **Admin** (a key whose scopes allow ``admin``; the dashboard's ``["*"]``
  key) may read and remove any member's records inside its own workspace: the
  dashboard is the owner's admin surface. It never becomes the owner — Relay
  does not write into Bridge with an admin's key on a member's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from auth.config import get_auth_settings
from auth.principal import deployment_owner_member_id, deployment_workspace_id

OWNER_WORKSPACE = "owner_workspace"
OWNER_MEMBER = "owner_member"


class RelayAccessError(ValueError):
    """The verified caller may not act on the record it named.

    A ValueError so the existing ``except ValueError`` surfaces turn it into an
    error dict / 4xx without a traceback. The message never names the owner.
    """


@dataclass(frozen=True)
class Caller:
    workspace_id: str
    member_id: str
    credential_id: str
    authenticated: bool
    admin: bool

    def stamp(self) -> dict:
        """The audit stamp recorded on writes (same shape as task ``created_by``)."""
        return {
            "workspace_id": self.workspace_id,
            "member_id": self.member_id,
            "credential_id": self.credential_id,
            "authenticated": self.authenticated,
        }

    def owner_fields(self) -> dict:
        return {OWNER_WORKSPACE: self.workspace_id, OWNER_MEMBER: self.member_id}


def caller_from_scope(scope: Mapping[str, Any] | None) -> Caller | None:
    """The caller behind an ASGI scope, or None when auth is on and nothing
    verified it. Reads enabled-ness from AuthSettings — the same truth
    build_auth_middleware reads (auth/asgi.py explains why not keys._AUTH_ENABLED)."""
    from auth.keys import scopes_allow

    identity = ((scope or {}).get("state") or {}).get("identity")
    if identity is not None:
        member = str(identity.get("member_id") or "")
        if not member:
            return None
        return Caller(
            workspace_id=str(identity.get("workspace_id") or deployment_workspace_id()),
            member_id=member,
            credential_id=str(identity.get("credential_id") or ""),
            authenticated=True,
            admin=scopes_allow(identity.get("scopes") or [], "admin"),
        )
    if get_auth_settings().ENABLED:
        return None
    return Caller(
        workspace_id=deployment_workspace_id(),
        member_id=deployment_owner_member_id(),
        credential_id="anonymous",
        authenticated=False,
        admin=False,
    )


def record_workspace(record: Mapping[str, Any] | None) -> str:
    return str((record or {}).get(OWNER_WORKSPACE) or deployment_workspace_id())


def is_bound(record: Mapping[str, Any] | None) -> bool:
    return bool((record or {}).get(OWNER_MEMBER))


def owns(record: Mapping[str, Any] | None, caller: Caller | None) -> bool:
    """Does ``caller`` own ``record`` (a hash / JSON object carrying the owner fields)?"""
    if caller is None:
        return False
    if not caller.authenticated:
        return True
    record = record or {}
    owner_member = record.get(OWNER_MEMBER) or ""
    if owner_member:
        return owner_member == caller.member_id and record_workspace(record) == caller.workspace_id
    return (
        caller.member_id == deployment_owner_member_id()
        and caller.workspace_id == deployment_workspace_id()
    )


def administers(record: Mapping[str, Any] | None, caller: Caller | None) -> bool:
    """Owner, or an admin key inside the record's workspace."""
    if owns(record, caller):
        return True
    return bool(caller and caller.admin and record_workspace(record) == caller.workspace_id)


def is_deployment_owner(caller: Caller | None) -> bool:
    return bool(
        caller
        and caller.member_id == deployment_owner_member_id()
        and caller.workspace_id == deployment_workspace_id()
    )
