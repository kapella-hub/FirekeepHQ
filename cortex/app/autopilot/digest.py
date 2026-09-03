"""The "what changed this week" digest.

WHY THIS IS ONE SCROLL AND NOT EIGHT. Every number here comes from an ISO
timestamp in a Qdrant payload, and this codebase does not range-match on those:
`Range` appears exactly twice, both times on numeric fields (`confirmed_count`,
`owm_n`). `app/dreams/task.py` records why — server-side ordering or ranging on
`timestamp` needs a payload index Qdrant enforces for `order_by`, which has
never been created or validated against a live Qdrant. So the established idiom
is `app/workers/gc.py`'s: scroll pages, classify in Python. Eight filtered
scrolls would each have to walk the collection anyway, so this walks it once and
derives every count from the same pass — which also makes the counts mutually
consistent, since they describe one snapshot rather than eight.

WHY THE COUNTS SAY WHEN THEY ARE APPROXIMATE. Scroll pages by point ID, which
is uncorrelated with timestamp, so a capped scan is an arbitrary sample and not
"the most recent N". Above the cap these numbers under-report by an unknown
amount, and the response says so (`approximate: true`, plus `scanned`) rather
than presenting a sample as a census. This is the same known limit, honestly
stated, that the dreaming activity scan carries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.dreams.select import parse_ts

logger = logging.getLogger(__name__)

# Decoupled from the dreaming caps on purpose: this pass does no O(n^2) work
# (one dict lookup per point), so its ceiling is set by Qdrant paging cost, not
# CPU. 5000 matches `_ACTIVITY_SCAN_LIMIT`, the other scan in this repo sized by
# the same reasoning.
SCAN_CAP = 5000
SCAN_BATCH = 500

# Sources that are not "a memory somebody learned": corpus chunks are ingested
# documents and dream output is derived. Same exclusion set the dreaming
# candidate selector applies, for the same reason — counting them as learned
# memories would report the system's own output back as its input.
_DERIVED_SOURCES = {"corpus", "dream", "dream_profile"}

MIN_DAYS, MAX_DAYS = 1, 90


def clamp_days(days: int) -> int:
    return max(MIN_DAYS, min(int(days), MAX_DAYS))


def _in_window(raw: object, cutoff: datetime) -> bool:
    ts = parse_ts(raw)
    return ts is not None and ts >= cutoff


async def scan_payloads(vector, settings) -> tuple[list[dict], bool]:
    """One bounded walk of the collection. Returns (payloads_with_ids, capped)."""
    out: list[dict] = []
    offset = None
    capped = False
    while True:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            limit=SCAN_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            payload = dict(p.payload or {})
            payload["__id"] = str(p.id)
            out.append(payload)
        if offset is None or not points:
            break
        if len(out) >= SCAN_CAP:
            capped = True
            break
    return out, capped


def count_from_payloads(payloads: list[dict], cutoff: datetime) -> dict[str, int]:
    """Every payload-derived digest number, from one pass over one snapshot."""
    learned = archived = superseded = dreams = drafted = activated = feedback = 0

    # `memories_superseded` prefers the `superseded_at` stamp (written by
    # update_status and the deep pass since 0.4.0). Supersessions from before
    # the stamp existed fall back to the keeper heuristic below: supersession
    # at learn-time happens synchronously inside the learn that causes it
    # (`contradiction.py` supersedes the old memory while storing the new one),
    # so "superseded by a memory written in the window" is "superseded in the
    # window" — for THAT path. The heuristic misses supersessions under a
    # pre-existing keeper (nightly deep pass, contested verdicts), which is
    # exactly why the stamp was added.
    written_in_window: set[str] = set()
    legacy_superseded_by: list[str] = []

    for payload in payloads:
        source = str(payload.get("source", "") or "")
        mem_type = str(payload.get("memory_type", "") or "")
        status = str(payload.get("status", "active") or "active")
        fresh = _in_window(payload.get("timestamp"), cutoff)

        if fresh and source not in _DERIVED_SOURCES and mem_type != "skill":
            written_in_window.add(payload["__id"])
            if status == "active":
                learned += 1

        if source == "dream" and fresh:
            dreams += 1

        if mem_type == "skill":
            if fresh:
                drafted += 1
            # Documented PROXY. `stale_reviewed_at` is the closest thing to an
            # activation time that exists, but it is stamped by TWO events:
            # promoting a draft to active, and a human clearing the stale flag
            # ("Still valid"). So this counts human blessings of a skill, of
            # which activation is one kind — it is not a pure activation count,
            # and the response says so rather than letting the label imply a
            # precision the store cannot supply.
            if status != "archived" and str(payload.get("skill_status", "")) == "active" \
                    and _in_window(payload.get("stale_reviewed_at"), cutoff):
                activated += 1

        if _in_window(payload.get("archived_at"), cutoff):
            archived += 1

        # A LAST-at field, so repeated feedback on one memory collapses to one:
        # this counts memories that received feedback in the window, not
        # feedback events. Naming it honestly in the notes is cheaper than
        # storing an event log nobody asked for.
        if _in_window(payload.get("feedback_last_at"), cutoff):
            feedback += 1

        if status == "superseded":
            if payload.get("superseded_at"):
                if _in_window(payload.get("superseded_at"), cutoff):
                    superseded += 1
            else:
                sb = payload.get("superseded_by")
                if sb:
                    legacy_superseded_by.append(str(sb))

    superseded += sum(1 for sb in legacy_superseded_by if sb in written_in_window)

    return {
        "memories_learned": learned,
        "memories_archived": archived,
        "memories_superseded": superseded,
        "dream_insights": dreams,
        "skills_drafted": drafted,
        "skills_activated": activated,
        "feedback_given": feedback,
    }


async def count_gc_actions(redis_client, cutoff: datetime) -> int:
    """GC actions inside the window, from the eviction audit log.

    Legacy normalization copied from the dashboard's reader: entries written
    before the archive-first pass carry `evicted_at` and no `occurred_at`, and
    reading only `occurred_at` would silently score every one of them as
    out-of-window.
    """
    from app.workers.gc import GC_EVICTION_LOG_KEY

    raw_entries = await redis_client.lrange(GC_EVICTION_LOG_KEY, 0, 999)
    count = 0
    for raw in raw_entries or []:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        when = entry.get("occurred_at") or entry.get("evicted_at")
        if _in_window(when, cutoff):
            count += 1
    return count


def summarize(counts: dict[str, int], days: int, approximate: bool) -> str:
    """One sentence a human can read without opening anything else."""
    window = "the last 24 hours" if days == 1 else f"the last {days} days"

    def plural(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    clauses: list[str] = []
    if counts["memories_learned"]:
        clauses.append(f"learned {plural(counts['memories_learned'], 'memory', 'memories')}")
    if counts["memories_superseded"]:
        clauses.append(f"superseded {counts['memories_superseded']}")
    if counts["memories_archived"]:
        clauses.append(f"archived {counts['memories_archived']}")
    if counts["dream_insights"]:
        clauses.append(
            f"consolidated {plural(counts['dream_insights'], 'insight', 'insights')}"
        )
    if counts["skills_drafted"]:
        clauses.append(f"drafted {plural(counts['skills_drafted'], 'skill', 'skills')}")
    if counts["skills_activated"]:
        clauses.append(f"activated {counts['skills_activated']}")
    if counts["feedback_given"]:
        clauses.append(
            f"took feedback on {plural(counts['feedback_given'], 'memory', 'memories')}"
        )
    if counts["gc_actions"]:
        clauses.append(
            f"ran {plural(counts['gc_actions'], 'maintenance action', 'maintenance actions')}"
        )

    if not clauses:
        return f"No knowledge-base activity in {window}."

    if len(clauses) == 1:
        body = clauses[0]
    else:
        body = ", ".join(clauses[:-1]) + " and " + clauses[-1]
    prefix = "At least in" if approximate else "In"
    return f"{prefix} {window} Firekeep {body}."


def _ladder_reach(last_run: dict | None) -> dict[str, dict[str, Any]]:
    """`reach_by_tier`'s raw shown/reached/applied into {shown, reached, rate}.
    `rate` is None rather than 0.0 when nothing was shown — a fraction with a
    zero denominator is not a measurement of zero, it is no measurement, and
    this whole block exists to answer whether the evidence signals exist at
    all."""
    reach_by_tier = (last_run or {}).get("reach_by_tier") or {}
    out: dict[str, dict[str, Any]] = {}
    for tier in ("active", "trial"):
        tier_data = reach_by_tier.get(tier) or {}
        shown = int(tier_data.get("shown") or 0)
        reached = int(tier_data.get("reached") or 0)
        out[tier] = {
            "shown": shown,
            "reached": reached,
            "rate": round(reached / shown, 3) if shown else None,
        }
    return out


async def build_ladder_block(redis_client) -> dict[str, Any]:
    """The skill-ladder pass's (PR1, shadow) shadow reach, read-only and
    redis-only — everything it needs (`last_run`) is written by
    `app/skills/ladder.py`'s nightly pass, and nothing here touches Qdrant.
    `mode` reports what the last run actually did, never `settings` — a
    setting can change before the next run executes it."""
    from app.skills.ladder import read_last_run

    last_run = await read_last_run(redis_client)

    return {
        "mode": (last_run.get("mode") if last_run else None) or "shadow",
        "last_run": last_run,
        "trial_count": int((last_run or {}).get("trial_count") or 0),
        "reach": _ladder_reach(last_run),
    }


async def build_digest(vector, redis_client, settings, *, days: int,
                       now: datetime | None = None) -> dict[str, Any]:
    """Assemble the digest. Each source is guarded independently — a dead
    Redis must not cost the operator the Qdrant-derived numbers, and vice
    versa; a digest that is all-or-nothing reports nothing on the day one
    dependency is unhappy, which is the day it is most worth reading."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = clamp_days(days)
    cutoff = now - timedelta(days=days)

    errors: dict[str, str] = {}
    approximate = False
    scanned = 0
    counts: dict[str, int] = {
        "memories_learned": 0, "memories_archived": 0, "memories_superseded": 0,
        "dream_insights": 0, "skills_drafted": 0, "skills_activated": 0,
        "feedback_given": 0,
    }

    try:
        payloads, capped = await scan_payloads(vector, settings)
        scanned = len(payloads)
        approximate = capped
        counts.update(count_from_payloads(payloads, cutoff))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Autopilot digest: memory scan failed")
        errors["memories"] = str(exc)[:200]

    counts["gc_actions"] = 0
    try:
        counts["gc_actions"] = await count_gc_actions(redis_client, cutoff)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Autopilot digest: gc log read failed")
        errors["gc_actions"] = str(exc)[:200]

    fleet: dict[str, Any] = {"enabled": bool(getattr(settings, "FLEET_ENQUEUE_ENABLED", True)),
                             "jobs": {}}
    try:
        from app.fleet import ledger as _ledger
        fleet["jobs"] = await _ledger.summarize(redis_client, days=days, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Autopilot digest: fleet ledger read failed")
        errors["fleet"] = str(exc)[:200]

    ladder: dict[str, Any] = {
        "mode": "shadow", "last_run": None, "trial_count": 0,
        "reach": {"active": {"shown": 0, "reached": 0, "rate": None},
                 "trial": {"shown": 0, "reached": 0, "rate": None}},
    }
    try:
        ladder = await build_ladder_block(redis_client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Autopilot digest: ladder read failed")
        errors["ladder"] = str(exc)[:200]

    payload: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "window_days": days,
        "since": cutoff.isoformat(),
        "counts": counts,
        "approximate": approximate,
        "scanned": scanned,
        "summary": summarize(counts, days, approximate),
        "fleet": fleet,
        "ladder": ladder,
        "notes": [
            "skills_activated counts human blessings via stale_reviewed_at "
            "(promotion to active OR clearing the stale flag) — the store has "
            "no separate activation timestamp.",
            "feedback_given counts memories whose feedback_last_at falls in the "
            "window, so repeated feedback on one memory counts once.",
            "memories_superseded counts by the superseded_at stamp; supersessions "
            "from before the stamp existed are counted only when their keeper was "
            "written in the window, which undercounts old deep-pass supersessions.",
            "fleet.jobs rates are null when nothing has been approved or rejected yet "
            "— a rate is never invented from a prior; window counts sum UTC days.",
        ],
    }
    if approximate:
        payload["notes"].append(
            f"scan capped at {SCAN_CAP} points; scroll pages by point ID, not by "
            "time, so these counts under-report by an unknown amount."
        )
    if errors:
        payload["errors"] = errors
    return payload
