"""Firekeep Auth — key primitives (fastapi-free).

This module owns key generation, storage, and validation so that pure-ASGI
consumers (auth/asgi.py — injected into the FastMCP services, whose images
ship starlette via fastmcp but NOT fastapi) can import it. auth/middleware.py
re-exports everything here and adds the FastAPI-only require_scope dependency
on top. Both layers read the same Redis DB 7 store.

API keys are stored as SHA-256 hashes (plaintext never stored):
  auth:key:{sha256hex} -> hash {agent_id, scopes (JSON), created_at, key_id, expires_at?}
  auth:key_index       -> zset of key_id (first 16 hash chars) scored by creation ts
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scope definitions
# ---------------------------------------------------------------------------

SCOPES = {
    "memory:read",
    "memory:write",
    "session:read",
    "session:write",
    "replay:read",
    "relay:read",
    "relay:write",
    "eval:read",
    "eval:write",  # POST /agent/action/after (cortex/app/agent_gateway/api.py:43)
    "admin",
}
# NOTE: memory:*/session:*/relay:* are not yet demanded by any route —
# reserved for SP4 per-route enforcement. Do not delete (SP1a §4.2).
# twin:read was removed 2026-07: the twin module is deleted; the scope dangled.

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_AUTH_ENABLED = False  # Set via init_auth()
_redis = None          # Redis DB 7 client, set via init_auth()

_KEY_PREFIX = "auth:key:"
_KEY_INDEX = "auth:key_index"  # sorted set of key IDs

# ---------------------------------------------------------------------------
# Anonymous identity (the AUTH_ENABLED=false path)
# ---------------------------------------------------------------------------

# The scope set handed to a caller who presented no key because enforcement is
# off. DERIVED from SCOPES rather than listed literally, so a scope added later
# is granted automatically instead of being silently withheld — but "admin" is
# subtracted unconditionally, and "*" cannot sneak in from SCOPES either (it is
# not a member; the explicit subtraction below states the intent regardless).
#
# WHY not []: with auth disabled the product must still work for a single user
# who has done no key management at all, and an empty set 403s every
# require_scope route — memory, sessions, replay, evals, the lot.
#
# WHY not ["*"] (what shipped until audit blocker 7): "admin" IS the exposure.
# It is the scope gating decrypted secret reads (vault/api.py) and API-key
# minting (auth/api.py), and granting it to every anonymous caller is what put
# 12 real secrets from the author's VPS on the public internet. Everything
# except the keys to the kingdom is the line.
ANONYMOUS_SCOPES: tuple[str, ...] = tuple(sorted(SCOPES - {"admin", "*"}))

_ANONYMOUS_IDENTITY = {
    "agent_id": "anonymous",
    "scopes": list(ANONYMOUS_SCOPES),
    "authenticated": False,
}


def scopes_allow(scopes, required: str, *, allow_wildcard: bool = True) -> bool:
    """Does `scopes` satisfy `required`?

    allow_wildcard=True is the AUTHENTICATED path: the keys
    deploy/bootstrap-keys.sh mints for the owner and the dashboard carry ["*"]
    and must keep passing every gate, admin included.

    allow_wildcard=False is the AUTH-DISABLED path. A "*" reaching that path
    could only come from a regression in ANONYMOUS_SCOPES above, and honouring
    it would silently re-open the vault and key-minting surfaces this module
    exists to close — so a caller that never presented a key gets a plain
    membership test, never a wildcard.
    """
    if allow_wildcard and "*" in scopes:
        return True
    return required in scopes


def anonymous_denied_detail(required: str) -> str:
    """403 body for an anonymous caller refused `required` (auth disabled).

    By construction "admin" is the only scope this can fire for. Operators meet
    this message on a default-configured box — the dashboard's DLQ Requeue
    button, a vault_store MCP call — so it has to say what is wrong AND how to
    fix it, not just "forbidden".
    """
    return (
        f"Insufficient scope: requires '{required}'. Auth is disabled "
        "(AUTH_ENABLED=false), so every caller is the anonymous identity, and "
        f"the anonymous identity is deliberately never granted '{required}' — "
        "it gates decrypted secret reads (/vault/*) and API-key minting "
        "(/auth/*). To use admin routes: set AUTH_ENABLED=true, restart, and "
        "send the admin key as X-API-Key. deploy/bootstrap-keys.sh (run by "
        "install.sh/update.sh) mints that key and prints it exactly once."
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def init_auth(redis_client=None, enabled: bool = False) -> None:
    """Initialize the auth system.

    Args:
        redis_client: Async Redis client connected to DB 7.
        enabled: Whether to enforce auth checks.
    """
    global _AUTH_ENABLED, _redis
    _AUTH_ENABLED = enabled
    _redis = redis_client
    if enabled:
        logger.info("Auth enforcement ENABLED")
    else:
        logger.info("Auth enforcement DISABLED (pass-through)")


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def _hash_key(api_key: str) -> str:
    """SHA-256 hash of an API key. Never store plaintext."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a cryptographically secure API key.

    Format: nxs_{48 random hex chars} (52 chars total).
    """
    return f"nxs_{secrets.token_hex(24)}"


async def create_key(
    agent_id: str,
    scopes: list[str],
    expires_days: int | None = None,
) -> dict[str, Any]:
    """Create a new API key.

    Returns the plaintext key (only shown once) and metadata.
    """
    if _redis is None:
        raise RuntimeError("Auth not initialized")

    # Validate scopes
    invalid = set(scopes) - SCOPES - {"*"}
    if invalid:
        raise ValueError(f"Invalid scopes: {invalid}")

    api_key = generate_api_key()
    key_hash = _hash_key(api_key)
    now = datetime.now(timezone.utc)

    metadata = {
        "agent_id": agent_id,
        "scopes": json.dumps(scopes),
        "created_at": now.isoformat(),
        "key_id": key_hash[:16],  # Short ID for listing/revocation
    }
    if expires_days:
        metadata["expires_at"] = (now + timedelta(days=expires_days)).isoformat()

    redis_key = f"{_KEY_PREFIX}{key_hash}"
    ttl = expires_days * 86400 if expires_days else None

    if ttl:
        await _redis.hset(redis_key, mapping=metadata)
        await _redis.expire(redis_key, ttl)
    else:
        await _redis.hset(redis_key, mapping=metadata)

    # Add to index for listing
    await _redis.zadd(_KEY_INDEX, {key_hash[:16]: now.timestamp()})

    return {
        "api_key": api_key,  # Only returned at creation time
        "key_id": key_hash[:16],
        "agent_id": agent_id,
        "scopes": scopes,
        "created_at": now.isoformat(),
        "expires_at": metadata.get("expires_at"),
    }


async def list_keys(limit: int = 50) -> list[dict[str, Any]]:
    """List all API keys (metadata only, never plaintext)."""
    if _redis is None:
        return []

    key_ids = await _redis.zrevrange(_KEY_INDEX, 0, limit - 1)
    keys = []
    for kid in key_ids:
        # Find the full hash — scan for keys matching this prefix
        async for full_key in _redis.scan_iter(f"{_KEY_PREFIX}{kid}*", count=10):
            data = await _redis.hgetall(full_key)
            if data:
                keys.append({
                    "key_id": data.get("key_id", kid),
                    "agent_id": data.get("agent_id", "unknown"),
                    "scopes": json.loads(data.get("scopes", "[]")),
                    "created_at": data.get("created_at"),
                    "expires_at": data.get("expires_at"),
                })
            break
    return keys


async def revoke_key(key_id: str) -> bool:
    """Revoke an API key by its short ID."""
    if _redis is None:
        return False

    # Find and delete the key
    async for full_key in _redis.scan_iter(f"{_KEY_PREFIX}{key_id}*", count=10):
        await _redis.delete(full_key)
        await _redis.zrem(_KEY_INDEX, key_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def validate_key(api_key: str, redis_client=None) -> dict[str, Any] | None:
    """Validate an API key and return its identity, or None if invalid.

    Args:
        api_key: The plaintext key from the X-API-Key header.
        redis_client: Optional explicit DB 7 client. The ASGI middleware
            (auth/asgi.py) passes its own lazily-created client because the
            FastMCP services never call init_auth(); when omitted, falls back
            to the module-global client set by init_auth().

    Redis errors PROPAGATE — the caller decides the fail mode (the ASGI
    middleware fails closed with 503 per the Reliability Principle).
    """
    client = redis_client if redis_client is not None else _redis
    if client is None:
        return None

    key_hash = _hash_key(api_key)
    redis_key = f"{_KEY_PREFIX}{key_hash}"
    data = await client.hgetall(redis_key)

    if not data:
        return None

    # Check expiry
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) > exp:
                return None
        except (ValueError, TypeError):
            pass

    return {
        "agent_id": data.get("agent_id", "unknown"),
        "scopes": json.loads(data.get("scopes", "[]")),
        "authenticated": True,
        "key_id": data.get("key_id", key_hash[:16]),
    }
