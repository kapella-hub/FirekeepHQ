"""The nightly skill-ladder pass (spec: docs/superpowers/specs/2026-09-03-skill-ladder-design.md).

SHADOW CONTRACT (PR1, binding). This pass NEVER changes `skill_status`, NEVER
writes to the fleet ledger (`fleet:ledger:ladder` must not exist after a run),
and NEVER enqueues anything. The only payload keys it may write via
`set_payload` are `ladder_since` (once, where missing), `ladder_shadow` (per
affected skill — the decision it *would* have made) and `duplicate_of` (on a
draft that failed the duplicate check). `SKILL_LADDER_MODE="enforce"` still
runs shadow this PR; the run record just carries a warning that enforce mode
ships in PR2.

This module is the orchestrator only: it gathers trial/active/draft skills
(three Qdrant scrolls), stamps `ladder_since` where missing, asks
`app.skills.ladder_evidence.gather` for evidence over trial+active skills, asks
`app.skills.ladder_rules` for at most one decision per skill, and records that
decision. It owns no promotion/demotion/duplicate THRESHOLDS itself — those
live in `ladder_rules` (thresholds) and `app.config.Settings` (schedule and
promotion knobs).

Per-skill fault isolation: a single skill's `since`/`trial`/`active`/`admit`
processing raising is caught, appended to the run record's `errors` list with
the failing stage, and never stops the rest of the pass. If evidence gathering
itself raises for the WHOLE batch (rather than one bad session, which
`ladder_evidence.gather` already isolates internally), that is recorded as one
error with `skill_id=None, stage="evidence"` and the pass continues to the
draft admission loop with no evidence for trial/active skills.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from app.skills.ladder_evidence import Evidence, gather
from app.skills.ladder_rules import (
    ADMIT_PER_RUN,
    Decision,
    admit_block_reason,
    decide_admit,
    decide_demote,
    decide_expire,
    decide_flag,
    decide_promote,
    default_ladder_since,
)

logger = logging.getLogger(__name__)

LOCK_KEY = "skills:ladder:lock"
DECISIONS_KEY = "skills:ladder:decisions"
LAST_RUN_KEY = "skills:ladder:last_run"

#: Newest-first, capped — mirrors the ladder_evidence failure-session cap's
#: rationale: enough for a human to spot-check without unbounded growth.
_MAX_DECISIONS = 500

#: Per-skill scroll ceiling (spec: "skills are few"). Generous enough that no
#: real deployment should ever hit it, cheap enough to bound a runaway loop.
_SCROLL_CEILING = 2000
_SCROLL_PAGE = 200

_ENFORCE_WARNING = "enforce mode ships in PR2 — ran shadow"

#: Maps a Decision's action to the run-record counter it increments. "admit"
#: is handled separately (it has its own skip categories alongside it).
_ACTION_TO_COUNT = {"expire": "expired", "demote": "demoted", "promote": "promoted"}


def _parse_since(value: str | None) -> datetime | None:
    """Parse an ISO-8601 `ladder_since` value defensively: naive -> assume
    UTC, unparsable/None -> None. Mirrors `ladder_rules._parse_dt` — kept
    local rather than imported so this module never reaches into another
    module's private helper."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class _DupCheckUnavailable(RuntimeError):
    """Raised by the default duplicate-check helper when the semantic search
    degraded to a plain scroll (embed backend down). Per controller ruling 2:
    a draft is NEVER admitted without a WORKING duplicate check — this is
    caught by the admission loop's own per-skill try/except and recorded as an
    `errors` entry (`stage="admit"`), never silently treated as "no match"."""


async def _scroll_status(vector, settings, status: str) -> list:
    """All skill points at one `skill_status`, paginated to `_SCROLL_CEILING`."""
    must = [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="skill_status", match=MatchValue(value=status)),
    ]
    out: list = []
    offset = None
    while True:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=Filter(must=must),
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        out.extend(points)
        if not offset or len(out) >= _SCROLL_CEILING:
            break
    return out


async def _default_dup_fn(vector, settings, payload: dict) -> tuple[str, float] | None:
    """Semantic duplicate check against active+trial skills.

    Fix round 1 (controller ruling 2's premise was wrong): this does NOT go
    through `search_skill_points`, because that helper's `semantic=False`
    covers THREE distinct paths — the embed failed, nothing cleared
    `SKILL_MATCH_SCORE_FLOOR`, or the filter matched zero points — and the
    third is the ordinary state of a fresh Keep with no active/trial skills
    yet. Keying "unavailable" off that flag deadlocked admission forever:
    with nothing to compare against, `query_points` legitimately returns
    nothing, which is "no duplicate", not "the check is broken".

    So this embeds directly with the same primitive `search_skill_points`
    uses (`_embed_with_cache_warm`) and treats ONLY an embed exception as
    "unavailable" (`_DupCheckUnavailable`). A successful embed always yields a
    real answer — `None` for zero hits, a real score otherwise — with no score
    floor: DUP_THRESHOLD (0.92) in `ladder_rules.admit_block_reason` is the
    only cutoff that matters here.
    """
    from app.skills.api import _skill_embed_text
    from app.skills.search import DEFAULT_EMBED_TIMEOUT, _embed_with_cache_warm, _float_setting

    timeout = _float_setting(settings, "SKILL_MATCH_EMBED_TIMEOUT_SECONDS", DEFAULT_EMBED_TIMEOUT)
    try:
        vec = await _embed_with_cache_warm(vector, _skill_embed_text(payload), timeout)
    except Exception as exc:  # noqa: BLE001 — the ONLY "unavailable" case is a broken embed
        raise _DupCheckUnavailable("dup_check_unavailable") from exc

    must = [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="skill_status", match=MatchAny(any=["active", "trial"])),
    ]
    # NOT wrapped: a genuine Qdrant failure here must propagate and be caught
    # by the admission loop's own per-skill try/except (stage="admit"), same
    # as every other draft-processing failure — it is not an "unavailable
    # duplicate check", it is a storage failure.
    results = await vector._client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=vec,
        query_filter=Filter(must=must),
        limit=1,
        with_payload=False,
    )
    points = list(results.points)
    if not points:
        return None
    top = points[0]
    return str(top.id), float(top.score)


async def _record(redis_client, vector, settings, decision: Decision, payload: dict,
                  now: datetime) -> None:
    """The ONLY two writes a decision produces: an LPUSH+LTRIM'd decision-log
    entry, and a `ladder_shadow` payload stamp on the skill itself."""
    now_iso = now.isoformat()
    entry = {
        "at": now_iso,
        "skill_id": decision.skill_id,
        "title": str(payload.get("trigger", ""))[:120],
        "action": decision.action,
        "from": decision.from_status,
        "to": decision.to_status,
        "reason": decision.reason,
        "evidence": decision.evidence,
        "mode": "shadow",
    }
    await redis_client.lpush(DECISIONS_KEY, json.dumps(entry))
    await redis_client.ltrim(DECISIONS_KEY, 0, _MAX_DECISIONS - 1)
    await vector._client.set_payload(
        collection_name=settings.QDRANT_COLLECTION,
        payload={"ladder_shadow": {
            "action": decision.action,
            "from": decision.from_status,
            "to": decision.to_status,
            "reason": decision.reason,
            "at": now_iso,
        }},
        points=[decision.skill_id],
    )


async def _stamp_since(vector, settings, points, errors: list) -> tuple[dict[str, str | None], int]:
    """For every point lacking `ladder_since`, stamp `default_ladder_since`
    where it resolves to something; return a skill_id -> effective-`ladder_since`
    map (existing, freshly stamped, or still None) for the REST of this run to
    use, so a second scroll is never needed to see the freshly-written value,
    plus how many stamps were written."""
    since_by_skill: dict[str, str | None] = {}
    stamped = 0
    for p in points:
        skill_id = str(p.id)
        payload = p.payload or {}
        existing = payload.get("ladder_since")
        if existing:
            since_by_skill[skill_id] = existing
            continue
        default = default_ladder_since(payload)
        if default is not None:
            try:
                await vector._client.set_payload(
                    collection_name=settings.QDRANT_COLLECTION,
                    payload={"ladder_since": default},
                    points=[skill_id],
                )
                stamped += 1
            except Exception as exc:  # noqa: BLE001 — one bad stamp never stops the rest
                errors.append({"skill_id": skill_id, "stage": "since", "error": str(exc)})
        since_by_skill[skill_id] = default
    return since_by_skill, stamped


async def run_ladder_impl(vector, replay_r, redis_client, settings, *, now=None,
                          events_fn=None, bridge_statuses=None, dup_fn=None) -> dict:
    """One nightly ladder pass. Returns the run record (or a `{"status": ...}`
    short-circuit for disabled/locked). Per-skill decision/admission failures
    are caught and appended to the run record's `errors` list rather than
    propagating; a Qdrant/redis outage during the three status scrolls, the
    `last_run` write, or `_stamp_since`'s own set_payload calls is NOT
    isolated the same way (`_stamp_since` isolates only per-point stamp
    failures, not a scroll-level outage) — those propagate to the caller,
    which for the Celery task means `run_skill_ladder`'s own try/except turns
    it into `{"status": "error", ...}` rather than a partial run record."""
    if not settings.SKILL_LADDER_ENABLED:
        return {"status": "disabled"}

    now = now or datetime.now(timezone.utc)
    lock_ttl = max(1, int(settings.SKILL_LADDER_SCHEDULE_HOURS)) * 3600
    acquired = await redis_client.set(LOCK_KEY, "1", nx=True, ex=lock_ttl)
    if not acquired:
        return {"status": "locked"}

    try:
        return await _run_locked(
            vector, replay_r, redis_client, settings, now=now,
            events_fn=events_fn, bridge_statuses=bridge_statuses, dup_fn=dup_fn,
        )
    finally:
        try:
            await redis_client.delete(LOCK_KEY)
        except Exception:  # noqa: BLE001 — a stuck lock self-heals via its TTL
            pass


async def _run_locked(vector, replay_r, redis_client, settings, *, now,
                      events_fn, bridge_statuses, dup_fn) -> dict:
    errors: list[dict] = []
    counts = {
        "expired": 0, "demoted": 0, "flagged": 0, "promoted": 0, "admitted": 0,
        "skipped_duplicate": 0, "skipped_capped": 0, "skipped_parked": 0,
        "skipped_incomplete": 0,
    }

    trial_points = await _scroll_status(vector, settings, "trial")
    active_points = await _scroll_status(vector, settings, "active")
    draft_points = await _scroll_status(vector, settings, "draft")

    since_map, stamped_since = await _stamp_since(
        vector, settings, [*trial_points, *active_points, *draft_points], errors,
    )

    since_trial_active: dict[str, datetime] = {}
    for p in (*trial_points, *active_points):
        skill_id = str(p.id)
        parsed = _parse_since(since_map.get(skill_id))
        if parsed is not None:
            since_trial_active[skill_id] = parsed

    try:
        evidence = await gather(
            replay_r, settings, since_by_skill=since_trial_active,
            events_fn=events_fn, bridge_statuses=bridge_statuses, now=now,
        )
    except Exception as exc:  # noqa: BLE001 — a total gather failure never aborts the pass
        errors.append({"skill_id": None, "stage": "evidence", "error": str(exc)})
        evidence = {}

    trial_ids = {str(p.id) for p in trial_points}
    active_ids = {str(p.id) for p in active_points}
    reach_by_tier = {
        "active": {"shown": 0, "reached": 0, "applied": 0},
        "trial": {"shown": 0, "reached": 0, "applied": 0},
    }
    for skill_id, ev in evidence.items():
        tier = "trial" if skill_id in trial_ids else "active" if skill_id in active_ids else None
        if tier is not None:
            reach_by_tier[tier]["shown"] += ev.shown
            reach_by_tier[tier]["reached"] += ev.reached
            reach_by_tier[tier]["applied"] += ev.applied

    for p in trial_points:
        skill_id = str(p.id)
        payload = dict(p.payload or {})
        try:
            ev = evidence.get(skill_id) or Evidence()
            ladder_since = since_map.get(skill_id)
            decision = decide_expire(
                skill_id, "trial", ev.last_shown_at, ladder_since, now,
                settings.SKILL_LADDER_TRIAL_TTL_DAYS,
            )
            if decision is None:
                decision = decide_demote(skill_id, "trial", ev, settings.OWM_PRIOR_N)
            if decision is None:
                decision = decide_promote(
                    skill_id, "trial", ev, settings.OWM_PRIOR_N,
                    min_successes=settings.SKILL_LADDER_PROMOTE_MIN_SUCCESSES,
                    min_agents=settings.SKILL_LADDER_PROMOTE_MIN_AGENTS,
                )
            if decision is not None:
                await _record(redis_client, vector, settings, decision, payload, now)
                counts[_ACTION_TO_COUNT[decision.action]] += 1
        except Exception as exc:  # noqa: BLE001 — one bad trial never stops the rest
            errors.append({"skill_id": skill_id, "stage": "trial", "error": str(exc)})

    for p in active_points:
        skill_id = str(p.id)
        payload = dict(p.payload or {})
        try:
            ev = evidence.get(skill_id) or Evidence()
            already_flagged = bool(payload.get("ladder_rewrite_requested_at"))
            decision = decide_flag(skill_id, "active", ev, settings.OWM_PRIOR_N, already_flagged)
            if decision is not None:
                await _record(redis_client, vector, settings, decision, payload, now)
                counts["flagged"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad active never stops the rest
            errors.append({"skill_id": skill_id, "stage": "active", "error": str(exc)})

    trial_domain_counts: dict[str, int] = {}
    for p in trial_points:
        domain = (p.payload or {}).get("domain") or ""
        trial_domain_counts[domain] = trial_domain_counts.get(domain, 0) + 1

    dup_fn_effective = dup_fn or _default_dup_fn
    # Oldest first by `timestamp`; a draft missing one sorts last (spec ruling
    # 6) rather than first, so an undated draft never jumps the queue.
    ordered_drafts = sorted(
        draft_points,
        key=lambda p: (0, (p.payload or {}).get("timestamp"))
        if (p.payload or {}).get("timestamp") else (1, ""),
    )
    for p in ordered_drafts:
        skill_id = str(p.id)
        payload = dict(p.payload or {})
        try:
            domain = payload.get("domain") or ""
            domain_trial_count = trial_domain_counts.get(domain, 0)

            # Cheap pre-check: incomplete/rereview/parked never depend on
            # dup_match or domain_trial_count, so they are decided before
            # either costs anything. `domain_cap` is deliberately NOT
            # decided here — ladder_rules orders `duplicate` before
            # `domain_cap` (a draft that is both must be recorded as a
            # duplicate, not silently re-bucketed as capped), so that
            # decision waits for the real dup_match below.
            prelim = admit_block_reason(payload, dup_match=None, domain_trial_count=0)
            if prelim in ("incomplete", "rereview"):
                counts["skipped_incomplete"] += 1
                continue
            if prelim and prelim.startswith("parked:"):
                counts["skipped_parked"] += 1
                continue

            # Per-run cap BEFORE the duplicate lookup (ruling 6: "stop after
            # ADMIT_PER_RUN admits") — a draft beyond the cap must never pay
            # for an embed + Qdrant search it cannot use.
            if counts["admitted"] >= ADMIT_PER_RUN:
                counts["skipped_capped"] += 1
                continue

            dup_match = await dup_fn_effective(vector, settings, payload)
            reason = admit_block_reason(payload, dup_match=dup_match, domain_trial_count=domain_trial_count)
            if reason is not None:
                if reason.startswith("duplicate:"):
                    dup_id = reason.split(":", 1)[1]
                    await vector._client.set_payload(
                        collection_name=settings.QDRANT_COLLECTION,
                        payload={"duplicate_of": dup_id},
                        points=[skill_id],
                    )
                    counts["skipped_duplicate"] += 1
                elif reason == "domain_cap":
                    counts["skipped_capped"] += 1
                else:
                    # incomplete/rereview/parked already excluded by the
                    # pre-check above on this same payload — unreachable in
                    # practice, kept only as a defensive fallback.
                    counts["skipped_incomplete"] += 1
                continue

            decision = decide_admit(skill_id, payload, dup_match=dup_match,
                                    domain_trial_count=domain_trial_count)
            if decision is not None:
                await _record(redis_client, vector, settings, decision, payload, now)
                counts["admitted"] += 1
                trial_domain_counts[domain] = domain_trial_count + 1
        except Exception as exc:  # noqa: BLE001 — one bad draft never stops the rest
            errors.append({"skill_id": skill_id, "stage": "admit", "error": str(exc)})

    run_record: dict = {
        "mode": "shadow",
        "at": now.isoformat(),
        "expired": counts["expired"],
        "demoted": counts["demoted"],
        "flagged": counts["flagged"],
        "promoted": counts["promoted"],
        "admitted": counts["admitted"],
        "skipped_duplicate": counts["skipped_duplicate"],
        "skipped_capped": counts["skipped_capped"],
        "skipped_parked": counts["skipped_parked"],
        "skipped_incomplete": counts["skipped_incomplete"],
        "stamped_since": stamped_since,
        "trial_count": len(trial_points),
        "reach_by_tier": reach_by_tier,
        "errors": errors,
    }
    if settings.SKILL_LADDER_MODE == "enforce":
        run_record["warning"] = _ENFORCE_WARNING
        logger.warning(_ENFORCE_WARNING)

    await redis_client.set(LAST_RUN_KEY, json.dumps(run_record))
    return run_record


async def _run_ladder_impl() -> dict:
    import redis.asyncio

    from app.config import get_settings
    from app.db.vector import VectorClient
    from app.owm import _fetch_bridge_statuses

    settings = get_settings()
    if not settings.SKILL_LADDER_ENABLED:
        return {"status": "disabled"}
    replay_r = redis.asyncio.from_url(settings.RP_REDIS_URL, decode_responses=True)
    redis_client = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    vector = VectorClient(settings)
    try:
        statuses = await _fetch_bridge_statuses(settings)
        return await run_ladder_impl(vector, replay_r, redis_client, settings,
                                     bridge_statuses=statuses)
    finally:
        for closer in (replay_r.aclose, redis_client.aclose, vector.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


# Import placement is load-bearing (confluence-collector precedent, mirrored by
# app.owm): celery_app is imported at the BOTTOM so this module's public
# surface exists first.
from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.skills.ladder.run_skill_ladder")
def run_skill_ladder() -> dict:
    """Beat fires unconditionally; the task self-gates on SKILL_LADDER_ENABLED
    and never raises — failures come back as a status dict."""
    import asyncio

    try:
        return asyncio.run(_run_ladder_impl())
    except Exception as exc:  # noqa: BLE001
        logger.exception("skill ladder pass crashed")
        return {"status": "error", "error": str(exc)}
