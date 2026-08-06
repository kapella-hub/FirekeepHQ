"""The nightly hardening pass.

TWO TIERS, and the split is the design's answer to a measured weakness rather
than a hedge:

  Tier A — frequency. Needs no outcome signal. "This procedure ran 41 times;
           step 3 was skipped in 24 of them" is true and useful on day one.
  Tier B — efficacy verdicts. Gated on executions whose sessions have a KNOWABLE
           outcome AND on that outcome actually DISCRIMINATING. Measured on this
           repo, _failure_rate is 0.0 for a session no event marked as failing,
           and the reconcile emit now stamps post_tool's `success`, which
           defaults to True — so effectively every session reads as a success.
           Counting knowable outcomes alone therefore opens the gate on a signal
           that separates nothing, and the efficacy comparison degenerates into
           a Beta-prior artefact of bucket size: it proposes deleting steps that
           were never once associated with a failure, "confidently, with
           statistics" (spec §F1 consequence 2). Both halves are required.
           Closed is the correct state; it reports WHICH closed state it is in.

AND THE DISCRIMINATION CHECK HOLDS WHERE THE COMPARISON IS MADE — PER STEP.
The `tier_b` string below is summed over every execution of every skill, so ONE
failing session anywhere in the deployment flipped it to `open` while the thing
it authorised is a per-step comparison; applied to a step whose own buckets are
uniformly successful it emits `dead_step` on a signal that separated nothing —
the same defect one level down. `tier_b` stays because an operator needs to know
which state the DEPLOYMENT is in; `_discriminates` is what authorises a verdict.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from app.procedures import store

logger = logging.getLogger(__name__)

# Tier B verdict states, reported verbatim through GET /procedures.
TIER_B_OPEN = "open"
TIER_B_INSUFFICIENT = "insufficient outcome signal"
TIER_B_UNIFORM = "uniform outcome signal"


async def _bridge_statuses(settings) -> dict[str, str]:
    """Session-status map from Bridge, via OWM's own fetcher.

    Spec F1 names Bridge `abandoned` as one of only TWO failure signals this
    system actually produces today, and this pass hardcoded `bridge_status=None`
    — discarding the signal most likely to legitimately open its own gate and
    making its "uniform outcome signal" verdict partly self-inflicted.

    OWM's helper is REUSED rather than reimplemented: it already carries the
    internal key, the 200-session clamp the Bridge route imposes, and the
    degrade-to-empty-on-any-failure contract. A second copy is a second thing to
    keep true.
    """
    try:
        from app.owm import _fetch_bridge_statuses

        return await _fetch_bridge_statuses(settings) or {}
    except Exception as exc:  # noqa: BLE001 — never fail the pass for this
        logger.warning("procedure pass: bridge statuses unavailable (%s) — "
                       "eval-only outcomes", exc)
        return {}


async def _resolve_outcome(replay_r, session_id: str,
                           bridge_status: str | None = None) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    Excludes on ANY doubt: no replay events, no outcome-bearing event (I4 —
    _failure_rate returns 0.0 in that case, which reads as success), an eval
    that cannot be computed, or session_success's ambiguous middle band.

    `bridge_status` is what makes `abandoned` count: `session_success` treats it
    as failure regardless of metrics, which is exactly the case F1 describes —
    every edit succeeded mechanically, `failure_rate` is 0.0, and the human
    walked away. I4 still runs first, so an abandoned session that carries no
    outcome-bearing replay event at all is still excluded (§6, H7).
    """
    if replay_r is None or not session_id:
        return None
    try:
        from replay.reader import get_session_timeline

        timeline = await get_session_timeline(replay_r, session_id, limit=1000)
        events = (timeline or {}).get("events") or []
        # I4 is checked BEFORE the eval is fetched, not after: an eval whose
        # failure_rate is 0.0 because nothing carried an outcome is
        # indistinguishable from a genuinely clean session once you have it.
        if not any(e.get("outcome") for e in events):
            return None

        from app.evals.store import get_eval

        ev = await get_eval(replay_r, session_id)
        if ev is None:
            # The evals router's own docstring records a live incident where
            # every stored eval was trigger="manual" and 12 days stale against
            # 54 completed sessions — a pass that only READ evals would have
            # almost no sample.
            from app.evals.compute import compute_session_eval

            ev = await compute_session_eval(replay_r, session_id, trigger="manual")
        if ev is None:
            return None

        from app.owm import session_success

        data = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
        return session_success(data, bridge_status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome unresolved for %s: %s", session_id, exc)
        return None


def _within_window(rec: dict, cutoff: datetime) -> bool:
    stamp = rec.get("last_seen_at") or rec.get("opened_at")
    if not stamp:
        return True  # undated: keep rather than silently drop evidence
    try:
        dt = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


_WS = re.compile(r"\s+")


def _flat(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def _spec_drift(skills: list[dict]) -> dict[str, list[dict]]:
    """Tier A, and it needs no executions at all — it compares two stored things.

    A spec's `text` is self-contained by design (it does not index into the
    skill's `## Steps` markdown), which is exactly why the two can silently
    diverge: a human PATCHes `content` and the specs still describe the old
    body. That is the one drift nothing else in the system can see, and it
    applies to `unobservable` steps too — the body was edited without the specs
    whether or not round 1 can watch the step.

    Substring over whitespace-flattened, case-folded text: a paraphrase reads as
    drift, which is a proposal to a human, dismissible and regenerated nightly,
    not a mutation.
    """
    out: dict[str, list[dict]] = {}
    for skill in skills:
        specs = skill.get("step_specs")
        if not isinstance(specs, list) or not specs:
            continue
        body = _flat(skill.get("content") or "")
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            text = (spec.get("text") or "").strip()
            step_id = spec.get("id")
            if not text or not step_id:
                continue
            if _flat(text) in body:
                continue
            out.setdefault(skill["skill_id"], []).append({
                "id": uuid.uuid4().hex[:12], "kind": "spec_drift",
                "skill_id": skill["skill_id"], "step_id": step_id,
                "detail": (f"Step \"{text}\" no longer appears in this skill's "
                           f"body — it was edited without the specs. Recompile "
                           f"the step specs?"),
            })
    return out


async def run_pass(redis_client, replay_r, vector, settings) -> dict:
    """One full hardening pass. Never raises for a single bad execution."""
    if not getattr(settings, "PROCEDURE_ENABLED", False):
        return {"status": "disabled"}

    min_n = int(getattr(settings, "PROCEDURE_MIN_EXECUTIONS", 5))
    prior_n = int(getattr(settings, "PROCEDURE_PRIOR_N", 5))
    delta = float(getattr(settings, "PROCEDURE_EFFICACY_DELTA", 0.15))
    cap = int(getattr(settings, "PROCEDURE_AGENT_CAP", 5))
    window = int(getattr(settings, "PROCEDURE_WINDOW_DAYS", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=window)

    # Self-healing: the index is also rebuilt on every spec write, but that
    # write-path rebuild fires only when step_specs are touched — a draft->active
    # PATCH changes index MEMBERSHIP without touching a spec. Rebuilding
    # unconditionally here means a missed write can never strand the index.
    #
    # ONE scan, two readers: the index rebuild and spec_drift both need every
    # active skill's payload, and a second scroll of the same collection would
    # only be a second chance to disagree with the first.
    skills: list[dict] = []
    scan_ok = True
    try:
        skills = await store.scan_active_skills(vector, settings)
        await store.write_index(redis_client, skills, settings)
    except Exception as exc:  # noqa: BLE001
        scan_ok = False
        logger.warning("index rebuild failed during hardening: %s", exc)

    drift = _spec_drift(skills)
    index = await store.load_index(redis_client)
    steps_by_skill: dict[str, list[dict]] = {}
    for entry in index:
        if not isinstance(entry, dict) or not entry.get("skill_id"):
            continue
        steps_by_skill.setdefault(entry["skill_id"], []).append(entry)

    executions = await store.iter_executions(redis_client)
    # Fetched ONCE per pass, and only when there is something to score — an
    # idle deployment must not make an HTTP call to say nothing happened.
    bridge_statuses = await _bridge_statuses(settings) if executions else {}
    # tallies[skill][step] -> counters
    tallies: dict[str, dict[str, dict]] = {}
    # agent_seen[skill][step][agent] -> count, for the fairness cap
    agent_seen: dict[str, dict[str, dict[str, int]]] = {}
    outcome_backed = 0
    outcome_success = outcome_failure = 0

    for rec in executions:
        skill_id = rec.get("skill_id")
        if skill_id not in steps_by_skill:
            continue
        if not _within_window(rec, cutoff):
            continue
        # I2, and it is checked against the steps the procedure HAS, not against
        # whatever ids the execution happens to name. Spec ids are minted
        # server-side and no surface returns them, so a wording edit re-keys
        # every step; the stored executions then name ids that no longer exist.
        # "This execution observed something" is satisfied by those, and every
        # CURRENT step was then tallied `skipped` — 40 real executions rendering
        # as "observed 0 / skipped 40" for steps performed in all 40.
        live_ids = {e["step_id"] for e in steps_by_skill[skill_id]}
        observed_ids = set(rec.get("observed") or {}) & live_ids
        # No sibling evidence => this execution says nothing about any step.
        # Without it every kiro session (fs_write maps to `other`), every
        # shell-heavy session and every personal-mode session votes to delete
        # each load-bearing step it never had the ability to observe.
        if not observed_ids:
            continue

        session_id = rec.get("session_id") or ""
        outcome = await _resolve_outcome(
            replay_r, session_id, bridge_statuses.get(session_id),
        )
        if outcome is not None:
            outcome_backed += 1
            if outcome:
                outcome_success += 1
            else:
                outcome_failure += 1
        agent = rec.get("agent_id") or "unknown"

        for entry in steps_by_skill[skill_id]:
            step_id = entry["step_id"]
            t = tallies.setdefault(skill_id, {}).setdefault(step_id, {
                "observed": 0, "skipped": 0, "executions": 0,
                "observed_scored": 0, "observed_success": 0,
                "skipped_scored": 0, "skipped_success": 0,
            })
            t["executions"] += 1
            was_observed = step_id in observed_ids
            t["observed" if was_observed else "skipped"] += 1

            if outcome is None:
                continue
            seen = agent_seen.setdefault(skill_id, {}).setdefault(step_id, {})
            if seen.get(agent, 0) >= cap:
                continue  # one identity must not decide a team's procedure
            seen[agent] = seen.get(agent, 0) + 1
            bucket = "observed" if was_observed else "skipped"
            t[f"{bucket}_scored"] += 1
            if outcome:
                t[f"{bucket}_success"] += 1

    # KNOWABILITY IS NOT DISCRIMINATION. `outcome_backed >= min_n` counts
    # sessions whose outcome can be resolved; it says nothing about whether the
    # resolved outcomes differ. With every session resolving `success` (the
    # measured state of this deployment — see the module docstring) the efficacy
    # of "observed" and "skipped" both collapse to (n + prior/2)/(n + prior),
    # and which branch fires is decided by bucket SIZE alone.
    #
    # THIS STRING IS A REPORT, NOT AN AUTHORISATION. It is summed across every
    # execution of every skill, so one failing session anywhere in the
    # deployment flipped it to `open` — and `_tier_b_proposals` was then applied
    # to steps whose OWN buckets were uniformly successful, which is the same
    # defect one level down. The per-step check that actually authorises a
    # verdict lives in `_discriminates`, where the comparison is made.
    if outcome_backed < min_n:
        tier_b = TIER_B_INSUFFICIENT
    elif not (outcome_success and outcome_failure):
        tier_b = TIER_B_UNIFORM
    else:
        tier_b = TIER_B_OPEN
    # How many steps could actually receive a verdict — every gate that
    # authorises one, not just the sample-size half. An open pass-level string is
    # not the same as a reachable verdict: `PROCEDURE_AGENT_CAP` is spent across
    # BOTH buckets while a verdict needs `min_n` scored executions in EACH, so
    # with both defaulting to 5 no step can be decided by fewer than two distinct
    # agent identities — deliberate (the cap exists precisely so one identity
    # cannot decide a team's procedure) but invisible, and "open with zero
    # proposals, forever" is indistinguishable from "open and nothing found".
    verdict_ready = sum(
        1 for steps in tallies.values() for t in steps.values()
        if _verdict_reachable(t, min_n)
    )
    written = proposed = 0

    # Iterate the INDEXED skills, not `tallies`. A skill only enters `tallies`
    # when it still has in-window evidence — which is exactly the set that does
    # NOT need clearing. Iterating it meant a procedure whose executions had
    # aged out kept its last stats and its last `dead_step` proposal ("skipped
    # in 11 of 12 executions. Remove it?") standing forever, on keys carrying no
    # TTL, against evidence that no longer exists. That is the opposite of the
    # OWM stale-reset shape this pass claims (owm.py runs a second sweep over
    # previously-scored points and deletes what it did not rewrite).
    stale_candidates = await store.written_skills(redis_client)
    touched: set[str] = set()
    for skill_id in sorted(set(steps_by_skill) | set(drift)):
        steps = tallies.get(skill_id, {})
        await store.write_step_stats(redis_client, settings, skill_id, steps)
        written += 1
        touched.add(skill_id)
        proposals: list[dict] = list(drift.get(skill_id, []))
        if steps:
            proposals.extend(_tier_b_proposals(
                skill_id, steps, steps_by_skill.get(skill_id, []),
                min_n, prior_n, delta,
            ))
        # Written even when empty: a proposal with no supporting evidence in
        # the window must DISAPPEAR (OWM's stale-reset shape), not ratchet.
        await store.write_proposals(redis_client, skill_id, proposals)
        proposed += len(proposals)

    # And the skills that left the index entirely — deleted, flipped to draft,
    # or had their specs removed. They can never reappear in `steps_by_skill`,
    # so nothing above can reach them; their stats and proposals were stranded
    # with no TTL and no sweep, still served by GET /procedures.
    #
    # Skipped outright when the skill scan FAILED: the index the sweep compares
    # against is then whatever the last successful pass left, and a Qdrant
    # outage must not read as "every procedure was deleted".
    #
    # AND skipped when the scan SUCCEEDED AND RETURNED NOTHING while the store
    # holds derived state for skills that existed before. `scan_ok` was False
    # only on a raise, but a scan can return an empty page perfectly happily —
    # a QDRANT_COLLECTION pointing at the wrong name, a collection restored
    # empty, a payload index mid-rebuild. `touched` is then empty and the sweep
    # clears every procedure's stats and proposals: silent data loss caused by a
    # transient infrastructure state, on keys nothing else can rebuild (the
    # execution records survive, the derived numbers do not until the next pass).
    # An empty scan is not evidence of deletion.
    sweep = "ok"
    if not scan_ok:
        sweep = "declined: scan failed"
    elif not skills and stale_candidates:
        sweep = "declined: vacuous scan"
        logger.warning(
            "procedure orphan sweep DECLINED: the skill scan returned 0 active "
            "skills while %s already hold derived state. An empty scan is not "
            "evidence of deletion — check QDRANT_COLLECTION and the collection's "
            "health; stats and proposals are left standing until a scan returns "
            "something. Affected: %s",
            len(stale_candidates), sorted(stale_candidates)[:20],
        )
    orphaned = 0
    for skill_id in (sorted(stale_candidates - touched) if sweep == "ok" else []):
        try:
            await store.clear_skill(redis_client, skill_id)
            orphaned += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("orphan sweep failed for %s: %s", skill_id, exc)

    result = {
        "status": "ok",
        "executions": len(executions),
        "skills": written,
        "proposals": proposed,
        "spec_drift": sum(len(v) for v in drift.values()),
        "orphans_cleared": orphaned,
        # Reported, not merely logged: "0 orphans cleared" is what a healthy
        # pass and a declined sweep both look like from outside.
        "orphan_sweep": sweep,
        "outcome_backed_executions": outcome_backed,
        "outcome_success": outcome_success,
        "outcome_failure": outcome_failure,
        "tier_b": tier_b,
        "verdict_ready_steps": verdict_ready,
        "health": "ok",
    }
    # Persisted, not merely returned: the Celery result backend is not a
    # surface a human reads, and "Tier B is closed for lack of outcome signal"
    # is the single most important thing this pass has to say on today's data.
    await store.record_run(redis_client, result)
    return result


def _discriminates(t: dict) -> bool:
    """Both outcome classes present in THIS STEP's own scored buckets.

    The pass-level gate is summed over every execution of every skill, so one
    failing session anywhere in the deployment opened it — and the comparison it
    authorised is per step. Applied to a step whose own scored outcomes are
    uniformly successful, `compute_efficacy` returns the IDENTICAL Beta-prior
    value for both buckets, `eff_skip >= eff_obs - delta` holds exactly, and a
    `dead_step` ("remove it?") is emitted on a signal that separated nothing.
    All-failure is refused for the mirror reason: every step of a procedure that
    only ever ran in failing sessions would read as load-bearing.
    """
    scored = t["observed_scored"] + t["skipped_scored"]
    successes = t["observed_success"] + t["skipped_success"]
    return 0 < successes < scored


def _verdict_reachable(t: dict, min_n: int) -> bool:
    return (t["observed_scored"] >= min_n and t["skipped_scored"] >= min_n
            and _discriminates(t))


def _tier_b_proposals(skill_id, steps, entries, min_n, prior_n, delta) -> list[dict]:
    from app.owm import compute_efficacy

    by_id = {e["step_id"]: e for e in entries}
    out: list[dict] = []
    for step_id, t in steps.items():
        if not _verdict_reachable(t, min_n):
            continue
        eff_obs = compute_efficacy(t["observed_success"], t["observed_scored"], prior_n)
        eff_skip = compute_efficacy(t["skipped_success"], t["skipped_scored"], prior_n)
        entry = by_id.get(step_id, {})
        text = entry.get("step_text") or step_id
        if eff_skip < eff_obs - delta and not entry.get("load_bearing"):
            out.append({
                "id": uuid.uuid4().hex[:12], "kind": "load_bearing",
                "skill_id": skill_id, "step_id": step_id,
                "detail": (f"Skipping \"{text}\" tracks with worse outcomes "
                           f"({eff_skip:.2f} vs {eff_obs:.2f} over "
                           f"{t['skipped_scored']}/{t['observed_scored']} scored "
                           f"executions). Mark it load-bearing?"),
            })
        elif eff_skip >= eff_obs - delta and t["skipped"] >= min_n:
            out.append({
                "id": uuid.uuid4().hex[:12], "kind": "dead_step",
                "skill_id": skill_id, "step_id": step_id,
                "detail": (f"\"{text}\" was skipped in {t['skipped']} of "
                           f"{t['executions']} executions with no measurable cost "
                           f"({eff_skip:.2f} vs {eff_obs:.2f}). Remove it?"),
            })
    return out


# Import placement is load-bearing (the owm.py / confluence-collector
# precedent): celery_app is imported at the BOTTOM so this module's public
# surface exists before the worker imports it.
import asyncio  # noqa: E402

from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.procedures.harden.run_procedure_hardening")
def run_procedure_hardening() -> dict:
    """Beat fires unconditionally; the task self-gates and never raises."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.PROCEDURE_ENABLED:
        return {"status": "disabled"}
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("procedure hardening crashed")
        return {"status": "error", "error": str(exc)}


async def _run() -> dict:
    import redis.asyncio

    from app.config import get_settings
    from app.db.vector import VectorClient

    settings = get_settings()
    # proc:* keys live on the CORTEX DATA db; evals/replay on the replay db.
    r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    replay_r = redis.asyncio.from_url(settings.RP_REDIS_URL, decode_responses=True)
    vector = VectorClient(settings)
    try:
        return await run_pass(r, replay_r, vector, settings)
    except Exception as exc:  # noqa: BLE001
        # A crashed run must not read as "never ran" on the next GET /procedures
        # — that is the state an operator uses to decide whether to look.
        await store.record_run(r, {"status": "error", "health": "error",
                                   "error": str(exc)})
        raise
    finally:
        for closer in (r.aclose, replay_r.aclose, vector.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass
