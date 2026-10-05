"""Canonical verified-principal helpers shared by Firekeep services."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)


_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DEFAULT_WORKSPACE_ID = "workspace-local"
_DEFAULT_OWNER_MEMBER_ID = "member-owner"
_CREDENTIAL_ID_RE = re.compile(r"^[0-9a-f]{1,64}$")

# The delegated-write contract (POST /memory/learn/delegated). A service that
# writes on a member's behalf names that member -- and, when it knows it, the
# credential that member acted through -- in these headers. Only a key holding
# DELEGATED_WRITE_SCOPE literally may use them; /memory/learn refuses them.
DELEGATED_MEMBER_HEADER = "X-Firekeep-Delegated-Member-Id"
DELEGATED_CREDENTIAL_HEADER = "X-Firekeep-Delegated-Credential-Id"
DELEGATED_WRITE_SCOPE = "memory:write:delegated"


class DelegatedAttributionError(ValueError):
    """A delegated write named an author the auth store cannot verify."""


def _deployment_id(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip() or fallback
    if not _ID_RE.fullmatch(value):
        raise RuntimeError(f"{name} must be 1-128 alphanumeric/._- characters")
    return value


def deployment_workspace_id() -> str:
    return _deployment_id("FIREKEEP_WORKSPACE_ID", _DEFAULT_WORKSPACE_ID)


def deployment_owner_member_id() -> str:
    return _deployment_id("FIREKEEP_OWNER_MEMBER_ID", _DEFAULT_OWNER_MEMBER_ID)


def owns_session(
    owner_member: str | None,
    owner_workspace: str | None,
    *,
    member_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Does the member ``member_id`` in ``workspace_id`` own a Bridge session
    whose recorded owner is ``owner_member`` / ``owner_workspace``?

    THE session-ownership rule. Bridge's ``session_owned_by`` (every MCP tool
    and REST route) and Cortex's session-owner resolver (POST /skill/evaluate,
    replay attribution) both call it, so "owner" means one thing everywhere:

    - a bound session (``owner_member`` non-empty) belongs to exactly that
      member and, when ``owner_workspace`` was recorded (sessions started after
      2026-10-01), only within that workspace;
    - a LEGACY session (no ``owner_member``) belongs to the deployment owner
      member in the deployment workspace, and to no one else;
    - no member (no verified principal) owns nothing.
    """
    if not member_id:
        return False
    if owner_member:
        if owner_member != member_id:
            return False
        return not owner_workspace or owner_workspace == workspace_id
    return (
        member_id == deployment_owner_member_id()
        and workspace_id == deployment_workspace_id()
    )


def anonymous_principal() -> dict[str, Any]:
    """Principal for the auth-disabled, single-workspace convenience mode."""
    from auth.keys import ANONYMOUS_SCOPES

    return {
        "workspace_id": deployment_workspace_id(),
        "member_id": deployment_owner_member_id(),
        "credential_id": "anonymous",
        "scopes": list(ANONYMOUS_SCOPES),
        "authenticated": False,
    }


def principal_from_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Return the verified request principal from one canonical accessor.

    When authentication is disabled no middleware is installed, so the
    deployment owner principal is returned. With authentication enabled, a
    missing attached identity is a wiring error and fails closed.
    """
    identity = scope.get("state", {}).get("identity")
    if identity is not None:
        return identity

    from auth.config import get_auth_settings

    if not get_auth_settings().ENABLED:
        return anonymous_principal()
    raise RuntimeError(
        "No verified principal attached while authentication is enabled"
    )


def request_principal(request) -> dict[str, Any]:
    """FastAPI/Starlette-compatible wrapper around :func:`principal_from_scope`."""
    return principal_from_scope(request.scope)


# ---------------------------------------------------------------------------
# Write provenance (2026-10-04)
# ---------------------------------------------------------------------------


def runtime_id_for(credential_id: str, runtime_label: str) -> str:
    """Stable correlation id for a runtime label used through ONE credential.

    The label (``X-Agent-Id``) is chosen by the client; namespacing it by the
    verified credential keeps two members who both call their agent
    ``claude`` from becoming one actor. With no verified credential there is
    no correlation id at all ("") -- never a label-only id.
    """
    if not credential_id:
        return ""
    digest = hashlib.sha256(
        f"{credential_id}\0{runtime_label}".encode("utf-8")
    ).hexdigest()[:24]
    return f"runtime-{digest}"


def _header(request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


def request_attribution(request) -> dict[str, Any]:
    """Who wrote this: the verified principal, plus the asserted runtime label.

    workspace/member/credential come only from :func:`request_principal`.
    ``X-Agent-Id`` and ``X-Session-Id`` are recorded as what they are -- a
    display label and a correlation handle -- and select nothing.
    """
    principal = request_principal(request)
    credential_id = str(principal.get("credential_id") or "")
    runtime_label = _header(request, "X-Agent-Id") or "unknown"
    return {
        "workspace_id": principal["workspace_id"],
        "member_id": principal["member_id"],
        "credential_id": credential_id,
        "runtime_label": runtime_label,
        "runtime_id": runtime_id_for(credential_id, runtime_label),
        "session_id": _header(request, "X-Session-Id") or "unknown",
        "delegated_by_credential_id": None,
    }


def has_delegated_attribution_headers(request) -> bool:
    """Does the request try to use the delegated-write contract?"""
    return bool(
        _header(request, DELEGATED_MEMBER_HEADER)
        or _header(request, DELEGATED_CREDENTIAL_HEADER)
    )


async def delegated_attribution(
    request,
    service_identity: Mapping[str, Any],
    *,
    redis_client,
) -> dict[str, Any]:
    """Verify the author a SERVICE names for a write it makes on their behalf.

    Authorization stays with ``service_identity`` (the key that arrived); only
    the provenance changes, and only after the auth store (Redis DB 7) confirms:

    - the service holds ``memory:write:delegated`` LITERALLY -- a ``*`` key does
      not pass (the dashboard key is ``*`` and nginx injects it into every
      browser request);
    - the named member is an ACTIVE member of the service's OWN workspace
      (``auth:member:<id>``; the owner row is guaranteed by ensure_workspace);
    - a named credential, if it still resolves, belongs to that same member
      and workspace. One that no longer resolves (revoked or expired since the
      session started) is dropped -- recorded as "" -- rather than trusted or
      allowed to strand the member's write.

    Raises DelegatedAttributionError on anything else. Callers must answer
    every failure identically, so the route is not a member/credential oracle.
    """
    from auth.keys import validate_key_by_hash

    scopes = service_identity.get("scopes") or []
    if DELEGATED_WRITE_SCOPE not in scopes:
        raise DelegatedAttributionError("service key lacks memory:write:delegated")
    if redis_client is None:
        raise DelegatedAttributionError("auth store unavailable")

    member_id = _header(request, DELEGATED_MEMBER_HEADER)
    credential_id = _header(request, DELEGATED_CREDENTIAL_HEADER)
    if not member_id or not _ID_RE.fullmatch(member_id):
        raise DelegatedAttributionError("delegated member missing or malformed")
    if credential_id and not _CREDENTIAL_ID_RE.fullmatch(credential_id):
        raise DelegatedAttributionError("delegated credential malformed")

    workspace_id = str(service_identity.get("workspace_id") or "")
    if not workspace_id:
        raise DelegatedAttributionError("service key has no workspace")

    member = await redis_client.hgetall(f"auth:member:{member_id}")
    if not member:
        raise DelegatedAttributionError("delegated member unknown")
    if (member.get("workspace_id") or deployment_workspace_id()) != workspace_id:
        raise DelegatedAttributionError("delegated member is in another workspace")
    if (member.get("status") or "active") != "active":
        raise DelegatedAttributionError("delegated member is not active")

    verified_credential = ""
    if credential_id:
        key_hash = await redis_client.get(f"auth:cred:{credential_id}")
        identity = (
            await validate_key_by_hash(key_hash, redis_client) if key_hash else None
        )
        if identity is not None and identity.get("credential_id") == credential_id:
            if (
                identity.get("member_id") != member_id
                or identity.get("workspace_id") != workspace_id
            ):
                raise DelegatedAttributionError(
                    "delegated credential belongs to another member"
                )
            verified_credential = credential_id
        else:
            logger.warning(
                "Delegated credential %s no longer resolves; recording the "
                "write for member %s without a credential",
                credential_id, member_id,
            )

    runtime_label = _header(request, "X-Agent-Id") or "unknown"
    return {
        "workspace_id": workspace_id,
        "member_id": member_id,
        "credential_id": verified_credential,
        "runtime_label": runtime_label,
        "runtime_id": runtime_id_for(verified_credential, runtime_label),
        "session_id": _header(request, "X-Session-Id") or "unknown",
        "delegated_by_credential_id": str(service_identity.get("credential_id") or ""),
    }
