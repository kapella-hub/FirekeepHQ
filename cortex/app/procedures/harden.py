"""The nightly hardening pass.

TWO TIERS, and the split is the design's answer to a measured weakness rather
than a hedge:

  Tier A — frequency. Needs no outcome signal. "This procedure ran 41 times;
           step 3 was skipped in 24 of them" is true and useful on day one.
  Tier B — efficacy verdicts. Gated on executions whose sessions have a KNOWABLE
           outcome. Measured on this repo, no production emitter passes outcome=
           to replay except Bridge's session lifecycle, so _failure_rate is 0.0
           and effectively every session reads as a success. A pass that trusted
           that would find every step dead and propose deleting the procedure.
           Closed is the correct state; it reports that it is closed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.procedures import store

logger = logging.getLogger(__name__)


async def _resolve_outcome(replay_r, session_id: str) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    Excludes on ANY doubt: no replay events, no outcome-bearing event (I4 —
    _failure_rate returns 0.0 in that case, which reads as success), an eval
    that cannot be computed, or session_success's ambiguous middle band.
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
        return session_success(data, None)
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
    try:
        await store.rebuild_index(vector, redis_client, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("index rebuild failed during hardening: %s", exc)

    index = await store.load_index(redis_client)
    steps_by_skill: dict[str, list[dict]] = {}
    for entry in index:
        if not isinstance(entry, dict) or not entry.get("skill_id"):
            continue
        steps_by_skill.setdefault(entry["skill_id"], []).append(entry)

    executions = await store.iter_executions(redis_client)
    # tallies[skill][step] -> counters
    tallies: dict[str, dict[str, dict]] = {}
    # agent_seen[skill][step][agent] -> count, for the fairness cap
    agent_seen: dict[str, dict[str, dict[str, int]]] = {}
    outcome_backed = 0

    for rec in executions:
        skill_id = rec.get("skill_id")
        if skill_id not in steps_by_skill:
            continue
        if not _within_window(rec, cutoff):
            continue
        observed_ids = set(rec.get("observed") or {})
        # I2: no sibling evidence => this execution says nothing about any step.
        # Without it every kiro session (fs_write maps to `other`), every
        # shell-heavy session and every personal-mode session votes to delete
        # each load-bearing step it never had the ability to observe.
        if not observed_ids:
            continue

        outcome = await _resolve_outcome(replay_r, rec.get("session_id") or "")
        if outcome is not None:
            outcome_backed += 1
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

    tier_b_open = outcome_backed >= min_n
    written = proposed = 0

    for skill_id, steps in tallies.items():
        await store.write_step_stats(redis_client, settings, skill_id, steps)
        written += 1
        proposals: list[dict] = []
        if tier_b_open:
            proposals = _tier_b_proposals(
                skill_id, steps, steps_by_skill[skill_id], min_n, prior_n, delta
            )
        # Written even when empty: a proposal with no supporting evidence in
        # the window must DISAPPEAR (OWM's stale-reset shape), not ratchet.
        await store.write_proposals(redis_client, skill_id, proposals)
        proposed += len(proposals)

    return {
        "status": "ok",
        "executions": len(executions),
        "skills": written,
        "proposals": proposed,
        "outcome_backed_executions": outcome_backed,
        "tier_b": "open" if tier_b_open else "insufficient outcome signal",
    }


def _tier_b_proposals(skill_id, steps, entries, min_n, prior_n, delta) -> list[dict]:
    from app.owm import compute_efficacy

    by_id = {e["step_id"]: e for e in entries}
    out: list[dict] = []
    for step_id, t in steps.items():
        if t["observed_scored"] < min_n or t["skipped_scored"] < min_n:
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
    finally:
        for closer in (r.aclose, replay_r.aclose, vector.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass
