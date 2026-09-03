"""Per-job-type approval counters for fleet output (spec decision 7).

WHY A LEDGER AND NOT A QUERY. Rejection of a draft skill is DELETION, and no
approval timestamp existed before this feature, so an approval rate read from
Qdrant would lose every rejected draft and flatter the fleet. These are
monotonic Redis counters written at the moments the store forgets: create
(`produced`), draft->active (`approved`), delete-while-draft (`rejected`), a
first verdict proposal (`proposed`), a human verdict on a pair that carried a
proposal (`resolved`, plus `matched` when the human agreed). All-time hash plus
one hash per UTC day (400-day TTL) so the digest can window them.

Every writer is best-effort: a Redis hiccup must never fail the skill write or
the verdict that triggered it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

JOB_DISTILL = "distill_session"
JOB_REAUTHOR = "reauthor_stale_skill"
JOB_VERDICT = "propose_contested_verdict"
JOB_LADDER = "ladder"   # not a fleet job: the skill ladder's own transitions (spec 2026-09-03 decision 8)
JOBS = (JOB_DISTILL, JOB_REAUTHOR, JOB_VERDICT, JOB_LADDER)

SKILL_COUNTERS = ("produced", "approved", "rejected")
VERDICT_COUNTERS = ("proposed", "resolved", "matched")
LADDER_COUNTERS = ("admitted", "promoted", "demoted", "expired", "rewrite_requested")
_COUNTERS = {JOB_DISTILL: SKILL_COUNTERS, JOB_REAUTHOR: SKILL_COUNTERS,
             JOB_VERDICT: VERDICT_COUNTERS, JOB_LADDER: LADDER_COUNTERS}

LEDGER_PREFIX = "fleet:ledger"
DAILY_TTL_SECONDS = 400 * 86400
REJECTED_TTL_SECONDS = 90 * 86400
# Equals relay's TASK_TTL_SECONDS: the JOB_VERDICT marker must not outlive the
# task it guards -- a proposal is a state the store remembers (proposed_verdict/
# proposed_at), so there is nothing to gain from holding the marker any longer.
# JOB_REAUTHOR uses the longer TTL below instead.
LIVE_MARKER_TTL_SECONDS = 7 * 86400
# A `still_valid`/`retire` verdict on JOB_REAUTHOR writes nothing to the store
# (only `resolve_contested`'s verdict path is a state the store remembers), and
# `stale` clears only on recall -- so with the same 7-day TTL as the relay task
# itself, a rewrite the model declined or a human has not acted on gets
# re-posted every week forever, burning a cap slot and a local-model call for
# no new information. A rewrite is retried monthly instead; the verdict job
# keeps the 7-day relay-TTL marker because a proposal IS a state the store
# remembers (proposed_verdict/proposed_at on both points).
REAUTHOR_LIVE_MARKER_TTL_SECONDS = 30 * 86400


def total_key(job: str) -> str:
    return f"{LEDGER_PREFIX}:{job}"


def day_key(job: str, day: str) -> str:
    return f"{LEDGER_PREFIX}:{job}:{day}"


def rejected_reauthor_key(skill_id: str) -> str:
    return f"fleet:rejected:{JOB_REAUTHOR}:{skill_id}"


def live_marker_key(job: str, subject: str) -> str:
    return f"fleet:enqueued:{job}:{subject}"


def rate(numer: int, denom: int) -> float | None:
    """None when there is no evidence — a rate is never invented from a prior."""
    return None if denom <= 0 else round(numer / denom, 3)


async def record(redis_client, job: str, counter: str, *, now: datetime | None = None) -> bool:
    if redis_client is None or job not in _COUNTERS or counter not in _COUNTERS[job]:
        return False
    try:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        pipe = redis_client.pipeline()
        pipe.hincrby(total_key(job), counter, 1)
        pipe.hincrby(day_key(job, day), counter, 1)
        pipe.expire(day_key(job, day), DAILY_TTL_SECONDS)
        await pipe.execute()
        return True
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails the caller
        logger.warning("fleet ledger write skipped (%s/%s): %s", job, counter, exc)
        return False


async def mark_rejected_reauthor(redis_client, skill_id: str) -> None:
    if redis_client is None or not skill_id:
        return
    try:
        await redis_client.set(rejected_reauthor_key(skill_id), "1", ex=REJECTED_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet rejection marker skipped for %s: %s", skill_id, exc)


def _ints(raw: dict | None, counters: tuple[str, ...]) -> dict[str, int]:
    """Normalize an `HGETALL` reply and read out `counters` as ints.

    The app's real redis client is constructed without `decode_responses=True`
    (`redis.asyncio.from_url(settings.REDIS_URL)` in `app/main.py`), so
    `HGETALL` returns `{b"produced": b"5", ...}` — bytes KEYS, not just bytes
    values. Decoding only the value and looking it up with a `str` key (the
    original bug) misses every entry and silently reports all zeros. Decode
    both, matching the house pattern in `app/procedures/store.py`'s `_smap`.
    """
    raw = raw or {}
    decoded = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
    out: dict[str, int] = {}
    for c in counters:
        v = decoded.get(c)
        if isinstance(v, bytes):
            v = v.decode()
        try:
            out[c] = int(v or 0)
        except (TypeError, ValueError):
            out[c] = 0
    return out


def _with_rate(counts: dict[str, int], job: str) -> dict:
    if job == JOB_LADDER:
        return {**counts}
    if job == JOB_VERDICT:
        return {**counts, "match_rate": rate(counts["matched"], counts["resolved"])}
    return {**counts, "approval_rate": rate(counts["approved"],
                                            counts["approved"] + counts["rejected"])}


async def summarize(redis_client, *, days: int, now: datetime | None = None) -> dict[str, dict]:
    """Window (last `days` UTC days, today included) and all-time, per job.

    Raises on a Redis failure — the digest's per-source guard owns the catch,
    so a dead Redis degrades the fleet block in place like every other source.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = max(1, int(days))
    out: dict[str, dict] = {}
    for job in JOBS:
        counters = _COUNTERS[job]
        window = {c: 0 for c in counters}
        for i in range(days):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            for c, v in _ints(await redis_client.hgetall(day_key(job, day)), counters).items():
                window[c] += v
        total = _ints(await redis_client.hgetall(total_key(job)), counters)
        all_time = _with_rate(total, job)
        if job in (JOB_DISTILL, JOB_REAUTHOR):
            all_time["pending"] = max(0, total["produced"] - total["approved"] - total["rejected"])
        out[job] = {"window": _with_rate(window, job), "all_time": all_time}
    return out
