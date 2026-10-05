"""Replay retention — the beat task that enforces RP_RETENTION_DAYS on rp:events.

``replay.emitter.trim_old_events`` existed but nothing scheduled it, so the
replay stream was bounded only by RP_STREAM_MAXLEN while each event's
``rp:eid:{id}`` lookup key expired at the retention horizon: an old event
stayed visible to the unscoped in-process readers (evals, OWM, the pattern
engine) and to none of the scoped REST reads. THREAT-MODEL §5.18.

Runs every RP_TRIM_INTERVAL_SECONDS (daily) from cortex-beat. Each run opens
its own client on the replay DB and closes it: a worker never calls
init_emitter, and a module-global client must not outlive the loop
``asyncio.run`` closes. The trim is idempotent and safe to overlap, so no
lock is taken; beat is a single instance anyway.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def trim_replay_retention(redis_client=None, settings=None) -> int:
    """Trim the replay stream once. Returns the number of events removed."""
    import redis.asyncio as aioredis

    from replay.config import get_replay_settings
    from replay.emitter import trim_old_events

    settings = settings or get_replay_settings()
    owned = redis_client is None
    client = redis_client or aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        trimmed = await trim_old_events(client, settings)
    finally:
        if owned:
            await client.aclose()
    if trimmed:
        logger.info("replay retention: trimmed %d event(s) older than %d days",
                    trimmed, settings.RETENTION_DAYS)
    return trimmed


try:
    from app.workers.sleep_cycle import celery_app

    @celery_app.task(name="app.workers.replay_trim.trim_replay_events")
    def trim_replay_events() -> int:
        try:
            return asyncio.run(trim_replay_retention())
        except Exception as exc:  # noqa: BLE001 — a failed trim retries on the next beat
            logger.warning("trim_replay_events failed: %s", exc)
            return 0

except ImportError:
    pass  # Celery not available in this environment
