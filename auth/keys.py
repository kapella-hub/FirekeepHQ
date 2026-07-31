"""Firekeep Auth — key primitives (fastapi-free).

This module owns key generation, storage, and validation so that pure-ASGI
consumers (auth/asgi.py — injected into the FastMCP services, whose images
ship starlette via fastmcp but NOT fastapi) can import it. auth/middleware.py
re-exports everything here and adds the FastAPI-only require_scope dependency
on top. Both layers read the same Redis DB 7 store.

API keys are stored as SHA-256 hashes (plaintext never stored):
  auth:key:{sha256hex}       -> hash {agent_id, scopes (JSON), created_at,
                                     key_id, credential_id, device_id, expires_at?}
  auth:cred:{credential_id}  -> sha256hex (new credentials; legacy records may omit it)
  auth:key_index             -> zset of credential IDs scored by creation ts
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
    # Vault READ, split out from "admin" 2026-07-29. Every vault route required
    # admin, and teammate keys deliberately carry no admin scope, so an agent
    # asking "deploy to my vps" got 403 on the credential it needed. Reproduced
    # with a properly minted key:
    #   GET /vault/secrets -> 403 "Insufficient scope: requires 'admin'"
    # The only workaround was handing teammates an admin key, which also grants
    # key-minting and every other secret — strictly worse than a read scope.
    #
    # WRITE and DELETE stay admin-only. Reading a secret you were given is
    # ordinary work; creating or destroying one is administration, and the blast
    # radius is not symmetric.
    "vault:read",
    "admin",
}
ENROLLABLE_SCOPES: frozenset[str] = frozenset(SCOPES - {"admin", "*"})
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
_CRED_PREFIX = "auth:cred:"

# A key_id reaches us from a URL path parameter and is interpolated into a
# Redis glob. Anything outside lowercase hex would either match nothing or --
# for '*', '?', '[' -- widen the scan to the whole keyspace, where "first
# match wins" becomes "delete an arbitrary credential and report success".
_KEY_ID_RE = re.compile(r"^[0-9a-f]{1,64}$")


class AmbiguousKeyIdError(Exception):
    """More than one stored record claims this key_id.

    Raised instead of acting on a guess. Under server-minted keys this is
    unreachable (it needs a 64-bit prefix collision); once a client supplies
    its own credential hash it becomes a choice, which is why resolution
    verifies rather than assumes.
    """

    def __init__(self, key_id: str, matches: list[str]) -> None:
        super().__init__(
            f"key_id {key_id!r} matches {len(matches)} stored records; refusing to guess"
        )
        self.key_id = key_id
        self.matches = matches


async def _resolve_key_id(key_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every record whose STORED key_id equals `key_id`.

    A prefix match is not an identity: the glob is only a way to narrow the
    scan, and each candidate is confirmed against its own key_id field.
    """
    if _redis is None or not _KEY_ID_RE.fullmatch(key_id):
        return []

    # New records have an exact, independently-minted identifier mapping. Do
    # not turn that identifier back into a hash prefix: a client controls its
    # credential hash, so a prefix is not an identity boundary.
    mapped_hash = await _redis.get(f"{_CRED_PREFIX}{key_id}")
    if mapped_hash:
        redis_key = f"{_KEY_PREFIX}{mapped_hash}"
        data = await _redis.hgetall(redis_key)
        stored_id = data.get("credential_id") or data.get("key_id") if data else None
        if data and stored_id == key_id:
            return [(redis_key, data)]
        logger.critical(
            "Credential mapping %s points to a missing or mismatched record; "
            "falling back to verified legacy resolution",
            key_id,
        )

    # Legacy records predate auth:cred mappings. A prefix scan is acceptable
    # only as a candidate finder; every candidate must verify its own ID.
    found: list[tuple[str, dict[str, Any]]] = []
    async for redis_key in _redis.scan_iter(f"{_KEY_PREFIX}{key_id}*", count=100):
        data = await _redis.hgetall(redis_key)
        stored_id = data.get("credential_id") or data.get("key_id") if data else None
        if data and stored_id == key_id:
            found.append((redis_key, data))
    return found


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
#
# "vault:read" IS SUBTRACTED TOO, and the derive-automatically property above is
# exactly why it has to be stated. That comment promises a newly added scope is
# granted to anonymous callers automatically — which for a scope that decrypts
# secrets would re-open the hole audit blocker 7 closed, on any default
# AUTH_ENABLED=false box. Reading a secret requires presenting a key, always.
# Guarded by tests/test_auth_scopes.py.
ANONYMOUS_SCOPES: tuple[str, ...] = tuple(sorted(SCOPES - {"admin", "*", "vault:read"}))

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


def build_credential_record(
    credential_id: str,
    device_id: str,
    scopes: list[str],
    now: datetime,
    expires_days: int | None,
    *,
    enrolled_via: str | None = None,
    device_label: str | None = None,
) -> dict[str, str]:
    """Build the Redis field map for a credential without performing I/O.

    Enrollment and ordinary key creation share this definition so validation,
    listing, and revocation see one DB-7 schema.  ``enrolled_via`` is kept
    narrow and keyword-only; create_key deliberately has no generic metadata
    passthrough.
    """
    metadata = {
        # Compatibility identity until the workspace principal model lands.
        "agent_id": device_id,
        "device_id": device_id,
        "credential_id": credential_id,
        "key_id": credential_id,
        "scopes": json.dumps(scopes),
        "created_at": now.isoformat(),
    }
    if expires_days:
        metadata["expires_at"] = (now + timedelta(days=expires_days)).isoformat()
    if enrolled_via is not None:
        metadata["enrolled_via"] = enrolled_via
        metadata["enrolled_at"] = now.isoformat()
    if device_label:
        metadata["device_label"] = device_label
    return metadata


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

    # Manual/server-minted keys retain their historical short hash ID. Client
    # enrollment supplies an independently-minted ID to the same pure helper.
    credential_id = key_hash[:16]
    metadata = build_credential_record(
        credential_id,
        agent_id,
        scopes,
        now,
        expires_days,
    )

    redis_key = f"{_KEY_PREFIX}{key_hash}"
    ttl = expires_days * 86400 if expires_days else None

    # One transaction, not three sequential round trips. MULTI/EXEC does NOT roll
    # back on a command error -- it guarantees every command is queued before any
    # of them applies, so a process death between them applies NONE of them. That
    # is exactly the window that mattered here, and there were two of them:
    #   HSET without ZADD  -> a credential validate_key accepts (full-hash lookup)
    #                         that list_keys can never show (walks the index).
    #                         Invisible to every enumeration path, unrevocable by
    #                         key_id -- the same invariant test_key_id_resolution's
    #                         TestSubsystemInvariant pins for revoke sequences,
    #                         entered through the write path instead.
    #   HSET without EXPIRE -> a credential that was meant to expire and never
    #                         will. Permanent when it was meant to be temporary.
    async with _redis.pipeline(transaction=True) as pipe:
        pipe.hset(redis_key, mapping=metadata)
        pipe.set(f"{_CRED_PREFIX}{credential_id}", key_hash)
        if ttl:
            pipe.expire(redis_key, ttl)
            pipe.expire(f"{_CRED_PREFIX}{credential_id}", ttl)
        # Index membership is part of the credential's existence, not a follow-up.
        pipe.zadd(_KEY_INDEX, {credential_id: now.timestamp()})
        await pipe.execute()

    return {
        "api_key": api_key,  # Only returned at creation time
        "key_id": credential_id,
        "credential_id": credential_id,
        "device_id": agent_id,
        "agent_id": agent_id,
        "scopes": scopes,
        "created_at": now.isoformat(),
        "expires_at": metadata.get("expires_at"),
    }


async def list_keys(limit: int = 50) -> list[dict[str, Any]]:
    """List all API keys (metadata only, never plaintext).

    Where one index member resolves to more than one record, EVERY record is
    emitted and each is marked ambiguous. Under-reporting is the failure mode
    here: a credential hidden behind another's prefix still authenticates.
    """
    if _redis is None:
        return []

    key_ids = await _redis.zrevrange(_KEY_INDEX, 0, limit - 1)
    rows: list[dict[str, Any]] = []
    for kid in key_ids:
        matches = await _resolve_key_id(kid)
        if len(matches) > 1:
            logger.critical(
                "AMBIGUOUS key_id %s matches %d stored records (%s); listing all",
                kid, len(matches), ", ".join(k for k, _ in matches),
            )
        for _redis_key, data in matches:
            rows.append({
                "key_id": data["key_id"],
                "credential_id": data.get("credential_id", data["key_id"]),
                "device_id": data.get("device_id", data.get("agent_id", "unknown")),
                "device_label": data.get("device_label"),
                "agent_id": data.get("agent_id", "unknown"),
                "scopes": json.loads(data.get("scopes", "[]")),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
                "enrolled_via": data.get("enrolled_via"),
                "ambiguous": len(matches) > 1,
            })
    return rows


async def revoke_key(key_id: str) -> bool:
    """Revoke an API key by its short ID.

    Returns True if exactly one record verified and was deleted, False if none
    did. Raises AmbiguousKeyIdError when more than one verified: deleting one
    of two while reporting success is the defect this function used to have.
    """
    if _redis is None:
        return False

    matches = await _resolve_key_id(key_id)
    if not matches:
        return False
    if len(matches) > 1:
        logger.critical(
            "AMBIGUOUS key_id %s matches %d stored records (%s); refusing to revoke",
            key_id, len(matches), ", ".join(k for k, _ in matches),
        )
        raise AmbiguousKeyIdError(key_id, [k for k, _ in matches])

    redis_key, data = matches[0]
    credential_id = data.get("credential_id") or data.get("key_id") or key_id
    async with _redis.pipeline(transaction=True) as pipe:
        pipe.delete(redis_key)
        pipe.delete(f"{_CRED_PREFIX}{credential_id}")
        pipe.zrem(_KEY_INDEX, key_id)
        await pipe.execute()
    return True


async def rename_device(credential_id: str, label: str) -> bool:
    """Change display metadata only; never rotate or rewrite the credential."""
    if _redis is None:
        return False
    matches = await _resolve_key_id(credential_id)
    if not matches:
        return False
    if len(matches) > 1:
        raise AmbiguousKeyIdError(credential_id, [key for key, _ in matches])
    await _redis.hset(matches[0][0], mapping={"device_label": label})
    return True


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

    Delegates to validate_key_by_hash after hashing the plaintext — that
    function is the single implementation of the lookup, so this and any
    other caller keying by hash can never drift apart.
    """
    return await validate_key_by_hash(_hash_key(api_key), redis_client)


async def validate_key_by_hash(key_hash: str, redis_client=None) -> dict[str, Any] | None:
    """Look up a stored key record by its already-computed SHA-256 hash.

    This is THE validation implementation — validate_key is a thin wrapper
    that hashes the plaintext and calls straight through. Exists as its own
    function so tests and the enrollment design can ask "does this stored
    record still authenticate?" without possessing the plaintext.
    """
    client = redis_client if redis_client is not None else _redis
    if client is None:
        return None
    data = await client.hgetall(f"{_KEY_PREFIX}{key_hash}")
    if not data:
        return None
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                return None
        except (ValueError, TypeError):
            pass
    return {
        "agent_id": data.get("agent_id", "unknown"),
        "scopes": json.loads(data.get("scopes", "[]")),
        "authenticated": True,
        "key_id": data.get("key_id", key_hash[:16]),
    }


async def invalid_credential_detail(api_key: str, redis_client=None) -> str:
    """Explain a failed credential without conflating expiry with absence."""
    client = redis_client if redis_client is not None else _redis
    if client is None:
        return "Unknown API key"
    data = await client.hgetall(f"{_KEY_PREFIX}{_hash_key(api_key)}")
    expires_at = data.get("expires_at") if data else None
    if expires_at:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                return f"API key expired at {expires_at}"
        except (ValueError, TypeError):
            pass
    return "Unknown API key"
