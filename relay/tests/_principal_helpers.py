"""Shared doubles for the verified-principal tests (2026-10-04 relay binding).

A real Starlette ``Request`` stands in for the HTTP request FastMCP exposes
through ``get_http_request()``: it carries the identity
FirekeepKeyAuthMiddleware attaches under ``scope["state"]["identity"]`` and a
real, case-insensitive ``headers`` mapping (so the raw ``X-API-Key`` Relay
forwards to Bridge is read exactly as production reads it).
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from starlette.requests import Request

WORKSPACE = "workspace-local"     # auth.principal default deployment workspace
OWNER = "member-owner"            # auth.principal default deployment owner


def identity(member_id: str, credential_id: str, scopes=("relay:read", "relay:write"),
             *, workspace_id: str = WORKSPACE) -> dict:
    return {
        "workspace_id": workspace_id,
        "member_id": member_id,
        "credential_id": credential_id,
        "scopes": list(scopes),
        "authenticated": True,
    }


ALICE = identity("member-alice", "cred-alice")
BOB = identity("member-bob", "cred-bob")
OWNER_AGENT = identity(OWNER, "cred-owner-laptop")
DASHBOARD = identity(OWNER, "cred-dashboard", ("*",))

KEYS = {
    "cred-alice": "nxs_alice",
    "cred-bob": "nxs_bob",
    "cred-owner-laptop": "nxs_owner",
    "cred-dashboard": "nxs_dashboard",
}


def make_request(ident: dict | None, *, method: str = "POST", path: str = "/mcp",
                 path_params: dict | None = None, body: dict | None = None,
                 query: dict | None = None, x_agent_id: str = "spoofed-label") -> Request:
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    headers = [
        (b"content-type", b"application/json"),
        (b"x-agent-id", x_agent_id.encode("utf-8")),
    ]
    if ident is not None:
        headers.append((b"x-api-key", KEYS[ident["credential_id"]].encode("utf-8")))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "path_params": path_params or {},
        "query_string": urlencode(query or {}).encode("ascii"),
        "state": {"identity": ident} if ident is not None else {},
    }
    return Request(scope, receive)


def enable_auth(monkeypatch) -> None:
    """Auth ENABLED for every reader of auth.config.get_auth_settings()."""
    import auth.config as auth_config
    from auth.config import AuthSettings

    monkeypatch.setattr(auth_config, "_settings", AuthSettings(ENABLED=True))
