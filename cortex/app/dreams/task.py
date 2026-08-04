"""The chunked Celery task for the Dreaming pass: gate -> lock -> one unit of
work -> record. See docs/superpowers/specs/2026-08-04-dreaming-design.md.

Structure mirrors app/owm.py: a pure gate function, an async run_one_unit that
builds and closes its own clients, and a thin sync Celery wrapper that
self-gates on DREAM_ENABLED before ever calling run_one_unit. celery_app is
imported at the BOTTOM (app/collectors/confluence.py:102-110 is the
precedent) — sleep_cycle.py's `include` list names this module, so importing
`celery_app` from sleep_cycle at the top of this file would run into that
module's own import machinery before this module's public surface exists.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LOCK_KEY = "dreams:lock"

# Bounds one Qdrant scroll pass. The live active pool is ~500 points (measured
# 2026-08-04, per the design doc's ground-truth table); this is generous
# headroom while still bounding worst-case scan cost on a much larger store.
# Applies independently to the (vector-less) activity scan and the
# (with-vectors) candidate scan.
_SCAN_LIMIT = 2000
_PAGE_SIZE = 256


def should_run(
    *,
    enabled: bool,
    now: datetime,
    last_write_at: datetime | None,
    idle_minutes: int,
    new_memories: int,
    min_new: int,
) -> tuple[bool, str]:
    """Pure ordered gate. Each closed check returns its own reason string so
    the caller can persist WHY a tick did nothing. Order: disabled -> not
    enough new memories -> not idle (no point counting new memories, let
    alone checking idleness, for a store the operator hasn't opted into
    consolidating).

    `last_write_at=None` (no non-dream write has ever been observed) is
    vacuously idle — there is nothing recent to be active about.
    """
    if not enabled:
        return False, "disabled"
    if new_memories < min_new:
        return False, f"no new memories ({new_memories}<{min_new})"
    if last_write_at is not None:
        elapsed_minutes = (now - last_write_at).total_seconds() / 60.0
        if elapsed_minutes < idle_minutes:
            return False, f"not idle (last write {elapsed_minutes:.0f}m ago)"
    return True, "ok"


def _scope_filter():
    """Active, non-corpus, non-dream-authored points — the pool both the
    activity gate and candidate selection scan.

    Excludes source in {corpus, dream, dream_profile}: corpus chunks aren't
    memories, and neither dream insights nor person profiles (Task 7, not yet
    implemented — writes with source="dream_profile") may count as "new"
    activity or feed back into clustering. That is precisely the self-feeding
    loop the design calls out: an activity gate on any store-level counter is
    satisfied by dreaming's own output unless dream-authored writes are
    excluded at the source.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(
        must=[FieldCondition(key="status", match=MatchValue(value="active"))],
        must_not=[
            FieldCondition(key="source", match=MatchValue(value="corpus")),
            FieldCondition(key="source", match=MatchValue(value="dream")),
            FieldCondition(key="source", match=MatchValue(value="dream_profile")),
        ],
    )


async def _activity_metrics(vector, settings, state) -> tuple[datetime | None, int]:
    """One scroll pass (no vectors — this is a cheap pre-check before the
    heavier candidate scan) computing the two signals should_run needs: the
    most recent non-dream write (idle detection) and how many landed after
    the last COMPLETED run (work-available detection).

    Anchored on `last_completed_at`, a field this module writes only when a
    run finishes (no cluster or profile left) — deliberately NOT
    `state.last_run_at()`. Every tick calls `record_run()`, including
    "skipped"/"ok" mid-run ticks, so last_run_at() advances on every
    invocation; anchoring new_memories on it would recompute the count
    against a few-minutes-old timestamp on tick 2 of a multi-tick run and
    could stall consolidation of a backlog that was large enough to open the
    gate in the first place.
    """
    from app.dreams.select import _parse_ts

    last_completed_dt = _parse_ts(state.get_run().get("last_completed_at"))
    latest: datetime | None = None
    new_count = 0
    scanned = 0
    offset = None
    scope = _scope_filter()
    while scanned < _SCAN_LIMIT:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=scope,
            limit=min(_PAGE_SIZE, _SCAN_LIMIT - scanned),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        for p in points:
            scanned += 1
            ts = _parse_ts((p.payload or {}).get("timestamp"))
            if ts is None:
                continue
            if latest is None or ts > latest:
                latest = ts
            if last_completed_dt is None or ts > last_completed_dt:
                new_count += 1
        if offset is None:
            break
    return latest, new_count


async def _scroll_candidates(vector, settings, *, now: datetime) -> list:
    """Page through the same scope WITH vectors (clustering needs them),
    keeping only points is_candidate() accepts (episodic-or-missing type,
    unconfirmed, old enough, not OWM-excluded)."""
    from app.dreams.select import Candidate, is_candidate

    out: list[Candidate] = []
    scanned = 0
    offset = None
    scope = _scope_filter()
    while scanned < _SCAN_LIMIT:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=scope,
            limit=min(_PAGE_SIZE, _SCAN_LIMIT - scanned),
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        for p in points:
            scanned += 1
            payload = p.payload or {}
            if is_candidate(
                payload,
                now=now,
                min_age_days=settings.DREAM_MIN_AGE_DAYS,
                owm_floor=settings.DREAM_OWM_FLOOR,
                owm_prior_n=settings.OWM_PRIOR_N,
            ):
                out.append(Candidate(
                    id=str(p.id),
                    text=str(payload.get("text", "")),
                    vector=list(p.vector or []),
                    payload=payload,
                ))
        if offset is None:
            break
    return out


async def _build_clients():
    """Client construction, isolated as its own seam so tests can short-
    circuit it without a live Redis/Qdrant (see
    test_disabled_task_returns_status_without_building_clients).

    Redis is the SYNC client: DreamState (state.py) makes plain blocking
    calls, mirroring memory_agent.py's lock/counter pattern — not
    collectors/state.py's async one.
    """
    import redis

    from app.config import get_settings
    from app.db.vector import VectorClient

    settings = get_settings()
    redis_client = redis.Redis.from_url(settings.REDIS_URL)
    vector = VectorClient(settings)
    return redis_client, vector, settings


async def run_one_unit() -> dict:
    """Gate -> lock -> AT MOST one cluster (or, once Task 7's profile.py
    lands, one profile) -> record. Returns after one unit; never loops over
    clusters, never raises.

    The single Celery worker runs --concurrency=1 --pool=solo, so a long task
    blocks every other periodic task (including the 60s agent-gateway
    sweeper). Beat fires this every DREAM_TICK_MINUTES; progress (which
    clusters are already consolidated) persists in Redis via DreamState, so a
    backlog is worked off across many short ticks instead of one long one.
    """
    from app.dreams import select, store, synthesize
    from app.dreams.state import DreamState

    redis_client = None
    vector = None
    lock_acquired = False
    try:
        redis_client, vector, settings = await _build_clients()
        state = DreamState(redis_client)

        lock_acquired = bool(
            redis_client.set(LOCK_KEY, "1", nx=True, ex=settings.DREAM_LOCK_TTL_SECONDS)
        )
        if not lock_acquired:
            return {"status": "locked"}

        now = datetime.now(timezone.utc)
        last_write_at, new_memories = await _activity_metrics(vector, settings, state)
        ok, reason = should_run(
            enabled=settings.DREAM_ENABLED,
            now=now,
            last_write_at=last_write_at,
            idle_minutes=settings.DREAM_IDLE_MINUTES,
            new_memories=new_memories,
            min_new=settings.DREAM_MIN_NEW_MEMORIES,
        )
        if not ok:
            state.record_run(status="skipped", reason=reason, health="ok")
            return {"status": "skipped", "reason": reason}

        candidates = await _scroll_candidates(vector, settings, now=now)
        clusters = select.select_clusters(
            candidates,
            threshold=settings.DREAM_CLUSTER_THRESHOLD,
            min_size=settings.DREAM_MIN_CLUSTER,
            max_clusters=settings.DREAM_MAX_CLUSTERS_PER_RUN,
        )

        target = None
        for cl in clusters:
            key = select.cluster_key(cl)
            if not state.is_unit_done("cluster", key):
                target = (key, cl)
                break

        if target is not None:
            key, members = target
            run_id = uuid.uuid4().hex
            insights = await synthesize.synthesize(
                members,
                base_url=settings.LLM_BASE_URL,
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                timeout=settings.DREAM_SYNTH_TIMEOUT_SECONDS,
                max_chars=settings.DREAM_MAX_INSIGHT_CHARS,
            )
            written = []
            # A cluster has ONE cluster_key, but synthesize() may return up
            # to 3 insights, and store.dream_point_id is derived from
            # cluster_key alone — writing every insight under the bare key
            # would have each overwrite the last. Suffix with the insight's
            # index instead: still deterministic/idempotent (re-processing
            # the same cluster lands on the same points), and every insight
            # survives. `members` is select_clusters' own output, so it is
            # never empty/undersized here (min_size is enforced upstream) —
            # store.build_dream_payload has no empty-members guard of its
            # own, so this invariant must hold at the call site.
            for i, insight in enumerate(insights):
                point_id = await store.write_dream(
                    vector, insight, members, cluster_key=f"{key}:{i}", run_id=run_id
                )
                written.append(point_id)
            # Mark the CLUSTER done regardless of insight count (including
            # zero) — a cluster the LLM can't synthesize must not be retried
            # forever (synthesize() already logs the failure at WARNING).
            state.mark_unit_done("cluster", key)
            done = state.bump_counter("clusters_done")
            state.record_run(
                status="ok", health="ok", clusters_done=done,
                last_cluster_key=key, insights_written=len(written),
            )
            return {
                "status": "ok", "unit": "cluster", "cluster_key": key,
                "insights": len(written),
            }

        # No cluster remains. Task 7 (app.dreams.profile — not yet
        # implemented) will wire a profile unit in here: pick the member
        # with the most not-yet-profiled memories this run, synthesize,
        # write via store.profile_point_id + vector.upsert_point, and
        # mark_unit_done("profile", member_id). Until profile.py lands,
        # DREAM_PROFILES_ENABLED has no effect and a run with no remaining
        # clusters always completes.

        state.reset_progress()
        state.record_run(status="complete", health="ok", last_completed_at=now.isoformat())
        return {"status": "complete"}
    except Exception as exc:
        logger.exception("Dream unit failed")
        if redis_client is not None:
            try:
                err_state = DreamState(redis_client)
                err_state.bump_counter("errors")
                err_state.record_run(status="error", health="degraded", error=str(exc)[:500])
            except Exception:
                logger.debug("Failed to record dream error state")
        return {"status": "error", "error": str(exc)}
    finally:
        if lock_acquired and redis_client is not None:
            try:
                redis_client.delete(LOCK_KEY)
            except Exception:
                logger.debug("Dream lock release failed")
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:
                logger.debug("Dream redis close failed")
        if vector is not None:
            try:
                await vector.close()
            except Exception:
                logger.debug("Dream vector close failed")


# Import placement is load-bearing (confluence-collector precedent, app/
# collectors/confluence.py:102-110): celery_app is imported at the BOTTOM so
# this module's public surface (should_run, run_one_unit, _build_clients —
# the test's monkeypatch target) is fully defined before sleep_cycle.py's own
# import machinery (triggered by its `include` list naming this module) runs.
from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.dreams.task.run_dream_tick", soft_time_limit=150, time_limit=180)
def run_dream_tick() -> dict:
    """Beat fires unconditionally; the task self-gates on DREAM_ENABLED
    BEFORE building any client (see
    test_disabled_task_returns_status_without_building_clients) and never
    raises — failures come back as a status dict."""
    from app.config import get_settings

    if not get_settings().DREAM_ENABLED:
        return {"status": "disabled"}
    try:
        return asyncio.run(run_one_unit())
    except Exception as exc:
        logger.exception("Dream tick crashed")
        return {"status": "error", "error": str(exc)}
