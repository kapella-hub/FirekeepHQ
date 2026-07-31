"""Ordered single-workspace migration for credentials and Qdrant memories."""

from __future__ import annotations

from qdrant_client.models import PayloadSchemaType

from auth.workspace import (
    MEMORY_MIGRATION_KEY,
    WorkspaceMigrationError,
    backfill_credentials,
    ensure_workspace,
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
    """Backfill and verify every existing Qdrant point before filtering starts."""
    missing_workspace: list = []
    missing_member: list = []
    total_before = 0
    async for point in _all_points(vector_client):
        total_before += 1
        payload = point.payload or {}
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
    """Run the mandatory order and mark filtering safe only at the end."""
    workspace = await ensure_workspace(auth_redis)
    await backfill_credentials(auth_redis, workspace)
    memory_count = await backfill_memories(
        vector_client, workspace.workspace_id, workspace.owner_member_id
    )
    await auth_redis.set(
        MEMORY_MIGRATION_KEY,
        f"complete:{workspace.workspace_id}:{memory_count}",
    )
    return workspace
