"""Shared pure-ASGI API-key validator (SP1a §4.1).

NO fastapi imports allowed here: bridge ships fastmcp/starlette only.
Injected via mcp.run(..., middleware=build_auth_middleware(...)) on the four
FastMCP services (wraps the /mcp endpoint AND every @mcp.custom_route —
verified against fastmcp 3.1.1) and via app.add_middleware(
FirekeepKeyAuthMiddleware, ...) on Cortex REST, replacing the legacy
APIKeyMiddleware.

Fail mode (Reliability Principle, SP1a §2): when enabled and the Redis key
store is unreachable, requests get a loud 503 — never a silent pass-through.
Compose healthchecks are TCP-only, so a container stays "healthy" during a
fail-closed outage: the loudness surfaces in logs and the 503 body.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from auth.config import get_auth_settings
from auth.keys import invalid_credential_detail, validate_key

logger = logging.getLogger(__name__)

DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/health",)
DEFAULT_SKIP_EXACT_PATHS: tuple[str, ...] = ()


class FirekeepKeyAuthMiddleware:
    """Whole-app X-API-Key gate backed by Redis DB 7 (auth:key:{sha256}).

    On success the verified identity is attached to
    scope["state"]["identity"] = {"agent_id", "scopes", "key_id"} so
    downstream handlers can trust it over self-declared X-Agent-Id.
    """

    def __init__(
        self,
        app,
        *,
        enabled: bool,
        redis_url: str,
        skip_paths: tuple[str, ...] = DEFAULT_SKIP_PATHS,
        skip_exact_paths: tuple[str, ...] = DEFAULT_SKIP_EXACT_PATHS,
        redis_client=None,
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.redis_url = redis_url
        self.skip_paths = tuple(skip_paths)
        # Exact-match skip list: use this for a single path that must bypass
        # auth WITHOUT exempting everything nested under it via prefix match
        # (skip_paths does `path.startswith(prefix)` — a bare "/foo" prefix
        # there silently exempts "/foo/api/secret-data" too). See Cortex's
        # dashboard wiring in app/main.py for the motivating case.
        self.skip_exact_paths = tuple(skip_exact_paths)
        # Test seam: pass a client explicitly; production lazily creates one.
        self._redis = redis_client

    def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.skip_exact_paths:
            await self.app(scope, receive, send)
            return
        if any(path.startswith(prefix) for prefix in self.skip_paths):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace")
        if not api_key:
            await self._reject(scope, receive, send, 401, "Missing X-API-Key header")
            return

        try:
            redis_client = self._get_redis()
            identity = await validate_key(api_key, redis_client=redis_client)
            invalid_detail = (
                await invalid_credential_detail(api_key, redis_client=redis_client)
                if identity is None
                else None
            )
        except Exception as exc:
            # Fail CLOSED and loud: auth store down while enforcement is on.
            logger.error(
                "AUTH STORE UNREACHABLE — failing closed with 503 (path=%s): %s",
                path,
                exc,
            )
            await self._reject(
                scope,
                receive,
                send,
                503,
                "Auth key store unreachable — failing closed. Check Redis DB 7.",
            )
            return

        if identity is None:
            await self._reject(scope, receive, send, 401, invalid_detail or "Unknown API key")
            return

        state: dict[str, Any] = scope.setdefault("state", {})
        state["identity"] = {
            "agent_id": identity["agent_id"],
            "scopes": identity["scopes"],
            "key_id": identity["key_id"],
        }
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, status: int, detail: str) -> None:
        response = JSONResponse({"detail": detail}, status_code=status)
        await response(scope, receive, send)


def build_auth_middleware(
    settings,
    skip_paths: tuple[str, ...] = DEFAULT_SKIP_PATHS,
    skip_exact_paths: tuple[str, ...] = DEFAULT_SKIP_EXACT_PATHS,
) -> list[Middleware]:
    """Middleware list for mcp.run(...) / Starlette from AuthSettings.

    Returns [] when auth is disabled (personal-VPS default) so services run
    exactly as today; the office instance opts in via AUTH_ENABLED=true.
    """
    if not settings.ENABLED:
        return []
    return [
        Middleware(
            FirekeepKeyAuthMiddleware,
            enabled=True,
            redis_url=settings.REDIS_URL,
            skip_paths=tuple(skip_paths),
            skip_exact_paths=tuple(skip_exact_paths),
        )
    ]


class ScopeError(Exception):
    """Raised by require_scope_asgi when the caller's key lacks the required scope."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def require_scope_asgi(request, scope: str) -> dict:
    """Starlette-level scope check for @mcp.custom_route handlers.

    require_scope() (auth/middleware.py) is a FastAPI Depends and cannot run
    on FastMCP's @mcp.custom_route handlers, which are plain Starlette. This
    reads the identity FirekeepKeyAuthMiddleware already attached to
    request.scope['state']['identity'] and checks scopes there instead.

    Enabled-ness derives from AuthSettings (the AUTH_ENABLED env var) — the
    SAME truth build_auth_middleware reads — NOT from keys._AUTH_ENABLED,
    which only init_auth() sets and only the Cortex REST lifespan calls: in
    the FastMCP service processes (bridge/relay/sentinel) that flag is
    permanently False, which made every scope gate here pass anonymously
    until the 2026-07-16 fix. When auth is disabled, the caller becomes the
    anonymous identity and is scope-checked like any other — mirroring
    require_scope(); non-admin scopes pass, "admin" is refused (audit blocker
    7). When auth IS enabled but no identity was attached (e.g. this route sits
    under FirekeepKeyAuthMiddleware's skip_paths, or the middleware isn't wired
    into this app), this fails closed with a 401 rather than silently granting
    anonymous/wildcard access.
    """
    from auth import keys as _keys

    if not get_auth_settings().ENABLED:
        # Same correction as require_scope(): this used to return the anonymous
        # identity WITHOUT consulting `scope`, so Bridge's admin-only
        # POST /ops/distill-dlq/requeue was open to anyone on the port whenever
        # auth was off. Wildcard is not honoured on this path — see
        # keys.scopes_allow.
        anon = _keys._ANONYMOUS_IDENTITY
        if not _keys.scopes_allow(anon["scopes"], scope, allow_wildcard=False):
            raise ScopeError(403, _keys.anonymous_denied_detail(scope))
        return anon

    identity = request.scope.get("state", {}).get("identity")
    if identity is None:
        raise ScopeError(401, "No identity attached — auth is enabled but this route is not covered by FirekeepKeyAuthMiddleware")

    # Wildcard IS honoured for a real key: the owner/dashboard keys carry ["*"].
    scopes = identity.get("scopes", [])
    if not _keys.scopes_allow(scopes, scope):
        raise ScopeError(403, f"Insufficient scope: requires '{scope}', key has {scopes}")
    return identity
