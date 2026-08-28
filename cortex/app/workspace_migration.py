"""Ordered single-workspace migration for credentials and Qdrant memories."""

from __future__ import annotations

from qdrant_client.models import PayloadSchemaType

from auth.workspace import (
    MEMORY_MIGRATION_KEY,
    WorkspaceMigrationError,
    backfill_credentials,
    ensure_workspace,
)

# identity-v2 D6: the sentinel workspace_id the freeze-migration stamps onto
# unattributable ("quarantine") points -- memory-shaped payloads with no
# workspace_id, copied at their existing id with workspace_id set to this
# value plus legacy_unscoped=True. No real principal ever holds this id, so
# a quarantine point is uniformly invisible to both recall legs until an
# admin explicitly adopts it. Defined ONCE here; the migration tool (D6)
# imports it rather than redefining the literal.
QUARANTINE_WORKSPACE = "__quarantine__"


def _is_quarantined(payload: dict) -> bool:
    """A point the single-workspace backfill must never touch.

    Two independent signals, either sufficient on its own: the sentinel
    workspace_id (fresh quarantine copies from the migration) and the
    `legacy_unscoped` flag (also stamped on quarantine copies, and on
    graph-side legacy rows per D4 -- checked here too so a point missing
    one of the two stamps, or a hand-repaired one, still quarantines).
    """
    return (
        payload.get("workspace_id") == QUARANTINE_WORKSPACE
        or payload.get("legacy_unscoped") is True
    )


async def _all_points(vector_client):
    offset = None
    while True:
        points, offset = await vector_client._client.scroll(
            collection_name=vector_client._collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            yield point
        if offset is None:
            break


async def backfill_memories(vector_client, workspace_id: str, owner_member_id: str) -> int:
    """Backfill and verify every existing Qdrant point before filtering starts.

    Quarantine/legacy_unscoped points (see `_is_quarantined`) are skipped
    entirely: not backfilled, not required to carry workspace_id/member_id
    in the post-backfill verification, and -- critically -- never subject
    to the cross-workspace guard below, since a quarantine point's
    workspace_id is BY DESIGN a sentinel that differs from every real
    workspace. Without this skip, the first restart after a freeze
    migration would either silently adopt the whole quarantine bucket into
    the deployment workspace or, worse, raise on it as if it were a
    genuine cross-workspace collision.
    """
    missing_workspace: list = []
    missing_member: list = []
    total_before = 0
    async for point in _all_points(vector_client):
        total_before += 1
        payload = point.payload or {}
        if _is_quarantined(payload):
            continue
        existing_workspace = payload.get("workspace_id")
        if existing_workspace and existing_workspace != workspace_id:
            raise WorkspaceMigrationError(
                f"memory {point.id} belongs to another workspace"
            )
        if not existing_workspace:
            missing_workspace.append(point.id)
        if not payload.get("member_id"):
            missing_member.append(point.id)

    if missing_workspace:
        await vector_client._client.set_payload(
            collection_name=vector_client._collection,
            payload={"workspace_id": workspace_id},
            points=missing_workspace,
        )
    if missing_member:
        await vector_client._client.set_payload(
            collection_name=vector_client._collection,
            payload={"member_id": owner_member_id},
            points=missing_member,
        )

    total_after = 0
    async for point in _all_points(vector_client):
        total_after += 1
        payload = point.payload or {}
        if _is_quarantined(payload):
            continue
        if (
            payload.get("workspace_id") != workspace_id
            or not payload.get("member_id")
        ):
            raise WorkspaceMigrationError(
                f"memory {point.id} was not fully backfilled"
            )
    if total_after != total_before:
        raise WorkspaceMigrationError(
            f"memory count changed during backfill ({total_before} -> {total_after})"
        )

    try:
        await vector_client._client.create_payload_index(
            collection_name=vector_client._collection,
            field_name="workspace_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass
    return total_after


async def migrate_single_workspace(auth_redis, vector_client):
    """Run the mandatory order and mark filtering safe only at the end.

    `MEMORY_MIGRATION_KEY` was written on completion since this module's
    introduction but never read -- every boot re-ran `backfill_credentials`
    and `backfill_memories` in full, which was merely wasteful before
    quarantine points existed. After D6 it is unsafe: a stray quarantine or
    legacy_unscoped point is excluded from backfill by design (see
    `_is_quarantined`), but re-scanning on every restart is still a full
    Qdrant scroll for no benefit once a workspace is provably migrated. So
    the marker is checked FIRST: `ensure_workspace` stays cheap and
    idempotent (safe, and needed either way to resolve/repair the
    workspace record), but the credential and memory backfills are skipped
    entirely once the marker is present.
    """
    workspace = await ensure_workspace(auth_redis)
    if await auth_redis.get(MEMORY_MIGRATION_KEY):
        return workspace
    await backfill_credentials(auth_redis, workspace)
    memory_count = await backfill_memories(
        vector_client, workspace.workspace_id, workspace.owner_member_id
    )
    await auth_redis.set(
        MEMORY_MIGRATION_KEY,
        f"complete:{workspace.workspace_id}:{memory_count}",
    )
    return workspace
