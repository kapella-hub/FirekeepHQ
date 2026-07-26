"""Memory audit trail — query replay events to see who read/wrote what memory, when.

The replay engine already captures memory_read and memory_write events.
This module provides a focused query layer for audit purposes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_STREAM_KEY = "rp:events"


async def get_memory_audit(
    r: aioredis.Redis,
    *,
    action: str | None = None,  # "read" | "write" | None (both)
    memory_chain_id: str | None = None,
    agent_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query replay events for memory access audit.

    Filters the replay stream for memory_read and memory_write events,
    optionally filtered by chain_id, agent, or namespace.
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
) -> dict[str, Any]:
    """Get aggregate memory access stats from replay events."""
    entries = await r.xrevrange(_STREAM_KEY, count=limit * 5)

    reads = 0
    writes = 0
    agents: set[str] = set()
    sessions: set[str] = set()

    for _, fields in entries:
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
