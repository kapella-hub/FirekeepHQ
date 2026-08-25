"""Outcome-Weighted Memory (OWM): recall ranked by real-world results.

Every /memory/recall stamps the RETURNED memory ids into its replay
``memory_read`` event; auto-evals + Bridge status say how each session ENDED.
This module joins the two: for each memory, of the sessions that recalled it
and have a KNOWN outcome, what fraction succeeded? The score is shrunk toward
neutral 0.5 with a Beta prior (``OWM_PRIOR_N`` pseudo-observations), so a
memory seen once or twice cannot swing rankings — the term only speaks once
evidence accumulates. Results land on the Qdrant payload (``owm_efficacy``,
``owm_n``, ``owm_updated_at``) where:

  - the RAG lifecycle scorer multiplies recall scores by
    ``1 + OWM_WEIGHT * 2 * (efficacy - 0.5)`` (neutral == bit-identical to
    pre-OWM ranking, including every memory never scored), and
  - the GC composite eviction score gains an efficacy factor (neutral 0.5 is
    bit-identical; persistently misleading memories age out faster).

Session success is DETERMINISTIC, no LLM, no statistics beyond the shrinkage:
Bridge ``abandoned`` is a failure regardless of grade; otherwise the recognized
(task_result, task_result_source) pair decides (2026-08-23, outcome truth) —
"success"/"failure" pass through, and "partial", a sourceless grade, or an
absent/legacy record are EXCLUDED from the join rather than guessed. Runs as a
nightly Celery pass (confluence-collector registration pattern); the whole
pass is idempotent — recomputed from scratch every run over the eval window,
so drift self-heals.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Callable

from app.evals.compute import _METRIC_SCAN_MAX  # single source of truth (task 4 brief):
# same cap as the eval metrics scan, imported rather than re-declared so the
# two can never drift apart.
from app.evals.models import recognized_grade_pair

logger = logging.getLogger(__name__)


async def _default_events_fn(replay_r, session_id: str) -> list[dict]:
    """Full-session snapshot+hydrate (PR1 primitives, task 4): the SAME two
    calls compute.py's metrics scan makes, capped at the same _METRIC_SCAN_MAX.
    Returns a plain list of event dicts — the caller filters by event_type."""
    from replay.reader import get_event_batch, get_session_event_ids

    ids = await get_session_event_ids(replay_r, session_id, limit=_METRIC_SCAN_MAX)
    return await get_event_batch(replay_r, ids)


def compute_efficacy(successes: int, n: int, prior_n: int = 5) -> float:
    """Beta-shrunk success fraction: (s + prior/2) / (n + prior). 0.5 at n=0."""
    return (successes + prior_n * 0.5) / (n + prior_n)


def session_success(eval_data: dict, bridge_status: str | None) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    2026-08-23 (outcome truth): grades come from the recognized
    (task_result, task_result_source) pair, replacing the failure_rate
    heuristic whose 0.0 was produced by Bridge's hard-coded completion stamp.
    This function reads RAW stored eval JSON, so it checks the pair itself
    (D2c): a sourceless grade is not evidence. "partial" and ungraded/legacy
    records return None — excluded rather than guessed. Bridge `abandoned`
    still overrides: a walked-away session is a failure regardless of grade.
    """
    if bridge_status == "abandoned":
        return False
    tr, _src = recognized_grade_pair(
        eval_data.get("task_result"), eval_data.get("task_result_source"))
    if tr == "success":
        return True
    if tr == "failure":
        return False
    return None


async def run_pass(replay_r, vector, settings, *,
                   bridge_statuses: dict[str, str] | None = None,
                   events_fn: Callable | None = None) -> dict:
    """One full OWM scoring pass. Returns a status dict; never raises for a
    single bad session/memory (those are counted, logged, and skipped).

    Fairness/hygiene guarantees (wf_51dd7c4e review):
      - a session counts ONCE per memory, and a single agent identity counts at
        most OWM_AGENT_CAP observations per memory (a CI bot's failing loop
        cannot bury a shared memory);
      - corpus chunks and skill points are excluded (outcome-scoring a document
        by ambient session failure is meaningless);
      - memories scored in a previous pass but absent from this run's evidence
        get their OWM keys DELETED (reset to neutral) — no permanent penalties,
        no self-reinforcing death spiral once evals expire (30d TTL);
      - abandoned-session detection depends on the Bridge status map, which the
        REST route caps at its 200 newest sessions — beyond that horizon a
        walked-away session with clean metrics counts as success (documented
        best-effort limit).
    """
    import json as _json

    events_fn = events_fn or _default_events_fn
    bridge_statuses = bridge_statuses or {}
    out = {"sessions_scanned": 0, "sessions_joined": 0, "memories_scored": 0,
           "write_errors": 0, "stale_reset": 0}
    agent_cap = int(getattr(settings, "OWM_AGENT_CAP", 5) or 5)

    window_start = time.time() - settings.OWM_WINDOW_DAYS * 86400
    session_ids = await replay_r.zrangebyscore("rp:eval_index", window_start, "+inf")

    # memory_id -> agent_id -> [successes, n] (n capped per agent at write-out)
    stats: dict[str, dict[str, list[int]]] = {}
    for sid_raw in session_ids:
        sid = sid_raw.decode() if isinstance(sid_raw, bytes) else sid_raw
        out["sessions_scanned"] += 1
        try:
            raw = await replay_r.get(f"rp:eval:{sid}")
            if not raw:
                continue  # 30d value TTL beat the never-pruned index entry
            success = session_success(_json.loads(raw), bridge_statuses.get(sid))
            if success is None:
                continue

            # Full-session snapshot+hydrate (task 4): the old
            # get_session_timeline(limit=1000) fetch applied the memory_read
            # filter AFTER pagination, so a late memory_read past the
            # oldest-1000 window never joined. events_fn returns the whole
            # (capped) session as a plain list — no envelope to unwrap — and
            # the type filter is applied here in Python instead.
            all_events = await events_fn(replay_r, sid)
            events = [e for e in (all_events or []) if e.get("event_type") == "memory_read"]
            mem_agents: dict[str, str] = {}
            for ev in events:
                agent = str(ev.get("agent_id") or "unknown")
                ids = (ev.get("payload") or {}).get("memory_ids") or []
                for m in ids:
                    if m:
                        mem_agents.setdefault(str(m), agent)
            if not mem_agents:
                continue  # pre-OWM event (no ids stamped) or no recalls

            out["sessions_joined"] += 1
            for mid, agent in mem_agents.items():  # once per (session, memory)
                per_agent = stats.setdefault(mid, {})
                s, n = per_agent.get(agent, (0, 0))
                if n >= agent_cap:
                    continue  # one identity cannot dominate a memory's score
                per_agent[agent] = [s + (1 if success else 0), n + 1]
        except Exception as exc:  # noqa: BLE001 — one bad session never stops the pass
            logger.warning("OWM: session %s skipped: %s", sid, exc)

    # Exclude corpus chunks + skills: their ids flow through the same recall
    # results, but playbooks/documents must not carry outcome scores.
    scorable: dict[str, tuple[int, int]] = {}
    all_ids = list(stats.keys())
    for i in range(0, len(all_ids), 100):
        batch = all_ids[i:i + 100]
        try:
            points = await vector._client.retrieve(
                collection_name=settings.QDRANT_COLLECTION, ids=batch,
                with_payload=True, with_vectors=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OWM: retrieve batch failed: %s", exc)
            continue
        for pt in points:
            payload = pt.payload or {}
            if payload.get("memory_type") == "skill" or payload.get("source") == "corpus":
                continue
            per_agent = stats.get(str(pt.id)) or {}
            s_total = sum(v[0] for v in per_agent.values())
            n_total = sum(v[1] for v in per_agent.values())
            if n_total:
                scorable[str(pt.id)] = (s_total, n_total)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    written: set[str] = set()
    for mid, (successes, n) in scorable.items():
        payload = {
            "owm_efficacy": round(compute_efficacy(successes, n, settings.OWM_PRIOR_N), 4),
            "owm_n": n,
            "owm_updated_at": now_iso,
        }
        try:
            await vector._client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload=payload,
                points=[mid],
            )
            out["memories_scored"] += 1
            written.add(mid)
        except Exception as exc:  # noqa: BLE001 — deleted/foreign ids are expected
            logger.warning("OWM: payload write failed for %s: %s", mid, exc)
            out["write_errors"] += 1

    # Stale reset: previously-scored points with no in-window evidence go back
    # to neutral (keys deleted). Recompute-from-scratch is only honest if
    # absence of evidence actually clears the old verdict.
    try:
        from qdrant_client import models as _qm

        offset = None
        while True:
            points, offset = await vector._client.scroll(
                collection_name=settings.QDRANT_COLLECTION,
                scroll_filter=_qm.Filter(must=[_qm.FieldCondition(
                    key="owm_n", range=_qm.Range(gte=1))]),
                limit=1000, offset=offset,
                with_payload=False, with_vectors=False)
            for pt in points or []:
                pid = str(pt.id)
                if pid in written:
                    continue
                try:
                    await vector._client.delete_payload(
                        collection_name=settings.QDRANT_COLLECTION,
                        keys=["owm_efficacy", "owm_n", "owm_updated_at"],
                        points=[pid])
                    out["stale_reset"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OWM: stale reset failed for %s: %s", pid, exc)
            if not offset:
                break
    except Exception as exc:  # noqa: BLE001 — reset is hygiene, never fatal
        logger.warning("OWM: stale-reset sweep failed: %s", exc)

    logger.info("OWM pass: %s", out)
    return out


async def _fetch_bridge_statuses(settings) -> dict[str, str]:
    """Best-effort session-status map from Bridge REST (abandoned == failure).
    Bridge unreachable degrades to eval-only signals, never fails the pass."""
    try:
        import httpx

        headers = {}
        internal = getattr(settings, "FIREKEEP_INTERNAL_KEY", "") or ""
        if internal:
            headers["X-API-Key"] = internal
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{settings.BRIDGE_URL}/sessions",
                                    params={"limit": 200}, headers=headers)  # route clamps at 200
            resp.raise_for_status()
            rows = resp.json().get("sessions", [])
        return {r["session_id"]: r.get("status", "") for r in rows
                if r.get("session_id")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("OWM: bridge statuses unavailable (%s) — eval-only", exc)
        return {}


async def _run_owm_impl() -> dict:
    import redis.asyncio

    from app.config import get_settings
    from app.db.vector import VectorClient

    settings = get_settings()
    r = redis.asyncio.from_url(settings.RP_REDIS_URL, decode_responses=True)
    vector = VectorClient(settings)
    try:
        statuses = await _fetch_bridge_statuses(settings)
        return await run_pass(r, vector, settings, bridge_statuses=statuses)
    finally:
        for closer in (r.aclose, vector.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


# Import placement is load-bearing (confluence-collector precedent): celery_app
# is imported at the BOTTOM so this module's public surface exists first.
from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.owm.run_owm_scoring")
def run_owm_scoring() -> dict:
    """Beat fires unconditionally; the task self-gates on OWM_ENABLED and never
    raises — failures come back as a status dict."""
    from app.config import get_settings

    if not get_settings().OWM_ENABLED:
        return {"status": "disabled"}
    try:
        return asyncio.run(_run_owm_impl())
    except Exception as exc:  # noqa: BLE001
        logger.exception("OWM pass crashed")
        return {"status": "error", "error": str(exc)}
