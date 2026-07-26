"""Redis streams event storage for FirekeepSentinel."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from redis.asyncio import Redis

STREAM_KEY = "ns:events"

logger = logging.getLogger(__name__)


def _internal_key_headers(key: str | None) -> dict[str, str]:
    """X-API-Key for Sentinel's server-initiated outbound calls (office AUTH).

    Local copy — Sentinel must NOT import cortex.app.* (client/server import
    boundary). Empty dict when no key -> personal-VPS calls unchanged.
    """
    return {"X-API-Key": key} if key else {}


async def _fire_cortex_webhook(
    source: str, severity: str, summary: str, event_type: str,
    internal_key: str | None = None,
) -> None:
    """Fire-and-forget webhook via Cortex's internal endpoint."""
    try:
        from app.config import get_settings
        cortex_url = get_settings().CORTEX_API_URL
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{cortex_url}/webhooks/internal/fire",
                params={"event_type": "sentinel.alert", "namespace": "default"},
                json={"source": source, "severity": severity, "summary": summary, "event_type": event_type},
                headers=_internal_key_headers(internal_key),
            )
    except Exception:
        logger.debug("Cortex webhook fire failed (non-critical)")


async def _broadcast_alert(
    relay_url: str, source: str, severity: str, summary: str,
    internal_key: str | None = None,
) -> None:
    """Fire-and-forget alert broadcast to Relay."""
    try:
        headers = {"Accept": "application/json, text/event-stream"}
        headers.update(_internal_key_headers(internal_key))
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{relay_url}/mcp",
                json={
                    "jsonrpc": "2.0", "method": "tools/call", "id": 1,
                    "params": {"name": "relay_broadcast", "arguments": {
                        "channel": "alerts", "sender": "sentinel",
                        "content": f"[{severity.upper()}] {source}: {summary}",
                        "tags": ["alert", severity, source],
                    }},
                },
                headers=headers,
            )
    except Exception:
        logger.debug("Alert broadcast to Relay failed (non-critical)")


async def push_event(
    redis: Redis,
    source: str,
    event_type: str,
    summary: str,
    details: dict | None = None,
    severity: str = "info",
    tags: list[str] | None = None,
    maxlen: int = 10000,
) -> str:
    """Add an event to the Redis stream. Returns the stream entry ID."""
    fields = {
        "source": source,
        "event_type": event_type,
        "summary": summary,
        "details": json.dumps(details or {}),
        "severity": severity,
        "tags": json.dumps(tags or []),
        "timestamp": str(time.time()),
    }
    entry_id: str = await redis.xadd(STREAM_KEY, fields, maxlen=maxlen, approximate=True)

    # Replay: trace environment change
    try:
        from replay.emitter import emit as replay_emit, is_enabled as replay_is_enabled
        if replay_is_enabled():
            await replay_emit(
                "env_change", session_id="sentinel", agent_id="sentinel",
                payload={"source": source, "event_type": event_type, "severity": severity, "summary": summary[:200]},
            )
    except Exception:
        pass  # Non-critical

    # Alert: broadcast error+ events to Relay + fire Cortex webhooks
    try:
        from app.config import get_settings
        _settings = get_settings()
        alert_sevs = [s.strip() for s in _settings.ALERT_SEVERITIES.split(",")]
        if severity in alert_sevs:
            asyncio.create_task(_broadcast_alert(
                _settings.RELAY_URL, source, severity, summary,
                _settings.FIREKEEP_INTERNAL_KEY,
            ))
            asyncio.create_task(_fire_cortex_webhook(
                source, severity, summary, event_type,
                _settings.FIREKEEP_INTERNAL_KEY,
            ))
    except Exception:
        pass  # Non-critical

    return entry_id


async def get_events(
    redis: Redis,
    source: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    since: float | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read events from the stream with optional filtering.

    Returns events in reverse chronological order (newest first).
    """
    # XREVRANGE returns newest-first
    start = "+"
    end = "-"
    if since is not None:
        end = f"{int(since * 1000)}-0"

    raw = await redis.xrevrange(STREAM_KEY, start, end, count=limit * 5)

    results: list[dict] = []
    for entry_id, fields in raw:
        if source and fields.get("source") != source:
            continue
        if event_type and fields.get("event_type") != event_type:
            continue
        if severity and fields.get("severity") != severity:
            continue

        results.append({
            "id": entry_id,
            "source": fields.get("source", ""),
            "event_type": fields.get("event_type", ""),
            "summary": fields.get("summary", ""),
            "details": json.loads(fields.get("details", "{}")),
            "severity": fields.get("severity", "info"),
            "tags": json.loads(fields.get("tags", "[]")),
            "timestamp": float(fields.get("timestamp", 0)),
        })
        if len(results) >= limit:
            break

    return results


async def get_event_count(redis: Redis) -> int:
    """Return the number of events in the stream."""
    try:
        return await redis.xlen(STREAM_KEY)
    except Exception:
        return 0


async def trim_by_age(redis: Redis, max_age_hours: int) -> int:
    """Remove events older than max_age_hours. Returns count of deleted entries."""
    cutoff_ms = int((time.time() - max_age_hours * 3600) * 1000)
    # XRANGE from beginning to cutoff
    old_entries = await redis.xrange(STREAM_KEY, "-", f"{cutoff_ms}-0")
    if not old_entries:
        return 0

    ids = [entry_id for entry_id, _ in old_entries]
    deleted = await redis.xdel(STREAM_KEY, *ids)
    logger.info("Retention trim: deleted %d events older than %dh", deleted, max_age_hours)
    return deleted
