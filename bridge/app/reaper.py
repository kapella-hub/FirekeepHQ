"""Crashed-session reaper — converts idle "active" sessions into abandoned ones.

WHY THIS EXISTS. A session whose agent died, crashed, or simply walked away
never calls ctx_complete_session, so it sits at status="active" forever: no TTL
is ever applied (only complete/abandon set one), it is never distilled, and it
is never evaluated. It does not fail — it *vanishes*.

That vanishing is one half of the measured outcome-degeneracy defect recorded in
docs/guides/memory-and-recall.md (OWM section). OWM ranks recall by real-world
results, and the load-bearing failure signal it has is Bridge's "abandoned"
status, which overrides an eval to failure. Sessions that die silently never
acquire that status, so the population OWM scores is the population that
survived long enough to report — and effectively every scored session reads as a
success. The reaper closes that gap by giving a crashed session the terminal
state it earned, which is what makes the failure signal exist at all.

KNOWN TRADEOFF, deliberate: abandonment does NOT distill. A reaped session's
plan, decisions and progress are discarded when the TTL lapses — that is the
existing abandon semantic and this module does not change it. Recovering
knowledge from failed sessions is future work; the outcome signal is the point
now. Anyone tempted to make the reaper distill instead should note that would
also invert the OWM signal it exists to produce.

The reaper deliberately owns none of the abandonment mechanics. Per session it
calls SessionManager.abandon_session (which owns status, active-pointer cleanup,
the 7-day TTL on all seven keys, and the `session.abandoned` replay event) and
then mcp_server.after_abandon (the `session_end` event plus the eval trigger).
Flipping status directly would recreate the dangling-active-pointer bug
documented at session.py:143-163, and skipping after_abandon would produce a
session abandoned in Redis but invisible to the scoring this exists to feed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.config import Settings, get_settings
from app.redis_client import get_redis
from app.session import SessionManager

logger = logging.getLogger(__name__)


async def reap_pass(redis: aioredis.Redis, settings: Settings) -> dict[str, int]:
    """Abandon every session that has been "active" and silent past the cutoff.

    Returns ``{"scanned", "reaped", "skipped"}``. ``scanned`` counts every index
    entry the cutoff query returned, and ``scanned == reaped + skipped`` always
    holds — a dangling index entry counts as scanned and skipped, not as a third
    thing, so the numbers in a log line add up.
    """
    summary = {"scanned": 0, "reaped": 0, "skipped": 0}
    if not settings.REAPER_ENABLED:
        return summary

    # nb:sessions is scored by LAST ACTIVITY, not by creation: SessionManager
    # zadds a fresh timestamp on every ctx_update (session.py:333). That is what
    # makes a score-bounded scan the right enumeration — a long-running session
    # that is still being written to keeps scoring above the cutoff and is never
    # a candidate. list_sessions is NOT usable here: it is limit-bounded, so it
    # would silently reap only the first N crashed sessions on a busy deploy.
    cutoff = (
        datetime.now(timezone.utc).timestamp()
        - settings.REAPER_IDLE_HOURS * 3600
    )
    # Bounded per pass (REAPER_MAX_PER_PASS): the first pass on an existing
    # deployment faces months of backlog, and each reaped session costs several
    # Redis round trips plus a detached eval POST. zrangebyscore returns the
    # LOWEST scores first — the longest-idle sessions — so the cap drains the
    # backlog oldest-first across consecutive hourly passes, and nothing is
    # missed, only deferred.
    candidates = await redis.zrangebyscore(
        SessionManager.INDEX_KEY, "-inf", cutoff,
        start=0, num=settings.REAPER_MAX_PER_PASS,
    )
    if not candidates:
        return summary

    mgr = SessionManager(redis, settings)

    for sid in candidates:
        summary["scanned"] += 1
        try:
            meta = await redis.hgetall(mgr._session_key(sid))

            # Index entry whose metadata has already expired. Self-heal exactly
            # as list_sessions does (session.py:622-624) — this is the only
            # sweep that visits the oldest end of the index on a schedule, so
            # leaving the tombstone means it is never cleaned up.
            if not meta:
                await redis.zrem(SessionManager.INDEX_KEY, sid)
                summary["skipped"] += 1
                continue

            # paused / completed / abandoned already carry a decision somebody
            # made and a TTL policy of their own. Only "active" is the crashed
            # state with no terminal transition ahead of it.
            if meta.get("status") != "active":
                summary["skipped"] += 1
                continue

            owner = meta.get("agent_id") or None
            await mgr.abandon_session(session_id=sid, agent_id=owner)
            # Same post-abandon effects as an explicit ctx_abandon_session, plus
            # reaped=True so a replay reader can tell the reaper's work from a
            # human's. Imported lazily: mcp_server imports this module from its
            # lifespan, and a module-level import here would close that cycle.
            from app.mcp_server import after_abandon

            await after_abandon(sid, owner or settings.DEFAULT_AGENT_ID, reaped=True)
            summary["reaped"] += 1
            logger.info(
                "Reaped crashed session %s (agent=%s, idle > %dh)",
                sid, owner or settings.DEFAULT_AGENT_ID, settings.REAPER_IDLE_HOURS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Per-session isolation: one malformed or racing session must not
            # abort the sweep and strand every candidate behind it.
            summary["skipped"] += 1
            logger.warning("Reaper skipped session %s: %s", sid, exc)

    if summary["reaped"]:
        logger.info(
            "Reaper pass complete: scanned=%d reaped=%d skipped=%d",
            summary["scanned"], summary["reaped"], summary["skipped"],
        )
    return summary


async def reaper_loop(
    interval_seconds: float | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running reaper loop. Started from the MCP server lifespan.

    Registered unconditionally and gated per pass rather than at startup, so
    flipping NB_REAPER_ENABLED needs no change to the lifespan wiring — the same
    idiom the rest of the stack uses for feature flags.
    """
    settings = get_settings()
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.REAPER_INTERVAL_SECONDS
    )
    logger.info(
        "Session reaper started (enabled=%s, idle=%dh, interval=%ss)",
        settings.REAPER_ENABLED, settings.REAPER_IDLE_HOURS, interval,
    )
    while stop_event is None or not stop_event.is_set():
        try:
            redis = await get_redis()
            await reap_pass(redis, get_settings())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Reaper pass failed (will retry): %s", exc)
        await asyncio.sleep(interval)
