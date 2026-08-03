"""Single-workspace membership storage and credential migration (Redis DB 7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from auth.principal import deployment_owner_member_id, deployment_workspace_id


WORKSPACE_KEY = "auth:workspace:current"
MEMBER_PREFIX = "auth:member:"
MEMBER_INDEX = "auth:member_index"
CREDENTIAL_MIGRATION_KEY = "auth:migration:workspace_credentials"
MEMORY_MIGRATION_KEY = "auth:migration:workspace_memories"


class WorkspaceMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    owner_member_id: str


async def ensure_workspace(redis_client) -> Workspace:
    """Create or verify the deployment's one workspace and owner member."""
    workspace_id = deployment_workspace_id()
    owner_member_id = deployment_owner_member_id()
    existing = await redis_client.hgetall(WORKSPACE_KEY)
    if existing:
        if (
            existing.get("workspace_id") != workspace_id
            or existing.get("owner_member_id") != owner_member_id
        ):
            raise WorkspaceMigrationError(
                "FIREKEEP_WORKSPACE_ID/FIREKEEP_OWNER_MEMBER_ID do not match "
                "the workspace already stored in Redis DB 7"
            )
    owner_key = f"{MEMBER_PREFIX}{owner_member_id}"
    owner = await redis_client.hgetall(owner_key)
    if owner and (
        owner.get("member_id", owner_member_id) != owner_member_id
        or owner.get("workspace_id", workspace_id) != workspace_id
        or owner.get("role", "owner") != "owner"
        or owner.get("status", "active") != "active"
    ):
        raise WorkspaceMigrationError(
            "the stored owner member does not match the deployment workspace"
        )
    now = datetime.now(timezone.utc).isoformat()
    async with redis_client.pipeline(transaction=True) as pipe:
        if not existing:
            pipe.hset(
                WORKSPACE_KEY,
                mapping={
                    "workspace_id": workspace_id,
                    "owner_member_id": owner_member_id,
                    "created_at": now,
                },
            )
        # Repair a missing owner row on every run. A partial Redis restore must
        # not leave the deployment workspace present but ownerless.
        pipe.hsetnx(owner_key, "member_id", owner_member_id)
        pipe.hsetnx(owner_key, "workspace_id", workspace_id)
        pipe.hsetnx(owner_key, "role", "owner")
        pipe.hsetnx(owner_key, "status", "active")
        pipe.hsetnx(owner_key, "created_at", now)
        pipe.zadd(MEMBER_INDEX, {owner_member_id: datetime.now(timezone.utc).timestamp()}, nx=True)
        await pipe.execute()

    return Workspace(workspace_id=workspace_id, owner_member_id=owner_member_id)


async def _credential_matches(redis_client, credential_id: str) -> list[tuple[str, dict[str, Any]]]:
    mapped_hash = await redis_client.get(f"auth:cred:{credential_id}")
    if mapped_hash:
        key = f"auth:key:{mapped_hash}"
        record = await redis_client.hgetall(key)
        stored = record.get("credential_id") or record.get("key_id") if record else None
        if record and stored == credential_id:
            return [(key, record)]

    matches: list[tuple[str, dict[str, Any]]] = []
    async for key in redis_client.scan_iter(f"auth:key:{credential_id}*", count=100):
        record = await redis_client.hgetall(key)
        stored = record.get("credential_id") or record.get("key_id") if record else None
        if record and stored == credential_id:
            matches.append((key, record))
    return matches


async def backfill_credentials(redis_client, workspace: Workspace) -> int:
    """Assign every indexed legacy credential to the workspace owner.

    The migration is idempotent and refuses missing/ambiguous index entries.
    Filtering must not be enabled unless this completes.
    """
    credential_ids = await redis_client.zrange("auth:key_index", 0, -1)
    updated = 0
    for credential_id in credential_ids:
        matches = await _credential_matches(redis_client, credential_id)
        if len(matches) != 1:
            raise WorkspaceMigrationError(
                f"credential {credential_id} resolves to {len(matches)} records; "
                "workspace migration refuses to guess"
            )
        key, record = matches[0]
        device_id = record.get("device_id") or record.get("agent_id") or credential_id
        changes = {
            "workspace_id": record.get("workspace_id") or workspace.workspace_id,
            "member_id": record.get("member_id") or workspace.owner_member_id,
            "credential_id": record.get("credential_id") or credential_id,
            "key_id": record.get("key_id") or credential_id,
            "device_id": device_id,
        }
        if changes["workspace_id"] != workspace.workspace_id:
            raise WorkspaceMigrationError(
                f"credential {credential_id} belongs to another workspace"
            )
        ttl = await redis_client.ttl(key)
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=changes)
            pipe.hdel(key, "agent_id")
            pipe.set(f"auth:cred:{credential_id}", key.removeprefix("auth:key:"))
            if ttl > 0:
                pipe.expire(f"auth:cred:{credential_id}", ttl)
            await pipe.execute()
        updated += 1

    await redis_client.set(
        CREDENTIAL_MIGRATION_KEY,
        f"complete:{workspace.workspace_id}:{len(credential_ids)}",
    )
    return updated
