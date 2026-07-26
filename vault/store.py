"""Vault secret store — Fernet encryption + Redis CRUD."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_redis = None
_fernet: Fernet | None = None

_SECRET_PREFIX = "vault:secret:"
_SECRET_INDEX = "vault:secret_index"

_KEY_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9\-_.]{1,200}$")


def _validate_key_name(key: str) -> None:
    """Validate secret key name: alphanumeric, hyphens, underscores, dots, 1-200 chars."""
    if not _KEY_NAME_PATTERN.match(key):
        raise ValueError(
            f"Invalid key name '{key}': must be 1-200 chars, "
            "alphanumeric with hyphens, underscores, and dots only"
        )


def init_vault(redis_client, vault_key: str) -> None:
    """Initialize the vault with a Redis client and Fernet encryption key.

    Args:
        redis_client: Async Redis client connected to the vault DB.
        vault_key: Fernet key string (base64-encoded, 32 bytes).
    """
    global _redis, _fernet

    try:
        _fernet = Fernet(vault_key.encode() if isinstance(vault_key, str) else vault_key)
    except (ValueError, Exception) as e:
        raise ValueError(f"Invalid Fernet key: {e}")

    _redis = redis_client
    logger.info("Vault initialized")


async def store_secret(
    key: str,
    value: str,
    description: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    created_by: str = "admin",
) -> dict:
    """Encrypt and store a secret. Upsert semantics."""
    if _redis is None or _fernet is None:
        raise RuntimeError("Vault not initialized")

    _validate_key_name(key)

    now = datetime.now(timezone.utc)
    encrypted = _fernet.encrypt(value.encode()).decode()

    redis_key = f"{_SECRET_PREFIX}{key}"

    existing = await _redis.hget(redis_key, "created_at")
    created_at = existing if existing is not None else now.isoformat()

    mapping = {
        "encrypted_value": encrypted,
        "description": description or "",
        "category": category or "",
        "tags": json.dumps(tags or []),
        "created_at": created_at,
        "updated_at": now.isoformat(),
        "created_by": created_by,
    }

    await _redis.hset(redis_key, mapping=mapping)
    await _redis.zadd(_SECRET_INDEX, {key: now.timestamp()})

    logger.info("Stored secret '%s' by %s", key, created_by)

    return {
        "key": key,
        "description": description,
        "category": category,
        "tags": tags or [],
        "created_at": created_at,
        "updated_at": now.isoformat(),
        "created_by": created_by,
    }


async def retrieve_secret(key: str) -> dict | None:
    """Fetch and decrypt a secret by key. Returns None if not found."""
    if _redis is None or _fernet is None:
        raise RuntimeError("Vault not initialized")

    _validate_key_name(key)

    redis_key = f"{_SECRET_PREFIX}{key}"
    data = await _redis.hgetall(redis_key)

    if not data:
        return None

    try:
        decrypted = _fernet.decrypt(data["encrypted_value"].encode()).decode()
    except (InvalidToken, KeyError) as e:
        logger.error("Failed to decrypt secret '%s': %s", key, e)
        raise RuntimeError(f"Decryption failed for secret '{key}'")

    try:
        tags = json.loads(data.get("tags", "[]"))
    except (json.JSONDecodeError, TypeError):
        tags = []

    return {
        "key": key,
        "value": decrypted,
        "description": data.get("description") or None,
        "category": data.get("category") or None,
        "tags": tags,
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "created_by": data.get("created_by"),
    }


async def list_secrets(
    category: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List secret metadata (no values). Optionally filter by category."""
    if _redis is None or _fernet is None:
        raise RuntimeError("Vault not initialized")

    keys = await _redis.zrevrange(_SECRET_INDEX, 0, limit - 1)
    results = []

    for secret_key in keys:
        redis_key = f"{_SECRET_PREFIX}{secret_key}"
        data = await _redis.hgetall(redis_key)

        if not data:
            continue

        secret_category = data.get("category") or None
        if category and secret_category != category:
            continue

        try:
            tags = json.loads(data.get("tags", "[]"))
        except (json.JSONDecodeError, TypeError):
            tags = []

        results.append({
            "key": secret_key,
            "description": data.get("description") or None,
            "category": secret_category,
            "tags": tags,
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "created_by": data.get("created_by"),
        })

    return results


async def delete_secret(key: str) -> bool:
    """Delete a secret by key. Returns True if it existed."""
    if _redis is None or _fernet is None:
        raise RuntimeError("Vault not initialized")

    _validate_key_name(key)

    redis_key = f"{_SECRET_PREFIX}{key}"
    deleted = await _redis.delete(redis_key)
    await _redis.zrem(_SECRET_INDEX, key)

    if deleted:
        logger.info("Deleted secret '%s'", key)
        return True
    return False
