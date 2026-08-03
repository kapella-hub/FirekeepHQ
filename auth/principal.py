"""Canonical verified-principal helpers shared by Firekeep services."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping


_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DEFAULT_WORKSPACE_ID = "workspace-local"
_DEFAULT_OWNER_MEMBER_ID = "member-owner"


def _deployment_id(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip() or fallback
    if not _ID_RE.fullmatch(value):
        raise RuntimeError(f"{name} must be 1-128 alphanumeric/._- characters")
    return value


def deployment_workspace_id() -> str:
    return _deployment_id("FIREKEEP_WORKSPACE_ID", _DEFAULT_WORKSPACE_ID)


def deployment_owner_member_id() -> str:
    return _deployment_id("FIREKEEP_OWNER_MEMBER_ID", _DEFAULT_OWNER_MEMBER_ID)


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
