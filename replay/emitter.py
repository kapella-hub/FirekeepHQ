"""Replay event emitter — shared library imported by all Firekeep services.

Usage:
    from replay.emitter import init_emitter, emit

    # During service startup:
    await init_emitter(redis_url="redis://redis:6379/6")

    # After any operation:
    await emit(
        event_type="memory_read",
        session_id="abc123",
        agent_id="default",
        payload={"query": "auth bug", "result_count": 3},
    )

Design:
    - Fire-and-forget: emit() NEVER raises. Failures are logged at DEBUG level.
    - Non-blocking: designed to add <1ms overhead to the calling operation.
    - Idempotent: duplicate writes (same idempotency_key) are silently dropped.
    - Global stream: all events go to rp:events with per-session sorted-set indexes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from replay.config import ReplaySettings, get_replay_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_redis: aioredis.Redis | None = None
_settings: ReplaySettings | None = None

# Backpressure / observability counters
_stats: dict[str, int] = {
    "emitted": 0,
    "dropped_disabled": 0,
    "dropped_dedup": 0,
    "dropped_error": 0,
    "stream_length": 0,
}


def get_emitter_stats() -> dict:
    """Return a snapshot of emitter counters for health/metrics endpoints."""
    return dict(_stats)

# Redis key constants
_STREAM_KEY = "rp:events"
_SESSION_IDX_PREFIX = "rp:session_idx:"
_CTX_PREFIX = "rp:ctx:"
_DEDUP_PREFIX = "rp:dedup:"
_EVENT_IDX_PREFIX = "rp:eid:"  # event_id → stream_id lookup index


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def init_emitter(
    redis_url: str | None = None,
    redis_client: aioredis.Redis | None = None,
    settings: ReplaySettings | None = None,
) -> None:
    """Initialize the replay emitter.

    Call once during service startup. Accepts either a redis_url (creates a
    new connection) or an existing redis_client.
    """
    global _redis, _settings
    _settings = settings or get_replay_settings()

    if not _settings.ENABLED:
        logger.info("Replay emitter disabled (RP_ENABLED=false)")
        return

    if redis_client is not None:
        _redis = redis_client
    elif redis_url:
        _redis = aioredis.from_url(
            redis_url,
            decode_responses=True,
            # SP0 D7: bound half-open-connection stalls on the emit hot path.
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    else:
        _redis = aioredis.from_url(
            _settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )

    logger.info("Replay emitter initialized (stream=%s)", _STREAM_KEY)


async def close_emitter() -> None:
    """Close the replay emitter's Redis connection."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def is_enabled() -> bool:
    """Check if the emitter is initialized and enabled."""
    s = _settings or get_replay_settings()
    return s.ENABLED and _redis is not None


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def _json_dumps(obj: Any) -> str:
    """Compact JSON serialization with fallback to str for non-serializable."""
    return json.dumps(obj, separators=(",", ":"), default=str)


async def emit(
    event_type: str,
    session_id: str,
    agent_id: str,
    payload: dict[str, Any],
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    outcome: str | None = None,
    context_ref: str | None = None,
    trace_links: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    tags: list[str] | None = None,
    namespace: str = "default",
    duration_ms: int | None = None,
    error: str | None = None,
) -> str | None:
    """Emit a trace event to the replay stream.

    Returns the Redis stream entry ID on success, or None on failure.
    NEVER raises — all errors are caught and logged at DEBUG level.
    """
    if _redis is None or (_settings and not _settings.ENABLED):
        _stats["dropped_disabled"] += 1
        return None

    try:
        event_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        settings = _settings or get_replay_settings()

        # Idempotency check
        if idempotency_key:
            dedup_key = f"{_DEDUP_PREFIX}{idempotency_key}"
            was_set = await _redis.set(dedup_key, "1", nx=True, ex=settings.DEDUP_TTL_SECONDS)
            if not was_set:
                _stats["dropped_dedup"] += 1
                return None  # Duplicate, silently skip

        # Build stream fields — all values must be strings
        fields: dict[str, str] = {
            "id": event_id,
            "schema_version": "1",
            "trace_id": trace_id or event_id,
            "span_id": span_id or event_id,
            "parent_span_id": parent_span_id or "",
            "session_id": session_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "payload": _json_dumps(payload),
            "outcome": outcome or "",
            "context_ref": context_ref or "",
            "trace_links": _json_dumps(trace_links or []),
            "idempotency_key": idempotency_key or "",
            "tags": _json_dumps(tags or []),
            "namespace": namespace.lower().strip().replace("-", "_"),
            "timestamp": now,
            "duration_ms": str(duration_ms) if duration_ms is not None else "",
            "error": error or "",
        }

        # Append to global stream with approximate max length
        stream_id = await _redis.xadd(
            _STREAM_KEY,
            fields,
            maxlen=settings.STREAM_MAXLEN,
            approximate=True,
        )

        # Update per-session index (sorted set, score = timestamp)
        ts_score = datetime.fromisoformat(now).timestamp()
        idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
        await _redis.zadd(idx_key, {event_id: ts_score})

        # Event ID → stream ID lookup index (for O(1) single-event reads)
        eid_key = f"{_EVENT_IDX_PREFIX}{event_id}"
        await _redis.set(eid_key, stream_id, ex=settings.RETENTION_DAYS * 86400)

        _stats["emitted"] += 1

        # Periodically sample stream length for backpressure visibility
        if _stats["emitted"] % 100 == 0:
            try:
                _stats["stream_length"] = await _redis.xlen(_STREAM_KEY)
            except Exception:
                pass

        return stream_id

    except Exception as e:
        _stats["dropped_error"] += 1
        logger.debug("Replay emit failed (non-critical): %s", e)
        return None


# ---------------------------------------------------------------------------
# Context snapshots (content-addressed)
# ---------------------------------------------------------------------------


async def store_context_snapshot(content: str) -> str | None:
    """Store a context snapshot and return its content-addressed hash.

    Uses SHA-256 of the content. Duplicate content reuses the same key.
    Returns the hash key on success, or None on failure.
    """
    if _redis is None:
        return None

    try:
        import hashlib

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
        key = f"{_CTX_PREFIX}{content_hash}"
        settings = _settings or get_replay_settings()
        ttl = settings.SNAPSHOT_TTL_DAYS * 86400

        # SET NX — only write if this snapshot doesn't already exist (dedup)
        await _redis.set(key, content, nx=True, ex=ttl)

        return content_hash

    except Exception as e:
        logger.debug("Context snapshot store failed (non-critical): %s", e)
        return None


async def get_context_snapshot(content_hash: str) -> str | None:
    """Retrieve a context snapshot by its content-addressed hash."""
    if _redis is None:
        return None

    try:
        key = f"{_CTX_PREFIX}{content_hash}"
        return await _redis.get(key)
    except Exception as e:
        logger.debug("Context snapshot read failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Retention / trimming
# ---------------------------------------------------------------------------


async def trim_old_events() -> int:
    """Remove events older than RETENTION_DAYS from the stream.

    Also cleans up session indexes that reference trimmed events.
    Returns the number of events trimmed.
    """
    if _redis is None:
        return 0

    try:
        settings = _settings or get_replay_settings()
        cutoff = datetime.now(timezone.utc).timestamp() - (settings.RETENTION_DAYS * 86400)
        # Redis stream IDs are {milliseconds}-{seq}
        cutoff_id = f"{int(cutoff * 1000)}-0"

        # Get events to trim
        entries = await _redis.xrange(_STREAM_KEY, min="-", max=cutoff_id, count=1000)
        if not entries:
            return 0

        # Collect stream IDs and event IDs for cleanup
        stream_ids = []
        session_events: dict[str, list[str]] = {}  # session_id → [event_ids]
        for stream_id, fields in entries:
            stream_ids.append(stream_id)
            sid = fields.get("session_id", "")
            eid = fields.get("id", "")
            if sid and eid:
                session_events.setdefault(sid, []).append(eid)

        # Delete from stream
        if stream_ids:
            await _redis.xdel(_STREAM_KEY, *stream_ids)

        # Clean up session indexes
        for sid, eids in session_events.items():
            idx_key = f"{_SESSION_IDX_PREFIX}{sid}"
            if eids:
                await _redis.zrem(idx_key, *eids)
            # Remove empty indexes
            remaining = await _redis.zcard(idx_key)
            if remaining == 0:
                await _redis.delete(idx_key)

        return len(stream_ids)

    except Exception as e:
        logger.warning("Replay trim failed: %s", e)
        return 0
