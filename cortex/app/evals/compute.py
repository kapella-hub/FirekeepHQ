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
from app.evals.store import store_eval

logger = logging.getLogger(__name__)

_EVAL_DLQ_PREFIX = "rp:eval_dlq:"


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
            "failure_type": failure_type,  # "infra" or "scoring"
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await replay_redis.set(key, data, ex=86400 * 7)  # Keep for 7 days
    except Exception:
        pass


async def compute_session_eval(
    replay_redis: aioredis.Redis,
    session_id: str,
    trigger: Literal["session_complete", "session_abandon", "manual"] = "session_complete",
    outcome: str | None = None,
) -> EvalResult | None:
    """Compute and store eval metrics for a session.

    Reads replay events, runs Tier 1 scorers, stores the result.
    Returns the EvalResult or None on failure. Never raises.
    """
    try:
        # Import reader here to avoid circular imports
        from replay.reader import get_session_timeline, get_session_summary

        # Get session summary for high-level stats
        summary = await get_session_summary(replay_redis, session_id)
        event_count = summary.get("event_count", 0)

        if event_count == 0:
            logger.debug("No replay events for session %s, skipping eval", session_id)
            return None

        # Get all events for scoring
        timeline = await get_session_timeline(
            replay_redis, session_id, limit=1000,
        )
        events = timeline.get("events", [])

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
            event_count=event_count,
            duration_ms=summary.get("duration_ms"),
            outcome=outcome or ("failure" if failure_event_ids else "success"),
            failure_event_ids=failure_event_ids,
            has_failures=bool(failure_event_ids),
            runtime=runtime,
            client_version=client_version,
            instructions=instructions,
            briefing_delivered=briefing_delivered,
            agents=agents,
        )

        # Store
        await store_eval(replay_redis, result)

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
                _trigger_to_event = {"session_complete": "session.completed", "session_abandon": "session.abandoned"}
                session_event = _trigger_to_event.get(trigger)
                if session_event:
                    await fire_webhooks(cortex_redis, session_event, {
                        "session_id": session_id, "outcome": result.outcome,
                        "event_count": result.event_count, "has_failures": result.has_failures,
                    })
                await fire_webhooks(cortex_redis, "eval.computed", {
                    "session_id": session_id, "outcome": result.outcome,
                    "event_count": result.event_count, "has_failures": result.has_failures,
                    "metric_count": len(metrics),
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
