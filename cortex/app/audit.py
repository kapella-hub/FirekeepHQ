"""Memory audit trail — query replay events to see who read/wrote what memory, when.

The replay engine already captures memory_read and memory_write events.
This module provides a focused query layer for audit purposes.

AUTHORIZATION (2026-10-01). A memory_read event carries the recall QUERY TEXT,
and until this date `/audit/*` returned every such event in the stream to any
valid key — Bob could read what Alice had been asking her memory. Now:

* the routes require `replay:read` (every member key holds it);
* every caller sees only events from its own workspace (an event with no
  recorded workspace predates attribution and belongs to this deployment);
* a caller WITHOUT admin sees only events stamped with its own `member_id`.
  Events that carry no member — everything emitted before attribution, plus
  receipts the server writes for itself — are hidden from such a caller: an
  unattributable event is not evidence that it is theirs;
* an admin ("admin" or "*", i.e. the owner and the dashboard) sees the whole
  workspace. With auth disabled the single anonymous principal IS the
  deployment owner, and is treated the same way, so a personal box keeps its
  pre-upgrade history visible.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query

from auth import keys as _auth_keys
from auth.middleware import require_scope

logger = logging.getLogger(__name__)

_STREAM_KEY = "rp:events"


def _audit_scope(identity: dict[str, Any]) -> tuple[str | None, str | None]:
    """(workspace_id, member_id) filters for this caller; member None = all."""
    workspace_id = identity.get("workspace_id")
    if not _auth_keys._AUTH_ENABLED:
        return workspace_id, None
    if _auth_keys.scopes_allow(identity.get("scopes", []), "admin"):
        return workspace_id, None
    return workspace_id, identity.get("member_id") or ""


def _visible(fields: dict[str, Any], workspace_id: str | None, member_id: str | None) -> bool:
    event_ws = fields.get("workspace_id") or ""
    if workspace_id and event_ws and event_ws != workspace_id:
        return False
    if member_id is not None:
        # Fail closed: a non-admin sees an event only when it is provably theirs.
        return bool(member_id) and fields.get("member_id") == member_id
    return True


def create_audit_router(
    get_replay_redis: Callable[[], aioredis.Redis],
) -> APIRouter:
    """The /audit/* routes, scoped to the verified caller (see module doc)."""
    router = APIRouter(prefix="/audit", tags=["audit"])

    @router.get("/memory")
    async def audit_memory(
        identity: dict = Depends(require_scope("replay:read")),
        action: str | None = Query(default=None),
        memory_chain_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        namespace: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        workspace_id, member_id = _audit_scope(identity)
        return {"events": await get_memory_audit(
            get_replay_redis(), action=action,
            memory_chain_id=memory_chain_id, agent_id=agent_id,
            namespace=namespace, limit=limit,
            workspace_id=workspace_id, member_id=member_id,
        )}

    @router.get("/memory/summary")
    async def audit_memory_summary(
        identity: dict = Depends(require_scope("replay:read")),
    ) -> dict[str, Any]:
        workspace_id, member_id = _audit_scope(identity)
        return await get_memory_access_summary(
            get_replay_redis(), workspace_id=workspace_id, member_id=member_id,
        )

    return router


async def get_memory_audit(
    r: aioredis.Redis,
    *,
    action: str | None = None,  # "read" | "write" | None (both)
    memory_chain_id: str | None = None,
    agent_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    workspace_id: str | None = None,
    member_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query replay events for memory access audit.

    Filters the replay stream for memory_read and memory_write events,
    optionally filtered by chain_id, agent, or namespace. `workspace_id` /
    `member_id` are AUTHORIZATION filters derived from the verified caller by
    the router (never from a query parameter); `member_id=None` means the
    caller may see every member's events.
    """
    target_types = set()
    if action == "read":
        target_types = {"memory_read"}
    elif action == "write":
        target_types = {"memory_write"}
    else:
        target_types = {"memory_read", "memory_write"}

    entries = await r.xrevrange(_STREAM_KEY, count=limit * 10)

    results = []
    for stream_id, fields in entries:
        et = fields.get("event_type", "")
        if et not in target_types:
            continue
        if not _visible(fields, workspace_id, member_id):
            continue

        # Apply filters
        if agent_id and fields.get("agent_id") != agent_id:
            continue
        if namespace and fields.get("namespace") != namespace:
            continue

        payload = {}
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass

        # Filter by chain_id if specified
        if memory_chain_id:
            pid = payload.get("memory_chain_id") or payload.get("memory_id", "")
            if memory_chain_id not in pid:
                continue

        results.append({
            "timestamp": fields.get("timestamp", ""),
            "event_type": et,
            "agent_id": fields.get("agent_id", ""),
            "member_id": fields.get("member_id") or None,
            "session_id": fields.get("session_id", ""),
            "namespace": fields.get("namespace", ""),
            "payload": payload,
            "outcome": fields.get("outcome") or None,
        })

        if len(results) >= limit:
            break

    return results


async def get_memory_access_summary(
    r: aioredis.Redis,
    limit: int = 200,
    workspace_id: str | None = None,
    member_id: str | None = None,
) -> dict[str, Any]:
    """Get aggregate memory access stats from replay events (same scoping as
    get_memory_audit)."""
    entries = await r.xrevrange(_STREAM_KEY, count=limit * 5)

    reads = 0
    writes = 0
    agents: set[str] = set()
    sessions: set[str] = set()

    for _, fields in entries:
        if not _visible(fields, workspace_id, member_id):
            continue
        et = fields.get("event_type", "")
        if et == "memory_read":
            reads += 1
        elif et == "memory_write":
            writes += 1
        else:
            continue
        agents.add(fields.get("agent_id", ""))
        sessions.add(fields.get("session_id", ""))

    return {
        "total_reads": reads,
        "total_writes": writes,
        "unique_agents": len(agents),
        "unique_sessions": len(sessions),
        "agents": sorted(agents - {""}),
    }
