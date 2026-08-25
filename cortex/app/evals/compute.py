"""Eval computation — orchestrates metric scoring from replay data.

Called after session completion to compute and store eval results.
Designed to be fire-and-forget — never blocks the calling operation.
"""

from __future__ import annotations

import logging
from typing import Literal

import redis.asyncio as aioredis

from app.evals.models import EvalResult
from app.evals.scorers import brier_score, compute_tier1_metrics
from app.evals.store import get_eval, store_eval

logger = logging.getLogger(__name__)

_EVAL_DLQ_PREFIX = "rp:eval_dlq:"
_GRADE_SCAN_MAX = 5000  # one-shot snapshot cap; disclosed in spec D7
_METRIC_SCAN_MAX = 5000  # module constant; matches get_session_event_ids
# default + find_terminal_grade's _GRADE_SCAN_MAX. Caps the metrics scan (and,
# via import, the OWM memory_read join) — the cap is the SAME value as the
# grade lift's, but a separate constant because the two scans are
# conceptually distinct call sites (task 4, outcome truth PR2 D3).


_GRADE_HYDRATE_WINDOW = 200


async def find_terminal_grade(
    replay_redis, session_id: str,
) -> tuple[str | None, str | None]:
    """Newest recognized grade pair, via a ONE-SHOT ID snapshot + local
    backward hydration.

    Snapshot-first is load-bearing on two counts (round-6 finding 2 + round-5
    finding 6): (a) a live rank-relative window read is not stable — events
    appended between page reads shift negative ranks, so a later page can
    repeat a prior page and skip the grade; (b) get_event_batch omits IDs
    whose bodies were trimmed/expired. Capturing the ID list ONCE fixes (a)
    (the list can't shift), and walking IDs in windows (not hydrated events)
    fixes (b) (a window with missing bodies is walked past)."""
    from app.evals.models import grade_from_events
    from replay.reader import get_session_event_ids, get_event_batch

    ids = await get_session_event_ids(replay_redis, session_id,
                                      limit=_GRADE_SCAN_MAX)
    # walk newest-first in windows; ids is oldest->newest
    for end in range(len(ids), 0, -_GRADE_HYDRATE_WINDOW):
        window = ids[max(0, end - _GRADE_HYDRATE_WINDOW):end]
        events = await get_event_batch(replay_redis, window)
        tr, src = grade_from_events(events)
        if tr:
            return tr, src
    return None, None


async def _record_eval_failure(
    replay_redis: aioredis.Redis,
    session_id: str,
    error_msg: str,
    failure_type: str = "unknown",
) -> None:
    """Record a failed eval attempt for later investigation."""
    try:
        import json
        from datetime import datetime, timezone

        key = f"{_EVAL_DLQ_PREFIX}{session_id}"
        data = json.dumps({
            "session_id": session_id,
            "error": str(error_msg)[:500],
            "failure_type": failure_type,  # "infra", "scoring", or "store"
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await replay_redis.set(key, data, ex=86400 * 7)  # Keep for 7 days
    except Exception:
        pass


async def compute_session_eval(
    replay_redis: aioredis.Redis,
    session_id: str,
    trigger: Literal["session_complete", "session_abandon", "manual"] = "session_complete",
    task_result_hint: str | None = None,
) -> EvalResult | None:
    """Compute and store eval metrics for a session.

    Reads replay events, runs Tier 1 scorers, stores the result.
    Returns the EvalResult or None on failure. Never raises.
    """
    try:
        # Import reader here to avoid circular imports
        from replay.reader import get_event_batch, get_session_event_ids, get_session_summary

        # Get session summary for high-level stats
        summary = await get_session_summary(replay_redis, session_id)
        event_count = summary.get("event_count", 0)

        if event_count == 0:
            logger.debug("No replay events for session %s, skipping eval", session_id)
            return None

        # Full-session snapshot+hydrate (PR1 primitives, task 4): the metrics
        # scan, the Brier predict/reconcile join and failure_event_ids must see
        # the WHOLE session, not the oldest-1000 window get_session_timeline
        # used to silently truncate to. Capped at _METRIC_SCAN_MAX, with the
        # cap surfaced via metrics_truncated rather than dropped silently.
        ids = await get_session_event_ids(
            replay_redis, session_id, limit=_METRIC_SCAN_MAX,
        )
        events = await get_event_batch(replay_redis, ids)
        metrics_truncated = len(ids) >= _METRIC_SCAN_MAX
        if metrics_truncated:
            logger.warning(
                "eval metrics truncated at %d events for session %s",
                _METRIC_SCAN_MAX, session_id,
            )

        if not events:
            return None

        # Compute Tier 1 metrics
        metrics = compute_tier1_metrics(events)

        # Brier calibration score from agent.action.predict + agent.action.reconcile events
        predict_by_id: dict[str, float] = {}
        for e in events:
            if e.get("event_type") == "agent.action.predict":
                action_id = e.get("payload", {}).get("action_id")
                confidence = (e.get("payload", {}).get("prediction") or {}).get("confidence")
                if action_id is not None and confidence is not None:
                    predict_by_id[action_id] = float(confidence)

        reconcile_actions = []
        for e in events:
            if e.get("event_type") == "agent.action.reconcile":
                aid = e.get("payload", {}).get("action_id")
                if aid in predict_by_id:
                    reconcile_actions.append({
                        "prediction_confidence": predict_by_id[aid],
                        "prediction_match_score": e.get("payload", {}).get("prediction_match_score"),
                    })

        brier = brier_score(reconcile_actions)
        if brier is not None:
            metrics["brier_score"] = brier

        logger.info(
            "Eval computed for session %s: %d events, memory_reads=%d, memory_writes=%d, tool_success=%.2f",
            session_id, len(events),
            metrics.get("memory_read_count", 0),
            metrics.get("memory_write_count", 0),
            metrics.get("tool_success_rate", 0),
        )

        # Identify failure events
        failure_event_ids = [
            e.get("id", "") for e in events
            if e.get("outcome") == "failure"
        ]

        from app.evals.models import recognized_grade_pair
        task_result, task_result_source = recognized_grade_pair(
            task_result_hint, "self_reported")
        if task_result is None:
            task_result, task_result_source = await find_terminal_grade(
                replay_redis, session_id)

        # Attribution (Living Instructions round 2): read from the
        # session_start event in the timeline this function ALREADY loaded —
        # no new I/O. A session from a client that predates 0.1.41 carries no
        # attribution keys and reads as unattributed — honestly. Malformed
        # values read as absent, never raise.
        runtime: str | None = None
        client_version: str | None = None
        instructions: dict[str, str] | None = None
        briefing_delivered: bool | None = None

        start_payload: dict | None = None
        for e in events:
            if e.get("event_type") == "session_start":
                p = e.get("payload")
                # A junk (non-dict) payload carries no readable receipts:
                # everything stays None/unknown rather than reading an empty
                # payload as a measured "no briefing".
                if isinstance(p, dict):
                    start_payload = p
                break

        if start_payload is not None:
            def _attr(key: str) -> str | None:
                value = start_payload.get(key)
                return value if isinstance(value, str) and value else None

            runtime = _attr("runtime")
            client_version = _attr("client_version")
            instr: dict[str, str] = {}
            for payload_key, out_key in (
                ("instr_rendered", "rendered"),
                ("instr_expected", "expected"),
                ("instr_gateway", "gateway"),
            ):
                value = _attr(payload_key)
                if value is not None:
                    instr[out_key] = value
            instructions = instr or None
            # The fetch receipt that already exists: a briefing_id on the
            # session means GET /briefing was actually delivered to it. The
            # KEY must be present to claim a measurement either way — a
            # session_start payload from a pre-round-2 bridge has no
            # briefing_id key at all, and reading that absence as a measured
            # False is exactly the absent-vs-measured conflation the contract
            # bans (external review 2026-08-12).
            briefing_delivered = (
                bool(start_payload["briefing_id"])
                if "briefing_id" in start_payload
                else None
            )

        agents_raw = summary.get("agents", [])
        agents = (
            [a for a in agents_raw if isinstance(a, str)]
            if isinstance(agents_raw, list) else []
        )

        result = EvalResult(
            session_id=session_id,
            trigger=trigger,
            metrics=metrics,
            metrics_truncated=metrics_truncated,
            event_count=event_count,
            duration_ms=summary.get("duration_ms"),
            outcome=task_result or ("failure" if failure_event_ids else "unknown"),
            task_result=task_result,
            task_result_source=task_result_source,
            failure_event_ids=failure_event_ids,
            has_failures=bool(failure_event_ids),
            runtime=runtime,
            client_version=client_version,
            instructions=instructions,
            briefing_delivered=briefing_delivered,
            agents=agents,
        )

        # Store
        stored = await store_eval(replay_redis, result)
        if not stored:
            # The store kept a different record, or persistence failed. The
            # candidate must not drive features/webhooks/the response unless
            # something authoritative exists (spec D9b/c).
            authoritative = await get_eval(replay_redis, session_id)
            if authoritative is None:
                await _record_eval_failure(
                    replay_redis, session_id,
                    "store_eval rejected and no authoritative record readable",
                    failure_type="store",
                )
                return None
            result = authoritative

        # Extract features for pattern analysis (best-effort)
        try:
            from app.patterns.extractor import extract_session_features
            from app.patterns.store import store_features, get_feature_count
            features = await extract_session_features(replay_redis, session_id, eval_result=result)
            if features:
                await store_features(replay_redis, features)
                count = await get_feature_count(replay_redis)
                if count >= 5 and (count % 5 == 0 if count <= 20 else count % 10 == 0):
                    from app.patterns.analyzer import analyze_patterns
                    from app.patterns.store import store_patterns
                    # KNOWN-DEAD, LEFT DEAD (outcome truth D11): the lifecycle import
                    # below has never resolved (promote_all_patterns lives in store.py),
                    # so this auto-analyze/promote block never runs — and reviving it
                    # would rewrite EVERY stored card (promote_all_patterns stores all
                    # cards back even when analysis returns []), resurrecting
                    # fabricated-era cards. Revival needs card provenance — PR3.
                    from app.patterns.lifecycle import promote_all_patterns
                    patterns = await analyze_patterns(replay_redis)
                    if patterns:
                        await store_patterns(replay_redis, patterns)
                    await promote_all_patterns(replay_redis)
        except Exception:
            logger.warning("Pattern extraction failed for session %s", session_id, exc_info=True)

        logger.info(
            "Eval stored for session %s: %d metrics, %d failures",
            session_id, len(metrics), len(failure_event_ids),
        )

        # Fire webhooks (best-effort, uses Cortex Redis DB 0)
        try:
            import os
            cortex_redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
            cortex_redis = aioredis.from_url(cortex_redis_url)
            try:
                from app.webhooks import fire_webhooks
                # D9f: webhook DELIVERY ORDER is not authoritative — the eval
                # store (GET /evals/sessions/{id}) is the sole truth, and a
                # consumer must re-fetch on session_id, never infer grade
                # order from arrival order. Re-read right here so a
                # superseded computation still emits the current
                # authoritative grade, not the stale one it computed.
                latest = await get_eval(replay_redis, session_id)
                if latest is None:
                    logger.error(
                        "Suppressing eval webhooks for %s: authoritative eval unreadable",
                        session_id,
                    )
                else:
                    result = latest
                    pair = {
                        "task_result": latest.task_result,
                        "task_result_source": latest.task_result_source,
                    }
                    _trigger_to_event = {"session_complete": "session.completed", "session_abandon": "session.abandoned"}
                    session_event = _trigger_to_event.get(trigger)
                    if session_event:
                        await fire_webhooks(cortex_redis, session_event, {
                            "session_id": session_id, "outcome": latest.outcome,
                            "event_count": latest.event_count, "has_failures": latest.has_failures,
                            **pair,
                        })
                    await fire_webhooks(cortex_redis, "eval.computed", {
                        "session_id": session_id, "outcome": latest.outcome,
                        "event_count": latest.event_count, "has_failures": latest.has_failures,
                        "metric_count": len(latest.metrics),
                        **pair,
                    })
            finally:
                await cortex_redis.aclose()
        except Exception:
            pass  # Non-critical

        return result

    except (ConnectionError, OSError, aioredis.RedisError) as e:
        logger.warning("Eval computation failed (infra) for session %s: %s", session_id, e)
        await _record_eval_failure(replay_redis, session_id, str(e), failure_type="infra")
        return None
    except Exception as e:
        logger.warning("Eval computation failed (scoring) for session %s: %s", session_id, e)
        await _record_eval_failure(replay_redis, session_id, str(e), failure_type="scoring")
        return None
