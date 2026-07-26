"""Sweeper for overdue agent actions — marks outcome_unknown for entries near TTL expiry."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def sweep_overdue_actions(prediction_redis, replay_emitter, grace_seconds: int = 30) -> int:
    """Find prediction entries near expiry and emit outcome_unknown events.

    Scans Redis for ``ag:predict:*`` keys whose remaining TTL falls below
    ``grace_seconds``.  For each such key, emits an ``agent.action.reconcile``
    event with ``outcome="unknown"`` and then deletes the key.

    Args:
        prediction_redis: async Redis client for the prediction store
        replay_emitter: async callable accepting keyword args
            ``event_type, session_id, agent_id, payload``
        grace_seconds: emit outcome_unknown when entry's remaining TTL
            falls below this threshold (default 30 s)

    Returns:
        Number of entries swept.
    """
    if prediction_redis is None:
        return 0

    swept = 0
    try:
        cursor = 0
        while True:
            cursor, keys = await prediction_redis.scan(
                cursor, match="ag:predict:*", count=100
            )
            for key in keys:
                try:
                    ttl = await prediction_redis.ttl(key)
                    # TTL == -2: key expired between scan and ttl call
                    # TTL == -1: no TTL set (skip — shouldn't happen in practice)
                    # TTL >  grace_seconds: not yet near expiry
                    if ttl is None or ttl == -2 or ttl == -1 or ttl > grace_seconds:
                        continue
                    raw = await prediction_redis.get(key)
                    if not raw:
                        continue
                    entry = json.loads(raw)
                    # Key is always str when decode_responses=True
                    action_id = key.split(":")[-1]
                    await replay_emitter(
                        event_type="agent.action.reconcile",
                        session_id=entry.get("session_id", ""),
                        agent_id=entry.get("agent_id", ""),
                        payload={
                            "action_id": action_id,
                            "outcome": "unknown",
                            "source": "sweeper",
                            "prediction_match_score": None,
                        },
                    )
                    await prediction_redis.delete(key)
                    swept += 1
                except Exception as exc:
                    logger.warning("sweep error on key %s: %s", key, exc)
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning("sweep failed: %s", exc)
    return swept


# ---------------------------------------------------------------------------
# Celery wrapper — registered in the beat schedule in sleep_cycle.py
# ---------------------------------------------------------------------------

try:
    from app.workers.sleep_cycle import celery_app

    @celery_app.task
    def sweep_overdue_actions_task():
        import asyncio
        import redis.asyncio as aioredis
        from app.config import get_settings

        async def _run():
            settings = get_settings()
            # Use the replay Redis URL (DB 6) — same DB as main.py wires prediction_redis
            try:
                from replay.config import get_replay_settings
                rp_url = get_replay_settings().REDIS_URL
            except Exception:
                rp_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")

            client = aioredis.from_url(rp_url, decode_responses=True)

            # Initialize and use the replay emitter directly
            try:
                from replay.emitter import init_emitter, emit as _emit
                await init_emitter(redis_url=rp_url)

                async def _emitter(**kwargs):
                    try:
                        await _emit(**kwargs)
                    except Exception as exc:
                        logger.warning("sweep emitter failed: %s", exc)
            except Exception:
                async def _emitter(**kwargs):
                    return None

            try:
                count = await sweep_overdue_actions(client, _emitter)
                return count
            finally:
                await client.aclose()

        try:
            return asyncio.run(_run())
        except Exception as exc:
            logger.warning("sweep_overdue_actions_task failed: %s", exc)
            return 0

except ImportError:
    pass  # Celery not available in this environment
