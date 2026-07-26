"""Narrowing algorithm — DAG-aware root cause analysis for trace events.

Given a failure event, walks backward through trace links, scoring each
ancestor by link confidence * proximity decay. Returns a ranked list of
suspect events that most likely contributed to the failure.

This replaces "bisect" (which assumes linear history). Agent execution
is a DAG — multiple parallel traces can contribute to a single failure.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import redis.asyncio as aioredis

from replay.reader import get_event, get_session_timeline

logger = logging.getLogger(__name__)

# Decay factor per hop — score reduces by 20% per link traversal
_HOP_DECAY = 0.8

# Maximum depth to walk backward
_DEFAULT_MAX_DEPTH = 10

# Maximum suspects to return
_DEFAULT_MAX_RESULTS = 20


async def narrow(
    r: aioredis.Redis,
    session_id: str,
    failure_event_id: str,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Walk trace links backward from a failure event and rank suspects.

    Algorithm:
    1. Start at the failure event.
    2. BFS through trace_links (backward direction).
    3. Score each ancestor: parent_score * link_confidence * HOP_DECAY.
    4. Also include temporal neighbors (events in same session within ±5s)
       as low-confidence inferred links.
    5. Return ranked suspects.

    Returns a NarrowingResponse-compatible dict.
    """
    failure = await get_event(r, failure_event_id)
    if not failure:
        return {
            "failure_event_id": failure_event_id,
            "suspects": [],
            "total_events_walked": 0,
        }

    suspects: list[dict[str, Any]] = []
    visited: set[str] = set()
    total_walked = 0

    # BFS queue: (event_id, accumulated_score, depth)
    queue: deque[tuple[str, float, int]] = deque()

    # Seed from failure event's trace links
    for link in failure.get("trace_links", []):
        link_confidence = link.get("confidence", 0.5)
        queue.append((link["target_event_id"], link_confidence * _HOP_DECAY, 1))

    # Also add temporal neighbors as low-confidence inferred links
    temporal_neighbors = await _get_temporal_neighbors(
        r, session_id, failure, window_seconds=5.0
    )
    for neighbor in temporal_neighbors:
        nid = neighbor.get("id", "")
        if nid and nid != failure_event_id:
            queue.append((nid, 0.3 * _HOP_DECAY, 1))  # Low confidence for temporal

    # BFS
    while queue:
        eid, score, depth = queue.popleft()

        if eid in visited or depth > max_depth:
            continue
        visited.add(eid)
        total_walked += 1

        event = await get_event(r, eid)
        if not event:
            continue

        suspects.append({
            "event": event,
            "suspicion_score": round(score, 4),
            "depth": depth,
        })

        # Follow this event's trace links deeper
        for link in event.get("trace_links", []):
            link_confidence = link.get("confidence", 0.5)
            child_score = score * link_confidence * _HOP_DECAY
            if child_score > 0.01:  # Prune negligible paths
                queue.append((link["target_event_id"], child_score, depth + 1))

    # Sort by suspicion score descending
    suspects.sort(key=lambda s: s["suspicion_score"], reverse=True)

    return {
        "failure_event_id": failure_event_id,
        "suspects": suspects[:max_results],
        "total_events_walked": total_walked,
    }


async def _get_temporal_neighbors(
    r: aioredis.Redis,
    session_id: str,
    event: dict[str, Any],
    window_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    """Find events in the same session within a time window of the target.

    These are potential inferred links — the agent may have acted on
    information from these events without an explicit trace link.
    """
    try:
        event_ts = event.get("timestamp", "")
        if not event_ts:
            return []

        from datetime import datetime, timedelta

        ts = datetime.fromisoformat(event_ts)
        window_start = (ts - timedelta(seconds=window_seconds)).isoformat()
        window_end = ts.isoformat()

        # Get session timeline in the window
        result = await get_session_timeline(
            r, session_id, limit=20, offset=0
        )

        neighbors = []
        for ev in result.get("events", []):
            ev_ts = ev.get("timestamp", "")
            if ev_ts and window_start <= ev_ts <= window_end:
                if ev.get("id") != event.get("id"):
                    neighbors.append(ev)

        return neighbors

    except Exception as e:
        logger.debug("Temporal neighbor lookup failed: %s", e)
        return []
