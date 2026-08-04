"""The chunked Celery task for the Dreaming pass: gate -> lock -> one unit of
work -> record. See docs/superpowers/specs/2026-08-04-dreaming-design.md.

Structure mirrors app/owm.py: a pure gate function, an async run_one_unit that
builds and closes its own clients, and a thin sync Celery wrapper that
self-gates on DREAM_ENABLED before ever calling run_one_unit. celery_app is
imported at the BOTTOM, matching app/owm.py and app/collectors/confluence.py —
see the comment at that import for why (a defensive convention, not a fix for
an active import cycle; the cycle claim in an earlier version of this
docstring was checked and found false).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LOCK_KEY = "dreams:lock"

# Two SEPARATE scan caps (fix-round review, dreaming Task 6/7, I6 + I7) — a
# single shared _SCAN_LIMIT=2000 conflated two different concerns with two
# different costs:
#
#   I7 (performance): select.cluster() is O(n^2) in candidate count. Measured
#   on this machine with synthetic 1024-dim vectors, cluster() (post-fix,
#   norm-precomputed): ~3.5s @500, ~14.1s @1000, ~58.8s @2000. Against
#   soft_time_limit=150s/time_limit=180s — which also has to leave room for
#   one synthesis call (~22.5s per the design doc's ground truth, up to
#   DREAM_SYNTH_TIMEOUT_SECONDS=120s worst case) plus Qdrant/Redis overhead —
#   2000 leaves too little margin. 1000 (~14s clustering) leaves generous
#   headroom.
#
#   I6 (correctness): the activity scan's `last_write_at` signal takes
#   max(timestamp) over the scanned page(s), but Qdrant scroll pages by point
#   ID, uncorrelated with timestamp. Above the scan cap, the true most-recent
#   write can land on an unscanned page and idle detection can wrongly read
#   "idle" while an agent is actively working. The correct fix is a single
#   `scroll(limit=1, order_by=...)` server-side lookup, but that requires a
#   payload index on `timestamp` (Qdrant enforces this for order_by) whose
#   behaviour could not be verified end-to-end — no live Qdrant was reachable
#   in the environment this fix round was done in. Per review guidance, the
#   accepted fallback is a larger, DECOUPLED cap for this scan: it does no
#   O(n^2) work (pure Python cost measured at 1.5ms for 6000 payloads — the
#   real cost is Qdrant network paging, not CPU), so it can afford a much
#   higher ceiling than the candidate scan without touching the time budget.
#   KNOWN LIMIT, honestly stated: above _ACTIVITY_SCAN_LIMIT active
#   non-dream memories, idle detection can still be wrong in the same way —
#   just at ~9x the pool size this was ever actually measured at (538, per
#   the design doc's ground truth). Revisit with a real order_by+DATETIME
#   payload-index fix once a live Qdrant is available to validate against.
_ACTIVITY_SCAN_LIMIT = 5000
_CANDIDATE_SCAN_LIMIT = 1000
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
    memories, and neither dream insights nor person profiles (source=
    "dream_profile", written by profile.py) may count as "new" activity or
    feed back into clustering. That is precisely the self-feeding loop the
    design calls out: an activity gate on any store-level counter is
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


def _profile_done_key(group_key: tuple[str, str, str, str]) -> str:
    """Stable dedupe key for one (member_id, workspace_id, namespace,
    project) profile group. A round-2 review finding: a plain `"::".join(...)`
    is ambiguous whenever a component itself contains "::" — member_id/
    workspace_id/namespace/project are free-form strings with no such
    guarantee, so two DIFFERENT groups could collide onto the SAME joined
    string (e.g. ("a::b", "c", "", "") and ("a", "b::c", "", "") both join to
    "a::b::c::::"). Hashing the tuple sidesteps the ambiguity entirely and
    mirrors select.cluster_key's own sha256-over-joined-ids approach."""
    joined = "\x1f".join(group_key)  # ASCII unit separator, still hashed below
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


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
    from app.dreams.select import parse_ts

    last_completed_dt = parse_ts(state.get_run().get("last_completed_at"))
    latest: datetime | None = None
    new_count = 0
    scanned = 0
    offset = None
    scope = _scope_filter()
    while scanned < _ACTIVITY_SCAN_LIMIT:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=scope,
            limit=min(_PAGE_SIZE, _ACTIVITY_SCAN_LIMIT - scanned),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        for p in points:
            scanned += 1
            ts = parse_ts((p.payload or {}).get("timestamp"))
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
    while scanned < _CANDIDATE_SCAN_LIMIT:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=scope,
            limit=min(_PAGE_SIZE, _CANDIDATE_SCAN_LIMIT - scanned),
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


def _is_backend_unavailable(exc: Exception) -> bool:
    """True when a call failed because the GENERATION backend is absent or
    unreachable (an embed-only deploy has no chat model) rather than a
    genuine per-call error. Mirrors app/knowledge/classifier.py's
    `_is_backend_unavailable` predicate exactly (fix-round review I5) —
    duplicated rather than imported, since that one is private to an
    unrelated package and this predicate is small/pure enough that
    duplication is cheaper and safer than a cross-package private import."""
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException)):
        return True
    if (isinstance(exc, httpx.HTTPStatusError) and exc.response is not None
            and exc.response.status_code == 404):
        return True
    msg = str(exc).lower()
    return "model" in msg and ("not found" in msg or "does not exist" in msg
                               or "not exist" in msg)


async def _generation_backend_available(settings) -> bool:
    """Gate #2 from the design spec (docs/superpowers/specs/2026-08-04-
    dreaming-design.md), not covered by Task 6's original brief — added in
    the fix round (I5). Before this, if the LLM backend was down,
    synthesize()/synthesize_profile() would return []/None as designed, but
    run_one_unit had no way to tell "backend unreachable" apart from "this
    particular cluster/member genuinely can't be synthesized" — so it marked
    every unit done anyway, walking the ENTIRE backlog to a false
    status=complete with zero insights written. On the office CPU deploy,
    generation-offline is a documented NORMAL state (see the corpus_only
    precedent in app/knowledge/classifier.py), so this was likely the
    default behaviour there, not an edge case.

    A cheap pre-flight probe: GET {LLM_BASE_URL}/models, the standard
    OpenAI-compatible listing endpoint Ollama also implements. Short,
    DEDICATED timeout — this is a health check, not a generation call, and
    must not eat into the tick's soft_time_limit budget. Any failure that
    ISN'T backend-unavailable (auth error, 500, malformed body) means the
    backend answered — treated as available, so synthesis attempts the real
    call and fails on its own terms rather than being pre-empted here.

    NOT independently verified against a live backend in this fix round (no
    Ollama/Qdrant reachable in the environment the round was done in) —
    the /models convention is standard OpenAI-compat surface and Night
    Shift's client-side backend detection (client/firekeep_client, see root
    CLAUDE.md) uses the same convention, but this specific call has only
    been exercised against mocked httpx transports (see
    test_dreams_task.py's backend-unavailable tests)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.LLM_BASE_URL}/models")
            resp.raise_for_status()
        return True
    except Exception as exc:
        if _is_backend_unavailable(exc):
            return False
        return True


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
    """Gate -> lock -> AT MOST one cluster (or, if none remain and profiles
    are enabled, one person profile) -> record. Returns after one unit; never
    loops over clusters, never raises.

    The single Celery worker runs --concurrency=1 --pool=solo, so a long task
    blocks every other periodic task (including the 60s agent-gateway
    sweeper). Beat fires this every DREAM_TICK_MINUTES; progress (which
    clusters are already consolidated) persists in Redis via DreamState, so a
    backlog is worked off across many short ticks instead of one long one.
    """
    from app.dreams import profile, select, store, synthesize
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

        # Design-spec gate #2 (fix-round review I5): if the generation
        # backend is down, don't walk the backlog marking every cluster/
        # profile "done" with zero insights — bail out before touching any
        # of it, and don't stamp completion either (there IS a backlog, we
        # just can't work it right now).
        if not await _generation_backend_available(settings):
            state.record_run(status="unavailable", health="unavailable")
            return {"status": "unavailable"}

        candidates = await _scroll_candidates(vector, settings, now=now)
        clusters = select.select_clusters(
            candidates,
            threshold=settings.DREAM_CLUSTER_THRESHOLD,
            min_size=settings.DREAM_MIN_CLUSTER,
            max_clusters=settings.DREAM_MAX_CLUSTERS_PER_RUN,
        )

        # One bulk read instead of one is_unit_done (SISMEMBER) call per
        # cluster (fix-round review M6 — the candidate-scan-scale version of
        # this same pattern hits the profile grouping loop below harder, but
        # applying it here too costs nothing and keeps both loops consistent).
        clusters_done = state.done_set("cluster")
        target = None
        for cl in clusters:
            key = select.cluster_key(cl)
            if key not in clusters_done:
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
            # to 3 insights. write_dream's `index` param (fix-round review,
            # store.py minor) distinguishes the resulting point ids WITHOUT
            # touching `cluster_key` itself, so the stored `dream_cluster_key`
            # provenance always names the real cluster — index==0 (insight 0)
            # reproduces the pre-fix point id exactly. `members` is
            # select_clusters' own output, so it is never empty/undersized
            # here (min_size is enforced upstream) — store.build_dream_payload
            # has no empty-members guard of its own, so this invariant must
            # hold at the call site.
            for i, insight in enumerate(insights):
                point_id = await store.write_dream(
                    vector, insight, members, cluster_key=key, run_id=run_id, index=i,
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

        # No cluster remains. If profiles are enabled, spend this tick's ONE
        # unit of work on the (member, tenancy-partition) group with the most
        # not-yet-profiled memories in this run's candidate pool (the same
        # pool already scanned above for clustering — a second full scroll is
        # not worth a second Qdrant pass in the same tick). is_candidate
        # already excludes dream/dream_profile-authored and confirmed points,
        # so a profile can never be built from another profile or from a
        # human-confirmed memory being re-synthesized against its will.
        #
        # Grouping key (fix-round review C1 — CRITICAL): must be
        # (member_id, *select.partition_key(payload)) — i.e. (member_id,
        # workspace_id, namespace, project) — NOT member_id alone. Grouping
        # on member_id alone let one member's memories from TWO workspaces be
        # synthesized into ONE profile, stamped with whichever workspace
        # happened to be first in Qdrant scroll order — content from a
        # workspace a reader has no access to could leak into a profile that
        # workspace's principal-scoped recall then legitimately surfaces.
        # That same scroll-order instability also meant the SAME member could
        # get a DIFFERENT workspace_id (and therefore a different
        # store.profile_point_id) on different runs, leaving two live profile
        # points for one member with no mechanism to reconcile or retire the
        # stale one. Grouping on the full tenancy partition makes every
        # group homogeneous by construction (mirrors how select.select_clusters
        # itself partitions BEFORE clustering) and makes which point a given
        # group writes to a function of real data, not scroll order.
        if settings.DREAM_PROFILES_ENABLED:
            profiles_done = state.done_set("profile")  # M6: one bulk read
            by_group: dict[tuple[str, str, str, str], list] = {}
            for c in candidates:
                member_id = str(c.payload.get("member_id") or "")
                if not member_id:
                    continue
                group_key = (member_id, *select.partition_key(c.payload))
                if _profile_done_key(group_key) in profiles_done:
                    continue
                by_group.setdefault(group_key, []).append(c)

            if by_group:
                group_key, members = max(
                    by_group.items(), key=lambda kv: len(kv[1])
                )
                member_id, workspace_id, namespace, project = group_key
                done_key = _profile_done_key(group_key)

                written = False
                if not workspace_id:
                    # M5 (round 2: moved ABOVE the synthesis call — the
                    # original ordering awaited synthesize_profile FIRST and
                    # only checked workspace_id afterward, burning a full LLM
                    # call on a group that was always going to be discarded;
                    # this check is pure and free, so it must run first). A
                    # candidate lacking workspace_id yields "" from
                    # partition_key — writing to profile_point_id(member_id,
                    # "") would be a permanently unreadable point (no real
                    # workspace could ever match it). Skip synthesis AND the
                    # write, but still mark the group done below so it isn't
                    # retried every tick.
                    logger.warning(
                        "Dream profile group for member %s has no workspace_id "
                        "(namespace=%r project=%r) — skipping synthesis and write",
                        member_id, namespace, project,
                    )
                else:
                    run_id = uuid.uuid4().hex
                    memories = [
                        {"text": m.text, "timestamp": m.payload.get("timestamp")}
                        for m in members
                    ]
                    raw_text = await profile.synthesize_profile(
                        member_id,
                        memories,
                        base_url=settings.LLM_BASE_URL,
                        model=settings.LLM_MODEL,
                        api_key=settings.LLM_API_KEY,
                        timeout=settings.DREAM_SYNTH_TIMEOUT_SECONDS,
                        max_chars=settings.DREAM_MAX_INSIGHT_CHARS,
                    )
                    # I2: derive namespace/project from the (now homogeneous)
                    # group instead of hardcoding "default"/None — project is
                    # a hard `must` filter in VectorClient.search, so a
                    # profile stamped project=None when its source memories
                    # actually carried one was invisible to every
                    # project-scoped recall.
                    if raw_text:
                        await profile.write_profile(
                            vector, raw_text, member_id=member_id,
                            workspace_id=workspace_id, run_id=run_id,
                            namespace=namespace or "default", project=project or None,
                        )
                        written = True
                # Mark the GROUP done regardless of whether synthesis was
                # attempted or produced usable text — a group that can't be
                # profiled (missing workspace_id, or the LLM can't produce
                # text) must not be retried forever within the same run
                # (mirrors the cluster branch's zero-insights handling above).
                state.mark_unit_done("profile", done_key)
                done = state.bump_counter("profiles_done")
                state.record_run(
                    status="ok", health="ok", profiles_done=done,
                    last_profile_member_id=member_id,
                )
                return {
                    "status": "ok", "unit": "profile", "member_id": member_id,
                    "workspace_id": workspace_id, "written": written,
                }

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


# celery_app is imported at the BOTTOM, matching app/owm.py and
# app/collectors/confluence.py:102-110.
#
# Corrected reasoning (fix-round review — the original comment here, copied
# from confluence.py's, claimed this avoids an import CYCLE via
# sleep_cycle.py's `include` list; that claim was checked directly and is
# FALSE for this codebase's actual import structure. `include` is Celery
# config DATA — a list of dotted module names — and is only resolved by
# Celery's loader lazily, at worker/beat startup; `_create_celery_app()`
# never imports those modules as a side effect of building the Celery app.
# Verified empirically: importing sleep_cycle alone does not add
# app.dreams.task to sys.modules, and moving this import to the TOP of the
# file and running it both ways (task-first, sleep_cycle-first) plus the
# full 1426-test suite all passed with no cycle of any kind.
#
# So this placement is a defensive convention, not a fix for an active bug:
# every module in sleep_cycle.py's `include` list imports celery_app back
# from the very module that names it, and keeping that import lexically LAST
# is a zero-cost guard against a FUTURE change to sleep_cycle.py's eagerness
# (e.g. an eager import_default_modules() call added there for some other
# reason) making the reverse-import order matter when it doesn't today. It
# also keeps every Celery task module in this codebase following one
# uniform, auditable pattern rather than several.
from app.workers.sleep_cycle import celery_app  # noqa: E402


# IMPORTANT (round-2 review finding, confirmed against docker-compose.yml:437):
# soft_time_limit/time_limit below are declared for correctness under a
# future PREFORK pool, but the current worker runs --pool=solo, and Celery's
# solo pool silently IGNORES both — no SoftTimeLimitExceeded is ever raised,
# no hard kill ever happens. They are decorative on this deployment today.
# The control that actually binds is DREAM_SYNTH_TIMEOUT_SECONDS
# (app/config.py — see its own comment for the 45s reasoning), enforced by
# httpx INSIDE synthesize()/synthesize_profile(), not by Celery.
#
# If the worker pool is ever switched to prefork, this task's broad
# `except Exception` (below) and run_one_unit's own outer `except Exception`
# will each catch celery.exceptions.SoftTimeLimitExceeded too, since it IS
# an Exception subclass — so simply switching pools would NOT make the soft
# limit start firing in practice; it would still be swallowed here just like
# any other failure. Whoever makes that switch must narrow both `except`
# clauses (e.g. `except SoftTimeLimitExceeded: raise` before the generic
# handler) or the limit will keep looking like it's enforced while doing
# nothing, which is worse than the current honestly-decorative state.
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
