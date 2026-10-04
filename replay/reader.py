"""Replay event reader — query functions for trace events.

Used by both REST API endpoints and MCP tools. All functions accept a Redis
client and return typed response dicts.

AUTHORIZATION. The read functions take an optional keyword-only ``scope``
(``replay.authz.ReplayScope``). The REST router always passes the verified
caller's scope; an event outside it is treated exactly like an event that does
not exist (no count, no label, no snapshot, no existence oracle). ``scope=None``
is the unfiltered internal path the eval/OWM/pattern workers use in-process,
and it is byte-for-byte the pre-authorization behaviour.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis

from replay.authz import ReplayScope, event_visible, session_owner

logger = logging.getLogger(__name__)

# Redis key constants (must match emitter.py)
_STREAM_KEY = "rp:events"
_SESSION_IDX_PREFIX = "rp:session_idx:"
_CTX_PREFIX = "rp:ctx:"
_EVENT_IDX_PREFIX = "rp:eid:"  # event_id → stream_id lookup index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_event(stream_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Parse a raw Redis stream entry into a TraceEventResponse-compatible dict."""
    return {
        "id": fields.get("id", ""),
        "schema_version": int(fields.get("schema_version", "1")),
        "trace_id": fields.get("trace_id", ""),
        "span_id": fields.get("span_id", ""),
        "parent_span_id": fields.get("parent_span_id") or None,
        "session_id": fields.get("session_id", ""),
        "agent_id": fields.get("agent_id", ""),
        # Verified provenance (absent on unattributed events). Not part of
        # TraceEventResponse; carried so in-process consumers (eval owner
        # attribution) can read it.
        "workspace_id": fields.get("workspace_id") or None,
        "member_id": fields.get("member_id") or None,
        "timestamp": fields.get("timestamp", ""),
        "namespace": fields.get("namespace", "default"),
        "event_type": fields.get("event_type", ""),
        "trace_links": json.loads(fields.get("trace_links", "[]")),
        "payload": json.loads(fields.get("payload", "{}")),
        "outcome": fields.get("outcome") or None,
        "duration_ms": int(fields["duration_ms"]) if fields.get("duration_ms") else None,
        "error": fields.get("error") or None,
        "context_ref": fields.get("context_ref") or None,
        "tags": json.loads(fields.get("tags", "[]")),
        "stream_id": stream_id,
    }


async def _get_session_event_ids(
    r: aioredis.Redis,
    session_id: str,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[str], int]:
    """Get event IDs for a session from the sorted-set index.

    Returns (event_ids, total_count).
    """
    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    total = await r.zcard(idx_key)
    if total == 0:
        return [], 0

    # ZRANGE with BYSCORE for chronological order
    event_ids = await r.zrange(idx_key, offset, offset + limit - 1)
    return event_ids, total


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


async def get_session_event_ids(
    r: aioredis.Redis,
    session_id: str,
    *,
    limit: int = 5000,
) -> list[str]:
    """The newest `limit` event IDs for a session, oldest-first — ONE zrange.

    The grade lift (find_terminal_grade) snapshots this once and hydrates it
    locally in backward windows. A single ID snapshot is snapshot-stable
    against concurrent appends (the list is fixed) and immune to missing
    bodies (callers iterate IDs, not hydrated events) — the two hazards that
    sank live rank-relative paging."""
    if limit <= 0:
        return []
    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    ids = await r.zrange(idx_key, -limit, -1)
    return [i.decode() if isinstance(i, bytes) else i for i in ids]


async def get_session_timeline(
    r: aioredis.Redis,
    session_id: str,
    *,
    event_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    scope: ReplayScope | None = None,
) -> dict[str, Any]:
    """Get the event timeline for a session.

    Returns a TimelineResponse-compatible dict. With a ``scope`` the page,
    ``total`` and ``has_more`` are all computed over the events the caller may
    see — filter first, then page — so neither the page nor the counts reveal
    another member's events in a shared client session id.
    """
    if scope is not None:
        visible = await _visible_session_events(r, session_id, scope)
        if event_type:
            visible = [e for e in visible if e["event_type"] == event_type]
        return {
            "session_id": session_id,
            "events": visible[offset:offset + limit],
            "total": len(visible),
            "has_more": (offset + limit) < len(visible),
        }

    event_ids, total = await _get_session_event_ids(r, session_id, offset, limit)
    if not event_ids:
        return {
            "session_id": session_id,
            "events": [],
            "total": 0,
            "has_more": False,
        }

    # Fetch full events from the stream by scanning.
    # Since we have event IDs but need to find them in the stream,
    # build a lookup set and scan the session's time range.
    target_ids = set(event_ids)
    events = []

    # Get time range from the session index
    first_score = await r.zscore(f"{_SESSION_IDX_PREFIX}{session_id}", event_ids[0])
    last_score = await r.zscore(f"{_SESSION_IDX_PREFIX}{session_id}", event_ids[-1])

    if first_score is None or last_score is None:
        return {"session_id": session_id, "events": [], "total": 0, "has_more": False}

    # Convert timestamps to Redis stream IDs (milliseconds)
    min_id = f"{int(first_score * 1000) - 1}-0"
    max_id = f"{int(last_score * 1000) + 1000}-0"

    # Scan the stream in the time range, filter by session + event IDs
    raw_entries = await r.xrange(_STREAM_KEY, min=min_id, max=max_id, count=limit * 5)

    for stream_id, fields in raw_entries:
        if fields.get("session_id") != session_id:
            continue
        eid = fields.get("id", "")
        if eid not in target_ids:
            continue
        if event_type and fields.get("event_type") != event_type:
            continue
        events.append(_parse_event(stream_id, fields))

    # Sort by timestamp
    events.sort(key=lambda e: e["timestamp"])

    # Apply event_type filter to total count
    filtered_total = total
    if event_type:
        filtered_total = len(events)

    return {
        "session_id": session_id,
        "events": events[:limit],
        "total": filtered_total,
        "has_more": (offset + limit) < filtered_total,
    }


async def get_event(
    r: aioredis.Redis,
    event_id: str,
    *,
    scope: ReplayScope | None = None,
) -> dict[str, Any] | None:
    """Get a single event by its ID.

    Uses the rp:eid:{event_id} → stream_id index for O(1) lookup.
    Falls back to stream scan if the index entry is missing. An event outside
    ``scope`` returns None — the same answer as an id that does not exist.
    """
    # Fast path: use the event_id → stream_id index
    eid_key = f"{_EVENT_IDX_PREFIX}{event_id}"
    stream_id = await r.get(eid_key)
    if stream_id:
        entries = await r.xrange(_STREAM_KEY, min=stream_id, max=stream_id, count=1)
        if entries:
            sid, fields = entries[0]
            if fields.get("id") == event_id:
                if not event_visible(fields, scope):
                    return None
                return _parse_event(sid, fields)

    # Slow fallback: scan recent entries (for events emitted before index existed)
    entries = await r.xrevrange(_STREAM_KEY, count=2000)
    for sid, fields in entries:
        if fields.get("id") == event_id:
            if not event_visible(fields, scope):
                return None
            return _parse_event(sid, fields)
    return None


async def get_event_batch(
    r: aioredis.Redis,
    event_ids: list[str],
    *,
    scope: ReplayScope | None = None,
) -> list[dict[str, Any]]:
    """Get multiple events by ID using the event_id → stream_id index.

    One MGET resolves all event_id → stream_id lookups, then one
    non-transactional pipeline of exact XRANGEs hydrates the bodies — a 5k
    grade scan makes ~2 round trips per window instead of ~2 per event.
    Preserves request order and duplicate/missing-ID behavior exactly.
    Events outside ``scope`` are omitted, exactly like missing ones.
    """
    if not event_ids:
        return []

    unique_ids = list(dict.fromkeys(event_ids))
    stream_ids = await r.mget(
        [f"{_EVENT_IDX_PREFIX}{event_id}" for event_id in unique_ids])

    indexed = [
        (event_id, stream_id)
        for event_id, stream_id in zip(unique_ids, stream_ids, strict=True)
        if stream_id
    ]
    async with r.pipeline(transaction=False) as pipe:
        for _, stream_id in indexed:
            pipe.xrange(_STREAM_KEY, min=stream_id, max=stream_id, count=1)
        rows = await pipe.execute()

    found: dict[str, dict[str, Any]] = {}
    for (event_id, _), entries in zip(indexed, rows, strict=True):
        if entries:
            stream_id, fields = entries[0]
            if fields.get("id") == event_id and event_visible(fields, scope):
                found[event_id] = _parse_event(stream_id, fields)
    return [found[event_id] for event_id in event_ids if event_id in found]


async def get_session_owner(
    r: aioredis.Redis,
    session_id: str,
    *,
    scan: int = 50,
) -> dict[str, str | None]:
    """``{workspace_id, member_id}`` of a session, from its OLDEST events.

    The session-start event is the first thing Bridge writes, so the oldest
    ``scan`` events contain it whenever it exists (``replay.authz.
    session_owner`` explains why it — and not a vote — decides). Unattributed
    ({None, None}) when there is no stamped start event.
    """
    ids = await r.zrange(f"{_SESSION_IDX_PREFIX}{session_id}", 0, scan - 1)
    if not ids:
        return {"workspace_id": None, "member_id": None}
    ids = [i.decode() if isinstance(i, bytes) else i for i in ids]
    return session_owner(await get_event_batch(r, ids))


async def _visible_session_events(
    r: aioredis.Redis,
    session_id: str,
    scope: ReplayScope,
) -> list[dict[str, Any]]:
    """Every event of ``session_id`` that ``scope`` may see, oldest first.

    Hydrates the session's own index (bounded by the session's size, not the
    global stream) through get_event_batch, which applies the predicate.
    """
    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    ids = await r.zrange(idx_key, 0, -1)
    if not ids:
        return []
    ids = [i.decode() if isinstance(i, bytes) else i for i in ids]
    events = await get_event_batch(r, ids, scope=scope)
    return [e for e in events if e["session_id"] == session_id]


def _summarize(session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """SessionSummaryResponse from already-authorized, parsed events."""
    event_type_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    agents: set[str] = set()
    timestamps = [e["timestamp"] for e in events if e.get("timestamp")]
    for e in events:
        et = e.get("event_type") or "unknown"
        event_type_counts[et] = event_type_counts.get(et, 0) + 1
        if e.get("outcome"):
            outcome_counts[e["outcome"]] = outcome_counts.get(e["outcome"], 0) + 1
        if e.get("agent_id"):
            agents.add(e["agent_id"])
    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None
    duration_ms = None
    if first_ts and last_ts:
        try:
            delta = datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)
            duration_ms = int(delta.total_seconds() * 1000)
        except (ValueError, TypeError):
            pass
    return {
        "session_id": session_id,
        "event_count": len(events),
        "duration_ms": duration_ms,
        "event_type_counts": event_type_counts,
        "outcome_counts": outcome_counts,
        "first_event_at": first_ts,
        "last_event_at": last_ts,
        "agents": sorted(agents),
        "has_failures": outcome_counts.get("failure", 0) > 0,
    }


async def get_session_summary(
    r: aioredis.Redis,
    session_id: str,
    *,
    scope: ReplayScope | None = None,
) -> dict[str, Any]:
    """Get summary statistics for a session's trace events.

    With a ``scope`` every figure — the count, the agent labels, the outcome
    tallies, the time bounds — is computed over visible events only.
    """
    if scope is not None:
        return _summarize(session_id, await _visible_session_events(r, session_id, scope))

    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    total = await r.zcard(idx_key)
    if total == 0:
        return {
            "session_id": session_id,
            "event_count": 0,
            "duration_ms": None,
            "event_type_counts": {},
            "outcome_counts": {},
            "first_event_at": None,
            "last_event_at": None,
            "agents": [],
            "has_failures": False,
        }

    # Get all event IDs
    all_ids = await r.zrange(idx_key, 0, -1)
    first_score = await r.zscore(idx_key, all_ids[0])
    last_score = await r.zscore(idx_key, all_ids[-1])

    # Scan events for stats
    event_type_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    agents: set[str] = set()
    has_failures = False
    first_ts: str | None = None
    last_ts: str | None = None

    min_id = f"{int(first_score * 1000) - 1}-0" if first_score else "-"
    max_id = f"{int(last_score * 1000) + 1000}-0" if last_score else "+"

    entries = await r.xrange(_STREAM_KEY, min=min_id, max=max_id)
    for _, fields in entries:
        if fields.get("session_id") != session_id:
            continue

        et = fields.get("event_type", "unknown")
        event_type_counts[et] = event_type_counts.get(et, 0) + 1

        outcome = fields.get("outcome", "")
        if outcome:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            if outcome == "failure":
                has_failures = True

        agent = fields.get("agent_id", "")
        if agent:
            agents.add(agent)

        ts = fields.get("timestamp", "")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    # Compute duration
    duration_ms = None
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts)
            t1 = datetime.fromisoformat(last_ts)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    return {
        "session_id": session_id,
        "event_count": total,
        "duration_ms": duration_ms,
        "event_type_counts": event_type_counts,
        "outcome_counts": outcome_counts,
        "first_event_at": first_ts,
        "last_event_at": last_ts,
        "agents": sorted(agents),
        "has_failures": has_failures,
    }


async def get_context_at(
    r: aioredis.Redis,
    session_id: str,
    event_id: str,
    *,
    scope: ReplayScope | None = None,
) -> dict[str, Any]:
    """Reconstruct the context at a specific event.

    If the event has a context_ref, returns the stored snapshot.
    Otherwise, finds the nearest snapshot and returns it with a note
    about the gap. With a ``scope``, an invisible target reads as "Event not
    found" and the backward walk skips invisible events, so another member's
    snapshot (their whole shadow) is never the "nearest" one.
    """
    scope_kw = {} if scope is None else {"scope": scope}
    event = await get_event(r, event_id, **scope_kw)
    if not event:
        return {"error": "Event not found", "event_id": event_id}

    # Direct snapshot?
    if event.get("context_ref"):
        snapshot = await r.get(f"{_CTX_PREFIX}{event['context_ref']}")
        if snapshot:
            return {
                "event_id": event_id,
                "snapshot_type": "exact",
                "context": snapshot,
                "timestamp": event["timestamp"],
            }

    # Walk backward to find nearest snapshot
    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    all_ids = await r.zrange(idx_key, 0, -1)

    # Find position of target event
    try:
        target_idx = all_ids.index(event_id)
    except ValueError:
        return {"error": "Event not in session index", "event_id": event_id}

    # Walk backward from target. Scoped, the distance counts only events the
    # caller can see — an index-position gap would reveal how many of another
    # member's events sit in between.
    visible_steps = 0
    for i in range(target_idx - 1, -1, -1):
        prev_event = await get_event(r, all_ids[i], **scope_kw)
        if prev_event is not None:
            visible_steps += 1
        if prev_event and prev_event.get("context_ref"):
            snapshot = await r.get(f"{_CTX_PREFIX}{prev_event['context_ref']}")
            if snapshot:
                return {
                    "event_id": event_id,
                    "snapshot_type": "nearest",
                    "context": snapshot,
                    "nearest_event_id": prev_event["id"],
                    "events_since_snapshot": (
                        target_idx - i if scope is None else visible_steps),
                    "timestamp": event["timestamp"],
                }

    return {
        "event_id": event_id,
        "snapshot_type": "none",
        "context": None,
        "note": "No context snapshot found in this session",
    }
